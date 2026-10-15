"""P3 POI matching and explicit map actions over the saved discovery results."""

import asyncio
import unicodedata
from time import monotonic
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from ..domain.places import (
    ConfirmManualPoi,
    ConfirmOrigin,
    ConfirmPoi,
    MapAction,
    MatchPlaces,
    Poi,
)
from ..integrations.amap import Amap, AmapBudgetExceeded, AmapError
from ..services.places import match_pois, normalize_poi
from ..storage.database import now
from .trips import _call


async def _before_amap_request(application):
    if not hasattr(application.state, "amap_lock"):
        application.state.amap_lock = asyncio.Lock()
        application.state.amap_last_call_monotonic = 0.0
    async with application.state.amap_lock:
        loop = asyncio.get_running_loop()
        delay = 1.1 - (loop.time() - application.state.amap_last_call_monotonic)
        if delay > 0:
            await asyncio.sleep(delay)
        application.state.amap_last_call_monotonic = loop.time()


def _amap(request, *, max_calls, expected_version, operation="poi_search"):
    settings = getattr(request.app.state, "settings", None)
    http = getattr(request.app.state, "http", None)
    if settings is None or not settings.amap_web_key or http is None:
        raise HTTPException(
            503,
            detail={
                "code": "amap_unavailable",
                "message": "高德地点服务暂不可用，请稍后重试",
            },
        )

    action_id = None
    started = None

    async def before_request():
        nonlocal action_id, started
        await _before_amap_request(request.app)
        action_id = _call(
            request,
            "begin_api_action",
            request.path_params["trip_id"],
            expected_version,
            "amap",
            operation,
        )
        started = monotonic()

    async def after_request(path, payload, error):
        nonlocal action_id
        if action_id is not None:
            status = (
                "completed"
                if error is None
                else "failed"
                if isinstance(error, AmapError) and error.infocode != "UNKNOWN"
                else "unknown"
            )
            if error is None and (
                not isinstance(payload.get("pois"), list)
                or any(not isinstance(row, dict) for row in payload["pois"])
                or (operation == "poi_detail" and len(payload["pois"]) != 1)
            ):
                status = "failed"
            finished = _call(
                request,
                "finish_api_action",
                action_id,
                status=status,
                elapsed_seconds=monotonic() - started,
                metadata={"api_version": "v5"},
            )
            if not finished:
                raise HTTPException(
                    503,
                    detail={
                        "code": "usage_unavailable",
                        "message": "用量记录未能保存，请稍后重试",
                    },
                )
            action_id = None

    return Amap(
        http,
        settings.amap_web_key,
        max_calls=max_calls,
        before_request=before_request,
        after_request=after_request,
    )


async def _search(request, amap, query, city, expected_version):
    cached = _call(request, "cached_poi_search", query, city)
    if cached is not None:
        action_id = _call(
            request,
            "begin_api_action",
            request.path_params["trip_id"],
            expected_version,
            "amap",
            "poi_search",
            cached=True,
        )
        finished = _call(
            request,
            "finish_api_action",
            action_id,
            status="completed",
            elapsed_seconds=0,
            metadata={"api_version": "v5"},
        )
        if not finished:
            raise HTTPException(
                503,
                detail={
                    "code": "usage_unavailable",
                    "message": "用量记录未能保存，请稍后重试",
                },
            )
        return cached
    raw = await amap.search_candidates(query, city)
    read_at = now()
    response = {
        "status": "1",
        "search_complete": raw.get("search_complete") is True,
        "pois": [
            Poi.model_validate(
                request.app.state.settings.redact(normalize_poi(poi, read_at, "v5"))
            ).model_dump(mode="json")
            for poi in raw.get("pois", [])
        ],
    }
    return _call(request, "save_poi_search", query, city, response, read_at)


