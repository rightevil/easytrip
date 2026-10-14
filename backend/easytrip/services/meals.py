"""Two source-backed meal lookups tied to actual schedule windows and POI areas."""

import math
import re
import sqlite3
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from ..agent.content import analysis_progress, note_hash, persist_extraction
from ..agent.discovery import DiscoveryPaused, DiscoveryStopped
from ..domain.discovery import CandidateDraft
from ..domain.models import TripRequest
from ..domain.places import Poi
from ..integrations.amap import AmapBudgetExceeded
from .collection import CollectionService
from .evidence import build_candidates, extract_note
from .places import match_pois, normalize_poi


def _time(value):
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("餐次时间必须带时区")
    return result


def meal_queries(frozen, suggestions):
    request = TripRequest.model_validate(frozen["request"])
    if request.start_at is None:
        return []
    zone = ZoneInfo(request.planning.timezone)
    date = request.start_at.astimezone(zone).date()
    deadline = request.planning.end_at
    if request.duration_minutes is not None:
        duration_end = request.start_at + timedelta(minutes=request.duration_minutes)
        deadline = min(deadline, duration_end) if deadline else duration_end
    if deadline is None:
        return []
    selected = {item["candidate_id"]: item for item in frozen["selected"]}
    result, seen = [], set()
    for suggestion in suggestions:
        payload = suggestion["payload"]
        for gap in payload.get("unknowns", []):
            period = gap.get("period")
            if (
                gap.get("code") != "meal_gap"
                or period not in ("lunch", "dinner")
                or period in seen
            ):
                continue
            opening, closing = (11, 14) if period == "lunch" else (17, 20)
            start = max(
                request.start_at,
                _time(gap["window_start"])
                if gap.get("window_start")
                else datetime.combine(date, time(opening), zone),
                request.planning.progress_at or request.start_at,
            )
            end = min(
                deadline,
                _time(gap["window_end"])
                if gap.get("window_end")
                else datetime.combine(date, time(closing), zone),
            )
            if start >= end:
                continue
            located = [
                item
                for item in payload.get("itinerary", [])
                if item.get("kind") == "activity"
                and item.get("state") != "skipped"
                and item.get("start_at")
                and item.get("candidate_id") in selected
                and _time(item["start_at"]) < end
            ]
            if not located:
                continue
            before = [item for item in located if _time(item["start_at"]) <= start]
            activity = (
                max(before, key=lambda item: _time(item["start_at"]))
                if before
                else min(located, key=lambda item: _time(item["start_at"]))
            )
            anchor = selected[activity["candidate_id"]]
            poi = Poi.model_validate(anchor["poi"])
            if not poi.id or not poi.name or poi.location is None or not poi.city:
                continue
            meal = "午餐" if period == "lunch" else "晚餐"
            query = f"{poi.city} {poi.name} 附近 {meal} {start.astimezone(zone):%H:%M}-{end.astimezone(zone):%H:%M} 餐馆"
            if len(query) > 120:
                continue
            seen.add(period)
            result.append(
                {
                    "query": query,
                    "purpose": "meal_gap",
                    "period": period,
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "anchor_candidate_id": anchor["candidate_id"],
                    "anchor_name": poi.name,
                    "anchor_location": poi.location,
                    "city": poi.city,
                }
            )
            if len(result) == 2:
                return result
    return result


def _nearby(left, right):
    # Geographic area filtering only; the scheduler must still query the real road route.
    lng1, lat1, lng2, lat2 = map(math.radians, [*left, *right])
    value = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(min(1, value))) <= 3000


def _gap(context, reason, message):
    return {
        "code": "meal_supplement_unfilled",
        "reason": reason,
        "message": message,
        "period": context["period"],
        "meal_window": {
            key: context[key]
            for key in ("period", "start_at", "end_at", "anchor_candidate_id")
        },
    }


