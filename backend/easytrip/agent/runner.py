"""One bounded local executor; P1 does not call external services."""

import asyncio
import logging
import sqlite3
from contextlib import suppress
from time import monotonic

from ..domain.tasks import (
    STAGES,
    PreparedWorkspace,
    RequestReadiness,
    RunSnapshot,
    WorkspaceSummary,
)

logger = logging.getLogger(__name__)


class RunExecutor:
    def __init__(self, database, *, before_stage=None):
        self.database = database
        self.before_stage = before_stage
        self._wake = asyncio.Event()
        self._worker = None
        self._current = None
        self._run_id = None
        self._generation = None
        self._closing = False

    async def start(self):
        if self._worker is not None:
            raise RuntimeError("执行器已启动")
        await self._db("recover_runs")
        self._closing = False
        self._worker = asyncio.create_task(self._work())

    async def close(self):
        self._closing = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        await self._db("recover_runs", graceful=True)

    async def _db(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.database, method), *args, **kwargs)

    def wake(self):
        self._wake.set()

    def interrupt(self, run_id, generation):
        if (
            self._run_id == run_id
            and self._generation == generation
            and self._current is not None
        ):
            self._current.cancel()
        self.wake()

    async def _work(self):
        while not self._closing:
            self._wake.clear()
            try:
                run = await self._db("claim_run")
            except (sqlite3.Error, OSError):
                logger.warning("run_storage_unavailable")
                run = None
            except Exception:
                logger.warning("run_claim_failed")
                run = None
            if run is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1)
                continue
            self._run_id = run["id"]
            self._generation = run["generation"]
            self._current = asyncio.create_task(self._execute(run))
            try:
                await self._current
            except asyncio.CancelledError:
                if self._closing:
                    break
            except Exception:
                # Failure recording may fail too; keep the worker available for other runs.
                logger.warning("run_execution_failed")
            finally:
                self._current = None
                self._run_id = None
                self._generation = None

    async def _execute(self, run):
        generation = run["generation"]
        stage_started = None
        try:
            snapshot = RunSnapshot.model_validate(run["params"])
            for stage in STAGES:
                if stage in run["checkpoint"]["completed_stages"]:
                    continue
                if not await self._db("run_is_current", run["id"], generation):
                    return
                run = await self._db("get_run", run["id"])
                remaining = (
                    run["budget"]["max_seconds"] - run["usage"]["elapsed_seconds"]
                )
                if run["usage"]["steps"] >= run["budget"]["max_steps"]:
                    await self._db(
                        "stop_run",
                        run["id"],
                        generation,
                        "partial",
                        "step_budget_exhausted",
                    )
                    return
                if remaining <= 0:
                    await self._db(
                        "stop_run",
                        run["id"],
                        generation,
                        "partial",
                        "time_budget_exhausted",
                    )
                    return
                stage_started = monotonic()
                async with asyncio.timeout(remaining):
                    if self.before_stage is not None:
                        await self.before_stage(run, stage)
                    # Check again after awaiting local work: cancellation may have arrived.
                    if not await self._db("run_is_current", run["id"], generation):
                        return
                    output = self._stage(stage, snapshot, run["checkpoint"])
                    if not await self._db(
                        "commit_run_stage",
                        run["id"],
                        generation,
                        stage,
                        output,
                        monotonic() - stage_started,
                    ):
                        return
                stage_started = None
                # Let cancellation and other HTTP requests run between local stages.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self._db(
                "stop_run",
                run["id"],
                generation,
                "partial",
                "time_budget_exhausted",
                elapsed_seconds=monotonic() - stage_started if stage_started else 0,
            )
        except (sqlite3.Error, OSError):
            # A failed database write cannot be represented as a successful checkpoint.
            logger.warning("run_storage_unavailable")
            with suppress(sqlite3.Error, OSError):
                await self._db(
                    "stop_run", run["id"], generation, "failed", "storage_unavailable"
                )
        except Exception:
            await self._db(
                "stop_run",
                run["id"],
                generation,
                "failed",
                "local_stage_failed",
                elapsed_seconds=monotonic() - stage_started if stage_started else 0,
            )

    @staticmethod
    def _stage(stage, snapshot, checkpoint):
        if stage == "validate_request":
            missing = [
                field
                for field in ("origin", "start_at", "duration_minutes")
                if not getattr(snapshot.request, field)
            ]
            return RequestReadiness(
                conditions_complete=not missing,
                missing=missing,
                destination=snapshot.request.destination,
                transport=snapshot.request.transport,
            ).model_dump(mode="json")
        if stage == "read_workspace":
            return WorkspaceSummary(
                messages_count=snapshot.messages_count,
                selected_count=len(snapshot.workspace.selected_ids),
                map_visible_count=len(snapshot.workspace.map_visible_ids),
                workspace_version=snapshot.workspace_version,
                active_tab=snapshot.workspace.active_tab,
            ).model_dump(mode="json")
        return PreparedWorkspace(
            conditions=checkpoint["validate_request"],
            workspace=checkpoint["read_workspace"],
            summary="已检查行程条件并汇总已保存内容",
            not_connected=["攻略发现", "地点地图", "时间排程"],
        ).model_dump(mode="json")