async def _verify_detail(request, poi_id, context, expected_version):
    amap = _amap(
        request, max_calls=1, expected_version=expected_version, operation="poi_detail"
    )
    try:
        async with asyncio.timeout(30):
            response = await amap.detail(poi_id)
    except (AmapError, ValueError, TimeoutError):
        raise HTTPException(
            502,
            detail={
                "code": "poi_detail_failed",
                "message": "POI详情核验失败，原备选已保留，请稍后重试",
            },
        ) from None
    raw = response.get("pois", [])
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise HTTPException(
            502,
            detail={
                "code": "poi_detail_invalid",
                "message": "高德未返回唯一有效的POI详情",
            },
        )
    try:
        normalized = request.app.state.settings.redact(
            normalize_poi(raw[0], now(), "v5")
        )
        poi = Poi.model_validate(normalized)
    except ValueError:
        raise HTTPException(
            502,
            detail={
                "code": "poi_detail_invalid",
                "message": "高德未返回有效的POI详情字段",
            },
        ) from None
    city = context.get("city")

    def normalized_city(value):
        return "".join(
            unicodedata.normalize("NFKC", value or "").casefold().split()
        ).removesuffix("市")

    if (
        poi.id != poi_id
        or poi.location is None
        or not poi.name
        or (city and normalized_city(city) != normalized_city(poi.city))
    ):
        raise HTTPException(
            502,
            detail={
                "code": "poi_detail_invalid",
                "message": "POI详情的标识、坐标或所属城市未通过核验",
            },
        )
    return poi.model_dump(mode="json")


