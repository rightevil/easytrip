"""One resumable discovery pipeline; every external call crosses the ledger gate."""

import asyncio
import json
import sqlite3
from contextlib import suppress
from time import monotonic

from ..config import direct_client
from ..domain.discovery_tasks import DISCOVERY_STAGES, DiscoverySnapshot
from ..integrations.xhs import SourcePaused
from ..services.collection import CollectionService
from ..services.understanding import understand, validate_understanding


class DiscoveryPaused(RuntimeError):
    pass


class DiscoveryStopped(RuntimeError):
    pass


class DiscoveryRunner:
    def __init__(self, database, settings, *, stages=None):
        self.database = database
        self.settings = settings
        self.stages = stages or {}

    async def db(self, method, *args, **kwargs):
        return await asyncio.to_thread(getattr(self.database, method), *args, **kwargs)

    async def model_hooks(self, run, logical_key):
        calls = await self.db("list_actions", run["id"])
        previous = [action for action in calls if action["action_key"].startswith(logical_key + ":model:")]
        for action in previous:
            result = action["result"] or {}
            if action["status"] == "completed" and result.get("model_data") is not None:
                return result["model_data"], None, None
        if previous:
            # Interrupted inputs cannot be replayed as a fresh image request in this round.
            raise DiscoveryPaused("model_retry_requires_new_run")
        attempt = len(previous)

        async def before(info):
            nonlocal attempt
            if attempt >= 2:
                return False
            attempt += 1
            action_id = await self.db("begin_action", run["id"], run["generation"], f"{logical_key}:model:{attempt}", "model", info)
            return action_id if action_id else False

        async def after(action_id, record):
            if action_id:
                status = "unknown" if record.get("result_unknown") else "completed" if record.get("status") == "passed" else "failed"
                await self.db("finish_action", action_id, status=status, result={"model_data": record.get("model_data"), "error": record.get("error")}, usage={key: value for key, value in record.items() if key != "model_data"})

        return None, before, after

    async def _understanding(self, run, snapshot, client):
        cached, before, after = await self.model_hooks(run, "understanding")
        if cached is not None:
            user_text = "\n".join(self.settings.redact(item["content"]) for item in snapshot.messages if item.get("role") == "user")
            return validate_understanding(json.dumps(cached), snapshot.request, user_text).model_dump(mode="json")
        if not all((self.settings.model_base_url, self.settings.model_api_key, self.settings.model_id)):
            raise DiscoveryPaused("model_configuration_missing")
        result = await understand(client, self.settings, snapshot.model_dump(mode="json"), before_request=before, after_request=after)
        if result["status"] != "passed":
            if result.get("error") == "request_not_allowed":
                raise DiscoveryStopped("model_budget_exhausted")
            raise DiscoveryPaused("model_request_failed" if any(item.get("result_unknown") for item in result["requests"]) else "model_output_invalid")
        return result["data"]

    async def stage(self, stage, run, snapshot, client):
        if stage in self.stages:
            return await self.stages[stage](run, snapshot, client)
        if stage == "understanding":
            return await self._understanding(run, snapshot, client)
        if stage == "discovering":
            return await CollectionService(self, run, client).discover(run["checkpoint"]["understanding"]["query_plan"])
        if stage == "fetching":
            return await CollectionService(self, run, client).fetch(run["checkpoint"]["discovering"]["feeds"])
        raise DiscoveryStopped("stage_not_available")

    async def execute(self, run):
        started = None
        generation = run["generation"]
        try:
            snapshot = DiscoverySnapshot.model_validate(run["params"])
            async with direct_client() as client:
                for stage in DISCOVERY_STAGES:
                    run = await self.db("get_run", run["id"])
                    if run["generation"] != generation:
                        return
                    if stage in run["checkpoint"]["completed_stages"]:
                        continue
                    if not await self.db("run_is_current", run["id"], run["generation"]):
                        return
                    remaining = run["budget"]["max_seconds"] - run["usage"]["elapsed_seconds"]
                    if remaining <= 0:
                        raise DiscoveryStopped("time_budget_exhausted")
                    started = monotonic()
                    async with asyncio.timeout(remaining):
                        output = await self.stage(stage, run, snapshot, client)
                        if not await self.db("commit_discovery_stage", run["id"], run["generation"], stage, output, monotonic() - started):
                            return
                    started = None
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await self.db("stop_run", run["id"], run["generation"], "partial", "time_budget_exhausted", elapsed_seconds=monotonic() - started if started else 0)
        except (SourcePaused, DiscoveryPaused) as error:
            reason = "source_login_or_access_required" if isinstance(error, SourcePaused) else str(error)
            await self.db("stop_run", run["id"], run["generation"], "paused", reason, elapsed_seconds=monotonic() - started if started else 0)
        except DiscoveryStopped as error:
            await self.db("stop_run", run["id"], run["generation"], "partial", str(error), elapsed_seconds=monotonic() - started if started else 0)
        except (sqlite3.Error, OSError):
            with suppress(sqlite3.Error, OSError):
                await self.db("stop_run", run["id"], run["generation"], "failed", "storage_unavailable")
        except Exception:
            await self.db("stop_run", run["id"], run["generation"], "failed", "discovery_stage_failed", elapsed_seconds=monotonic() - started if started else 0)
