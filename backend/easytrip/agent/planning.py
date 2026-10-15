"""Bounded planning on the existing executor, ledger and generation guards."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

from ..domain.planning import PLAN_STAGES, PlanPayload, SavePlan
from ..integrations.amap import Amap, AmapBudgetExceeded, AmapError
from ..services.day_planning import schedule
from ..services.lodgings import supplement_lodgings
from ..services.meals import supplement_meals
from ..services.routes import RouteProvider
from .discovery import DiscoveryRunner


class PlanningStopped(RuntimeError):
    pass


class PlanningRoutes(RouteProvider):
    def __init__(self, amap, database, is_current):
        super().__init__(amap, database)
        self.is_current = is_current
        self.cache_hits = 0

    async def route(self, **kwargs):
        if not await self.is_current():
            raise PlanningStopped()
        calls = len(self.amap.calls)
        result = await super().route(**kwargs)
        self.cache_hits += int(len(self.amap.calls) == calls)
        return result


class PlanningRunner(DiscoveryRunner):
    def __init__(self, database, settings, *, client=None, before_request=None):
        self.database = database
        self.settings = settings
        self.client = client
        self.before_request = before_request

    async def execute(self, run):
        generation = run["generation"]
        stage_start = monotonic()
        action_id = None

        async def before():
            nonlocal action_id
            if self.before_request is not None:
                await self.before_request()
            if not await self.db("run_is_current", run["id"], generation):
                raise PlanningStopped()
            current = await self.db("get_run", run["id"])
            action_id = await self.db(
                "begin_action",
                run["id"],
                generation,
                f"amap:{current['usage'].get('amap_calls', 0)}",
                "detail",
                {"service": "amap"},
            )
            if action_id is None:
                raise AmapBudgetExceeded("规划调用额度已用尽或有未知请求")

        async def after(path, payload, error):
            nonlocal action_id
            if action_id is not None:
                status = (
                    "completed"
                    if error is None
                    else "failed"
                    if isinstance(error, AmapError) and error.infocode != "UNKNOWN"
                    else "unknown"
                )
                await self.db(
                    "finish_action",
                    action_id,
                    status=status,
                    result={"path": path, "response_received": payload is not None},
                    usage={
                        "amap_kind": "city"
                        if path == "v3/config/district"
                        else "poi"
                        if path.startswith("v5/place/")
                        else "route"
                    },
                )
                action_id = None

        try:
            remaining = run["budget"]["max_seconds"] - run["usage"]["elapsed_seconds"]
            if remaining <= 0:
                await self.db(
                    "stop_run",
                    run["id"],
                    generation,
                    "partial",
                    "time_budget_exhausted",
                )
                return
            expires_at = monotonic() + remaining
            async with asyncio.timeout(remaining):
                for stage in PLAN_STAGES:
                    current = await self.db("get_run", run["id"])
                    if stage in current["checkpoint"]["completed_stages"]:
                        continue
                    if not await self.db("run_is_current", run["id"], generation):
                        return
                    stage_start = monotonic()
                    if stage == "planning_input":
                        output = run["params"]
                    elif stage == "routing":
                        remaining_calls = run["budget"]["max_routes"] - current[
                            "usage"
                        ].get("amap_calls", 0)
                        amap = Amap(
                            self.client,
                            self.settings.amap_web_key,
                            max_calls=max(1, remaining_calls),
                            before_request=before,
                            after_request=after,
                        )

                        async def is_current():
                            return await self.db(
                                "run_is_current", run["id"], generation
                            )

                        provider = PlanningRoutes(amap, self.database, is_current)
                        parent = (
                            await self.db(
                                "get_plan",
                                run["trip_id"],
                                run["params"]["parent_plan_id"],
                            )
                            if run["params"].get("parent_plan_id")
                            else None
                        )
                        initial = await self.db(
                            "get_run_item", run["id"], "planning:initial_routes"
                        )
                        if initial is None:
                            output = await schedule(
                                run["params"],
                                provider,
                                max_combinations=run["budget"]["max_combinations"],
                                parent_plan=parent,
                            )
                            initial = {
                                "suggestions": output,
                                "cache_hits": provider.cache_hits,
                            }
                            if not await self.db(
                                "save_run_item",
                                run["id"],
                                generation,
                                "planning:initial_routes",
                                initial,
                            ):
                                return
                        else:
                            output = initial["suggestions"]
                            provider.cache_hits = initial["cache_hits"]
                        lodgings = {"candidates": [], "gaps": [], "queries": []}
                        if run["params"]["request"]["planning"].get("day_windows"):
                            lodging_seconds = expires_at - monotonic() - 10
                            if lodging_seconds > 0:
                                try:
                                    async with asyncio.timeout(lodging_seconds):
                                        lodgings = await supplement_lodgings(
                                            self,
                                            run,
                                            self.client,
                                            run["params"],
                                            output,
                                            amap,
                                        )
                                except TimeoutError:
                                    lodgings["gaps"].append(
                                        {
                                            "code": "lodging_lookup_timeout",
                                            "message": "住宿补查达到时间预算，逐晚缺口保留",
                                        }
                                    )
                            else:
                                lodgings["gaps"].append(
                                    {
                                        "code": "lodging_lookup_budget",
                                        "message": "剩余时间不足，住宿缺口保留",
                                    }
                                )
                        meals = {"candidates": [], "gaps": [], "queries": []}
                        # Reserve time to retain already obtained routes if meal lookup times out.
                        meal_seconds = expires_at - monotonic() - 10
                        if meal_seconds > 0:
                            try:
                                async with asyncio.timeout(meal_seconds):
                                    meals = await supplement_meals(
                                        self,
                                        run,
                                        self.client,
                                        run["params"],
                                        output,
                                        amap,
                                    )
                                    if meals["candidates"]:
                                        updated = await schedule(
                                            run["params"],
                                            provider,
                                            max_combinations=run["budget"][
                                                "max_combinations"
                                            ],
                                            parent_plan=parent,
                                            supplemental_candidates=meals["candidates"],
                                        )
                                        meal_ids = {
                                            item["candidate_id"]
                                            for item in meals["candidates"]
                                        }
                                        if any(
                                            suggestion["status"]
                                            in {"validated", "conditional"}
                                            and any(
                                                item.get("candidate_id") in meal_ids
                                                and item.get("start_at")
                                                and item.get("end_at")
                                                for item in suggestion["payload"][
                                                    "itinerary"
                                                ]
                                                if item.get("kind") == "activity"
                                            )
                                            for suggestion in updated
                                        ):
                                            output = updated
                                        else:
                                            output = deepcopy(initial["suggestions"])
                                            meals["gaps"].append(
                                                {
                                                    "code": "meal_route_uncomputed",
                                                    "message": "补餐候选未通过路线或时间窗校验，保留原安排和餐次缺口",
                                                }
                                            )
                            except TimeoutError:
                                meals["gaps"].append(
                                    {
                                        "code": "meal_lookup_timeout",
                                        "message": "餐饮补查达到时间预算，保留已有路线与餐次缺口",
                                    }
                                )
                        else:
                            meals["gaps"].append(
                                {
                                    "code": "meal_lookup_budget",
                                    "message": "剩余时间不足以补查餐饮，餐次缺口保持可见",
                                }
                            )
                        for suggestion in output:
                            inherited_meals = suggestion["payload"].get(
                                "supplements", []
                            )
                            suggestion["payload"]["supplements"] = list(
                                {
                                    (
                                        item["candidate_id"],
                                        item.get("lodging_window", {}).get("date"),
                                    ): item
                                    for item in [
                                        *inherited_meals,
                                        *meals["candidates"],
                                        *lodgings["candidates"],
                                    ]
                                }.values()
                            )
                            suggestion["payload"]["unknowns"].extend(meals["gaps"])
                            suggestion["payload"]["unknowns"].extend(lodgings["gaps"])
                            for day in suggestion["payload"].get("days", []):
                                day["unknowns"].extend(
                                    gap
                                    for gap in lodgings["gaps"]
                                    if gap.get("date") == day["date"]
                                )
                            suggestion["payload"]["usage"]["lodging_queries"] = (
                                lodgings["queries"]
                            )
                            suggestion["payload"]["usage"]["meal_queries"] = meals[
                                "queries"
                            ]
                            if meals["gaps"] and suggestion["status"] == "validated":
                                suggestion["status"] = "conditional"
                        frozen_usage = (await self.db("get_run", run["id"]))["usage"]
                        frozen_usage["cache_hits"] = provider.cache_hits
                        for suggestion in output:
                            suggestion["payload"]["usage"].update(frozen_usage)
                    else:
                        plans = []
                        suggestions = current["checkpoint"]["routing"]
                        for index, suggestion in enumerate(suggestions):
                            if not await self.db(
                                "run_is_current", run["id"], generation
                            ):
                                return
                            payload = PlanPayload.model_validate(suggestion["payload"])
                            plan = await self.db(
                                "save_plan",
                                run["trip_id"],
                                SavePlan(
                                    operation_id=uuid5(
                                        NAMESPACE_URL, f"{run['id']}:plan:{index}"
                                    ),
                                    request_version=run["request_version"],
                                    input_hash=run["params"]["input_hash"],
                                    parent_plan_id=run["params"].get("parent_plan_id"),
                                    run_id=run["id"],
                                    status=suggestion["status"],
                                    payload=payload,
                                ),
                            )
                            plans.append(plan)
                        output = {
                            "plan_ids": [plan["id"] for plan in plans],
                            "input_hash": run["params"]["input_hash"],
                            "summary": f"已保存 {len(plans)} 个有限{'多日' if run['params']['request']['planning'].get('day_windows') else '单日'}建议；采用前仍须核对未知事项",
                            "partial": not plans
                            or all(
                                plan["status"] in {"draft", "partial", "conflict"}
                                for plan in plans
                            ),
                        }
                    if not await self.db(
                        "commit_planning_stage",
                        run["id"],
                        generation,
                        stage,
                        output,
                        monotonic() - stage_start,
                    ):
                        return
        except asyncio.CancelledError:
            if action_id is not None:
                with suppress(Exception):
                    await self.db("finish_action", action_id, status="unknown")
            raise
        except PlanningStopped:
            return
        except TimeoutError:
            if action_id is not None:
                await self.db("finish_action", action_id, status="unknown")
            await self.db(
                "stop_run",
                run["id"],
                generation,
                "partial",
                "time_budget_exhausted",
                elapsed_seconds=monotonic() - stage_start,
            )
        except Exception:
            if action_id is not None:
                with suppress(Exception):
                    await self.db("finish_action", action_id, status="unknown")
            await self.db(
                "stop_run",
                run["id"],
                generation,
                "failed",
                "planning_failed",
                elapsed_seconds=monotonic() - stage_start,
            )