def make_router():
    router = APIRouter(prefix="/api/trips", tags=["places"])

    @router.get("/{trip_id}/places")
    def places(trip_id: str, request: Request, run_id: UUID | None = None):
        return _call(request, "list_places", trip_id, str(run_id) if run_id else None)

    @router.post("/{trip_id}/places/match")
    async def match(trip_id: str, body: MatchPlaces, request: Request):
        session = _call(request, "begin_place_match", trip_id, body)
        amap = (
            _amap(request, max_calls=40, expected_version=body.expected_version)
            if session["items"]
            else None
        )
        matches = {}
        try:
            async with asyncio.timeout(60):
                for item in session["items"]:
                    candidate, mentions = item["candidate"], item["mentions"]
                    city = candidate.get("city") or next(
                        (
                            mention["city"]
                            for mention in mentions
                            if mention.get("city")
                        ),
                        "",
                    )
                    if (
                        candidate["kind"] != "place"
                        or not city
                        or len(candidate["name"]) > 80
                    ):
                        matches[candidate["id"]] = {
                            "status": "unmatched",
                            "poi": None,
                            "options": [],
                            "reason": "缺少可核实的具体地点名称或城市，保留原候选",
                        }
                        continue
                    try:
                        response = await _search(
                            request,
                            amap,
                            candidate["name"],
                            city,
                            body.expected_version,
                        )
                        matches[candidate["id"]] = match_pois(
                            candidate, mentions, response, now()
                        )
                    except AmapBudgetExceeded:
                        break
                    except (AmapError, ValueError):
                        matches[candidate["id"]] = {
                            "status": "unmatched",
                            "poi": None,
                            "options": [],
                            "reason": "高德地点查询失败，保留原候选，请稍后重试",
                        }
        except TimeoutError:
            pass
        result = _call(request, "finish_place_match", trip_id, body, session, matches)
        result["budget"] = {
            "candidate_limit": 20,
            "call_limit": 40,
            "timeout_seconds": 60,
            "calls": len(amap.calls) if amap else 0,
            "processed": len(matches),
            "limited": len(matches) < session["total"],
        }
        return result

    @router.post("/{trip_id}/places/map")
    def map_action(trip_id: str, body: MapAction, request: Request):
        return _call(request, "apply_map_action", trip_id, body)

    @router.get("/{trip_id}/places/manual/search")
    async def search_manual(
        trip_id: str,
        request: Request,
        query: Annotated[str, Query(min_length=1, max_length=80)],
        city: Annotated[str, Query(min_length=1, max_length=200)],
        expected_version: Annotated[int, Query(ge=1)],
    ):
        if not query.strip() or not city.strip():
            raise HTTPException(
                422,
                detail={
                    "code": "invalid_manual_search",
                    "message": "地点名称和城市不能为空白",
                },
            )
        trip = _call(request, "get_trip", trip_id)
        if trip["request_version"] != expected_version or trip["status"] == "archived":
            raise HTTPException(
                409,
                detail={
                    "code": "version_conflict",
                    "message": "行程条件已变更，请重新搜索地点",
                    "current_version": trip["request_version"],
                },
            )
        amap = _amap(request, max_calls=2, expected_version=expected_version)
        try:
            async with asyncio.timeout(30):
                response = await _search(
                    request, amap, query.strip(), city.strip(), expected_version
                )
        except (AmapError, ValueError, TimeoutError):
            raise HTTPException(
                502,
                detail={
                    "code": "manual_search_failed",
                    "message": "地点搜索失败，请稍后重试",
                },
            ) from None
        return _call(
            request,
            "save_manual_search",
            trip_id,
            expected_version,
            query.strip(),
            city.strip(),
            response["pois"],
        )

    @router.post("/{trip_id}/places/manual/confirm")
    async def confirm_manual(trip_id: str, body: ConfirmManualPoi, request: Request):
        if not hasattr(request.app.state, "manual_confirmation_lock"):
            request.app.state.manual_confirmation_lock = asyncio.Lock()
        # shortcut: in-flight retries share this process lock; use shared claims before enabling multiple workers.
        async with request.app.state.manual_confirmation_lock:
            context = _call(request, "manual_confirmation_context", trip_id, body)
            verified = None
            if "candidate_id" not in context:
                verified = await _verify_detail(
                    request, body.poi_id, context, body.expected_version
                )
            return _call(request, "confirm_manual_poi", trip_id, body, verified)

    @router.post("/{trip_id}/places/candidates/{candidate_id}/confirm")
    async def confirm(
        trip_id: str, candidate_id: str, body: ConfirmPoi, request: Request
    ):
        context = _call(
            request, "candidate_confirmation_context", trip_id, candidate_id, body
        )
        verified = await _verify_detail(
            request, body.poi_id, context, body.expected_version
        )
        return _call(
            request, "confirm_candidate_poi", trip_id, candidate_id, body, verified
        )

    @router.get("/{trip_id}/places/origin/search")
    async def search_origin(
        trip_id: str,
        request: Request,
        query: Annotated[str, Query(min_length=1, max_length=80)],
        expected_version: Annotated[int, Query(ge=1)],
        city: Annotated[str, Query(min_length=1, max_length=200)],
    ):
        if not query.strip() or not city.strip():
            raise HTTPException(
                422,
                detail={
                    "code": "invalid_origin_search",
                    "message": "起点关键词和城市不能为空白",
                },
            )
        trip = _call(request, "get_trip", trip_id)
        if trip["request_version"] != expected_version or trip["status"] == "archived":
            raise HTTPException(
                409,
                detail={
                    "code": "version_conflict",
                    "message": "行程条件已变更，请重新搜索起点",
                    "current_version": trip["request_version"],
                },
            )
        amap = _amap(request, max_calls=2, expected_version=expected_version)
        try:
            async with asyncio.timeout(30):
                response = await _search(request, amap, query, city, expected_version)
        except (AmapError, ValueError, TimeoutError):
            raise HTTPException(
                502,
                detail={
                    "code": "origin_search_failed",
                    "message": "起点搜索失败，请稍后重试",
                },
            ) from None
        return _call(
            request, "save_origin_search", trip_id, expected_version, response["pois"]
        )

    @router.post("/{trip_id}/places/origin/confirm")
    async def confirm_start(trip_id: str, body: ConfirmOrigin, request: Request):
        verified = None
        if body.source == "amap":
            context = _call(request, "origin_confirmation_context", trip_id, body)
            verified = await _verify_detail(
                request, body.poi_id, context, body.expected_version
            )
        return _call(request, "confirm_origin", trip_id, body, verified)

    return router
