import asyncio
from uuid import uuid4

import pytest

from easytrip.agent.discovery import DiscoveryPaused, DiscoveryRunner
from easytrip.config import Settings
from easytrip.domain.discovery_tasks import DiscoverySnapshot
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


@pytest.fixture
def current(tmp_path):
    database = Database(tmp_path / "recovery.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), kind="discover", expected_version=1))
    run = database.claim_run()
    return database, run


def intent():
    unknown = {"value": None, "provenance": "unknown"}
    return {
        "intent": {
            **{key: unknown for key in ("destination", "origin", "start_at", "duration_minutes", "budget_cny", "preferences")},
            "transport": {"value": "mixed", "provenance": "default"},
            "participants": {"value": 1, "provenance": "default"},
            "destination_mode": "explore", "themes": ["山水"], "exclusions": [], "must_visit": [],
            "missing_for_scheduling": ["origin", "start_at", "duration_minutes"],
        },
        "query_plan": [{"query": "周末山水", "purpose": "discovery"}],
        "reserved_gap_searches": 2,
    }


def test_ledger_success_before_checkpoint_is_reused_without_request(current):
    async def check():
        database, run = current
        runner = DiscoveryRunner(database, Settings())
        _, before, after = await runner.model_hooks(run, "understanding")
        action = await before({"images_submitted": 0, "max_tokens": 100})
        await after(action, {"status": "passed", "model_data": intent(), "usage_complete": True,
                             "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
        database.recover_runs()
        recovered = database.get_run(run["id"])
        database.resume_run(run["id"], run["trip_id"], recovered["generation"])
        resumed = database.claim_run()
        data = await runner._understanding(resumed, DiscoverySnapshot.model_validate(resumed["params"]), None)
        assert data["intent"]["destination"]["provenance"] == "unknown"
        assert len(database.list_actions(run["id"])) == 1
        assert database.get_run(run["id"])["usage"]["model_tokens"] == 30
    asyncio.run(check())


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_unverified_image_request_is_not_resent_on_resume(current, status):
    async def check():
        database, run = current
        runner = DiscoveryRunner(database, Settings())
        _, before, after = await runner.model_hooks(run, "extract:n1")
        action = await before({"images_submitted": 2, "max_tokens": 100})
        await after(action, {"status": "failed", "result_unknown": status == "unknown", "usage_complete": False})
        cancelled = database.cancel_run(run["id"], run["trip_id"], run["generation"])
        database.resume_run(run["id"], run["trip_id"], cancelled["generation"])
        resumed = database.claim_run()
        with pytest.raises(DiscoveryPaused, match="model_retry_requires_new_run"):
            await runner.model_hooks(resumed, "extract:n1")
        assert len(database.list_actions(run["id"])) == 1
        assert database.get_run(run["id"])["usage"]["images"] == 2
    asyncio.run(check())
