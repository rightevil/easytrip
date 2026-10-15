"""Read frozen planning inputs and stored versions; never accept client route facts."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from ..domain.planning import AdoptPlan, ReplacePlanningCandidate, SearchRailTimetable
from ..integrations.rail import RailError, search_rail
from .trips import _call


def make_router():
    router = APIRouter(prefix="/api/trips", tags=["planning"])

    @router.get("/{trip_id}/planning-input")
    def inputs(trip_id: str, request: Request):
        return _call(request, "planning_input", trip_id)

    @router.post("/{trip_id}/rail/search")
    async def rail_search(trip_id: str, body: SearchRailTimetable, request: Request):
        snapshot = _call(request, "rail_search_input", trip_id, body)
        settings = request.app.state.settings
        http = getattr(request.app.state, "http", None)
        if not settings.rail_mcp or http is None:
            raise HTTPException(
                503,
                detail={
                    "code": "rail_unavailable",
                    "message": "12306 MCP 未配置或暂不可用",
                },
            )

        async def before_call(name):
            return _call(
                request,
                "begin_api_action",
                trip_id,
                snapshot["request_version"],
                "rail",
                name,
            )

        async def after_call(action_id, **usage):
            if not _call(request, "finish_api_action", action_id, **usage):
                raise HTTPException(
                    503,
                    detail={
                        "code": "usage_unavailable",
                        "message": "用量记录未能保存，请稍后重试",
                    },
                )

        try:
            result = await search_rail(
                settings.rail_mcp,
                http,
                date=body.date,
                departure_name=snapshot["departure_poi"]["name"],
                arrival_name=snapshot["arrival_poi"]["name"],
                before_call=before_call,
                after_call=after_call,
            )
        except RailError as error:
            raise HTTPException(
                502, detail={"code": "rail_query_failed", "message": str(error)}
            ) from None
        if _call(request, "rail_search_input", trip_id, body) != snapshot:
            raise HTTPException(
                409,
                detail={
                    "code": "rail_query_stale",
                    "message": "车站已改变，请重新查询班次",
                },
            )
        return {
            **result,
            **{
                key: value
                for key, value in snapshot.items()
                if key not in {"departure_poi", "arrival_poi"}
            },
        }

    @router.get("/{trip_id}/planning-replacements")
    def replacements(trip_id: str, request: Request):
        return _call(request, "planning_replacements", trip_id)

    @router.post("/{trip_id}/planning-replace")
    def replace(trip_id: str, body: ReplacePlanningCandidate, request: Request):
        return _call(request, "replace_planning_candidate", trip_id, body)

    @router.get("/{trip_id}/plans")
    def plans(trip_id: str, request: Request):
        return _call(request, "list_plans", trip_id)

    @router.get("/{trip_id}/plans/{plan_id}")
    def plan(trip_id: str, plan_id: UUID, request: Request):
        return _call(request, "get_plan", trip_id, str(plan_id))

    @router.post("/{trip_id}/plans/{plan_id}/adopt")
    def adopt(trip_id: str, plan_id: UUID, body: AdoptPlan, request: Request):
        return _call(request, "adopt_plan", trip_id, str(plan_id), body)

    return router
