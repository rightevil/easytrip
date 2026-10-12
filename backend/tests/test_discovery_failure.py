import asyncio
import sqlite3
from uuid import uuid4

import pytest

from easytrip.agent.discovery import DiscoveryPaused, DiscoveryRunner, DiscoveryStopped
from easytrip.config import Settings
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


@pytest.mark.parametrize("error,status,reason", [
    (DiscoveryPaused("model_result_unknown"), "paused", "model_result_unknown"),
    (DiscoveryStopped("model_budget_exhausted"), "partial", "model_budget_exhausted"),
    (TimeoutError(), "partial", "time_budget_exhausted"),
])
def test_summary_storage_failure_does_not_leave_task_running(tmp_path, monkeypatch, error, status, reason):
    async def check():
        database = Database(tmp_path / "failure.sqlite3")
        database.initialize()
        trip = database.create_trip(CreateTrip(operation_id=uuid4()))
        database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
        run = database.claim_run()

        async def stage(*args):
            raise error

        async def summary(*args, **kwargs):
            raise sqlite3.OperationalError("unavailable result storage")

        monkeypatch.setattr("easytrip.agent.discovery.publish", summary)
        runner = DiscoveryRunner(database, Settings(), stages={"understanding": stage})
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["status"] == status and final["stop_reason"] == reason
        assert final["checkpoint"]["completed_stages"] == []
    asyncio.run(check())
