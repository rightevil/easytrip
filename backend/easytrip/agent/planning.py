"""Bounded planning on the existing executor, ledger and generation guards."""

import asyncio
from contextlib import suppress
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

from ..domain.planning import PLAN_STAGES, PlanPayload, SavePlan
from ..integrations.amap import Amap, AmapBudgetExceeded, AmapError
from ..services.planning import schedule
from ..services.routes import RouteProvider


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


class PlanningRunner:
    def __init__(self, database, settings, *, client=None, before_request=None):
        self.database = database
        self.settings = settings
        self.client = client
        self.before_request = before_request

    async def _db(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.database, method), *args, **kwargs)

    async def execute(self, run):
        generation = run["generation"]
        stage_start = monotonic()
        action_id = None

        async def before():
            nonlocal action_id
            if self.before_request is not None:
                await self.before_request()
            if not await self._db("run_is_current", run["id"], generation):
                raise PlanningStopped()
            current = await self._db("get_run", run["id"])
            action_id = await self._db(
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
                await self._db(
                    "finish_action",
                    action_id,
                    status=status,
                    result={"path": path, "response_received": payload is not None},
                    usage={
                        "amap_kind": "city" if path == "v3/config/district" else "route"
                    },
                )
                action_id = None

        try:
            remaining = run["budget"]["max_seconds"] - run["usage"]["elapsed_seconds"]
            if remaining <= 0:
                await self._db(
                    "stop_run",
                    run["id"],
                    generation,
                    "partial",
                    "time_budget_exhausted",
                )
                return
            async with asyncio.timeout(remaining):
                for stage in PLAN_STAGES:
                    current = await self._db("get_run", run["id"])
                    if stage in current["checkpoint"]["completed_stages"]:
                        continue
                    if not await self._db("run_is_current", run["id"], generation):
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
                            return await self._db(
                                "run_is_current", run["id"], generation
                            )

                        provider = PlanningRoutes(amap, self.database, is_current)
                        parent = (
                            await self._db(
                                "get_plan",
                                run["trip_id"],
                                run["params"]["parent_plan_id"],
                            )
                            if run["params"].get("parent_plan_id")
                            else None
                        )
                        output = await schedule(
                            run["params"],
                            provider,
                            max_combinations=run["budget"]["max_combinations"],
                            parent_plan=parent,
                        )
                        frozen_usage = (await self._db("get_run", run["id"]))["usage"]
                        frozen_usage["cache_hits"] = provider.cache_hits
                        for suggestion in output:
                            suggestion["payload"]["usage"].update(frozen_usage)
                    else:
                        plans = []
                        suggestions = current["checkpoint"]["routing"]
                        for index, suggestion in enumerate(suggestions):
                            if not await self._db(
                                "run_is_current", run["id"], generation
                            ):
                                return
                            payload = PlanPayload.model_validate(suggestion["payload"])
                            plan = await self._db(
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
                            "summary": f"已保存 {len(plans)} 个有限单日建议；采用前仍须核对未知事项",
                            "partial": not plans
                            or all(
                                plan["status"] in {"draft", "partial", "conflict"}
                                for plan in plans
                            ),
                        }
                    if not await self._db(
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
                    await self._db("finish_action", action_id, status="unknown")
            raise
        except PlanningStopped:
            return
        except TimeoutError:
            if action_id is not None:
                await self._db("finish_action", action_id, status="unknown")
            await self._db(
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
                    await self._db("finish_action", action_id, status="unknown")
            await self._db(
                "stop_run",
                run["id"],
                generation,
                "failed",
                "planning_failed",
                elapsed_seconds=monotonic() - stage_start,
            )