async def supplement_meals(runner, run, client, frozen, suggestions, amap):
    """Return separate supplements; never mutate user selection or its input hash."""
    contexts = meal_queries(frozen, suggestions)
    result = {"queries": contexts, "candidates": [], "gaps": []}
    if not contexts:
        return result
    cached = await runner.db("get_run_item", run["id"], "meals:result")
    if cached is not None:
        await analysis_progress(runner, run)
        return cached
    if not all(
        (
            runner.settings.xhs_mcp,
            runner.settings.model_base_url,
            runner.settings.model_api_key,
            runner.settings.model_id,
        )
    ):
        result["gaps"] = [
            _gap(context, "configuration_missing", "来源或模型未配置，餐饮缺口保持可见")
            for context in contexts
        ]
        return result
    bounded = deepcopy(run)
    bounded["budget"].update(
        target_notes=min(2, run["budget"]["target_notes"]),
        max_searches=min(2, run["budget"]["max_searches"]),
        max_model_calls=min(4, run["budget"]["max_model_calls"]),
        max_images=0,
        include_replies=False,
        reply_limit=0,
        reserved_gap_searches=0,
    )
    service = CollectionService(runner, bounded, client)
    used_pois = {item["poi"]["id"] for item in frozen["selected"]}
    for context in contexts:
        try:
            feeds = await runner.db("get_run_item", run["id"], "collection:feeds", [])
            current = await runner.db("get_run", run["id"])
            matching_feeds = [
                feed
                for feed in feeds
                if any(
                    link.get("query") == context["query"]
                    for link in feed.get("queries", [feed])
                )
            ]
            if not matching_feeds:
                if (
                    current["usage"].get("searches", 0)
                    >= bounded["budget"]["max_searches"]
                    or len(feeds) >= bounded["budget"]["target_notes"]
                ):
                    raise DiscoveryStopped("meal_source_budget_exhausted")
                discovered = await service.discover(
                    [context],
                    gap=True,
                    note_target=min(len(feeds) + 1, bounded["budget"]["target_notes"]),
                )
                matching_feeds = [
                    feed
                    for feed in discovered["feeds"]
                    if any(
                        link.get("query") == context["query"]
                        for link in feed.get("queries", [feed])
                    )
                ]
            if not matching_feeds:
                result["gaps"].append(
                    _gap(context, "no_notes", "有限搜索未取得对应区域笔记")
                )
                continue
            feed = matching_feeds[0]
            record = await runner.db(
                "get_run_item", run["id"], "note:" + feed["external_id"]
            )
            if record is None:
                actions = await runner.db("list_actions", run["id"])
                if (
                    sum(
                        action["kind"] == "detail"
                        and action["params"].get("service") != "amap"
                        for action in actions
                    )
                    >= 2
                ):
                    raise DiscoveryStopped("meal_source_budget_exhausted")
                current = await runner.db("get_run", run["id"])
                service.budget = {
                    **bounded["budget"],
                    "comment_limit": max(
                        0,
                        min(
                            run["budget"]["comment_limit"],
                            5 - current["usage"].get("comments_selected", 0),
                        ),
                    ),
                }
                await service.fetch(
                    [feed], note_target=bounded["budget"]["target_notes"]
                )
                record = await runner.db(
                    "get_run_item", run["id"], "note:" + feed["external_id"]
                )
            if not record or record.get("status") != "ready":
                result["gaps"].append(
                    _gap(context, "note_unreadable", "笔记详情未成功取得，未生成餐厅")
                )
                continue
            note = record["note"]
            logical = "meals:extract:" + note["source_id"]
            extraction = await runner.db("get_run_item", run["id"], logical)
            if extraction is None:
                cached_data, before, after = await runner.model_hooks(run, logical)
                if cached_data is not None:
                    extraction = {
                        "status": "passed",
                        "data": cached_data,
                        "submitted_images": [],
                    }
                else:

                    async def bounded_before(info):
                        current = await runner.db("get_run", run["id"])
                        if (
                            current["usage"].get("model_calls", 0)
                            >= bounded["budget"]["max_model_calls"]
                        ):
                            return False
                        return await before(
                            {
                                **info,
                                "input_hash": note_hash(note),
                                "model_id": runner.settings.model_id,
                            }
                        )

                    extraction = await extract_note(
                        client,
                        runner.settings,
                        note,
                        max_images=0,
                        comment_limit=min(5, len(note["comments"])),
                        reply_limit=0,
                        before_request=bounded_before,
                        after_request=after,
                    )
                if any(
                    request.get("result_unknown")
                    for request in extraction.get("requests", [])
                ):
                    raise DiscoveryPaused("meal_model_result_unknown")
                extraction = {
                    key: value
                    for key, value in extraction.items()
                    if key not in ("requests", "usage")
                }
                if not await runner.db(
                    "save_run_item", run["id"], run["generation"], logical, extraction
                ):
                    raise DiscoveryStopped("run_changed")
            if extraction.get("status") != "passed":
                result["gaps"].append(
                    _gap(
                        context,
                        "model_output_invalid",
                        "模型未返回通过来源校验的餐饮抽取",
                    )
                )
                continue
            if await persist_extraction(runner, run, extraction) is None:
                raise DiscoveryStopped("run_changed")
            await analysis_progress(runner, run)
            sources = await runner.db(
                "list_run_sources", run["trip_id"], run["id"], include_private=True
            )
            drafts = [
                item
                for item in build_candidates([extraction], sources)
                if item["category"] == "eat" and item["kind"] == "place"
            ]
            found = False
            for draft in drafts[:2]:
                candidate = CandidateDraft.model_validate(
                    {
                        **draft,
                        "id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"p4-meal:{run['id']}:{draft['name']}:{','.join(draft['mention_ids'])}",
                            )
                        ),
                    }
                ).model_dump(mode="json")
                data = extraction["data"]
                mentions = [
                    item
                    for item in data["mentions"]
                    if item["id"] in candidate["mention_ids"]
                ]
                raw = await amap.search_candidates(candidate["name"], context["city"])
                timestamp = datetime.now(timezone.utc).isoformat()
                match = match_pois(candidate, mentions, raw, timestamp)
                if match["status"] != "unique":
                    continue
                detail = await amap.detail(match["poi"]["id"])
                raw_pois = detail.get("pois")
                if (
                    not isinstance(raw_pois, list)
                    or len(raw_pois) != 1
                    or not isinstance(raw_pois[0], dict)
                ):
                    continue
                verified = match_pois(
                    candidate,
                    mentions,
                    {
                        "status": "1",
                        "pois": [
                            normalize_poi(
                                raw_pois[0], datetime.now(timezone.utc).isoformat()
                            )
                        ],
                        "search_complete": True,
                    },
                    timestamp,
                )
                if (
                    verified["status"] != "unique"
                    or verified["poi"]["id"] != match["poi"]["id"]
                ):
                    continue
                poi = Poi.model_validate(
                    runner.settings.redact(verified["poi"])
                ).model_dump(mode="json")
                if poi["id"] in used_pois or not _nearby(
                    context["anchor_location"], poi["location"]
                ):
                    continue
                if (
                    await runner.db(
                        "save_discovery_batch",
                        run["trip_id"],
                        run["id"],
                        run["generation"],
                        candidates=[candidate],
                    )
                    is None
                ):
                    raise DiscoveryStopped("run_changed")
                evidence = [
                    item
                    for item in data["evidence"]
                    if item["id"] in candidate["evidence_ids"]
                ]
                activities = [
                    item
                    for item in data["activities"]
                    if item["mention_id"] in candidate["mention_ids"]
                ]
                source_ids = {
                    item["source_id"] for item in [*mentions, *activities, *evidence]
                }
                result["candidates"].append(
                    {
                        "candidate_id": candidate["id"],
                        "name": candidate["name"],
                        "category": "eat",
                        "kind": "place",
                        "city": poi["city"],
                        "run_id": run["id"],
                        "poi": poi,
                        "mentions": mentions,
                        "activities": activities,
                        "evidence": evidence,
                        "sources": [
                            {
                                key: value
                                for key, value in source.items()
                                if key
                                not in {"author_key", "first_read_at", "last_read_at"}
                            }
                            for source in sources
                            if source["id"] in source_ids
                        ],
                        "meal_window": {
                            key: context[key]
                            for key in (
                                "period",
                                "start_at",
                                "end_at",
                                "anchor_candidate_id",
                            )
                        },
                        "constraint": {
                            "candidate_id": candidate["id"],
                            "duration_minutes": 45,
                            "duration_source": "estimated",
                            "cost_cny": None,
                            "cost_source": "unknown",
                            "not_before": context["start_at"],
                            "not_after": context["end_at"],
                        },
                    }
                )
                used_pois.add(poi["id"])
                found = True
                break
            if not found:
                result["gaps"].append(
                    _gap(
                        context,
                        "poi_not_confirmed",
                        "未取得区域内唯一可核对餐饮POI；歧义分店未自动采用",
                    )
                )
        except sqlite3.Error:
            raise
        except Exception as error:
            reason = (
                "budget_exhausted"
                if isinstance(error, (DiscoveryStopped, AmapBudgetExceeded))
                else "source_or_model_unavailable"
            )
            result["gaps"].append(
                _gap(context, reason, "有限补查未完成，原餐次缺口保持可见")
            )
            for pending in contexts[contexts.index(context) + 1 :]:
                result["gaps"].append(
                    _gap(
                        pending,
                        "supplement_stopped",
                        "前一餐次补查停止，未重复外部请求",
                    )
                )
            break
    if frozen["request"].get("planning", {}).get("dietary_restrictions") or re.search(
        r"过敏|忌口|素食|无麸质|不耐受|不吃|不能吃",
        frozen["request"].get("preferences", ""),
    ):
        result["gaps"].append(
            {
                "code": "dietary_unverified",
                "message": "笔记、评论和模型不能证明过敏或忌口安全，需向餐厅核实",
            }
        )
    await runner.db(
        "save_run_item", run["id"], run["generation"], "meals:result", result
    )
    return result
