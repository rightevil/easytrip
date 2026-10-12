import asyncio
import sqlite3
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from easytrip.agent.discovery import DiscoveryPaused, DiscoveryRunner
from easytrip.agent.runner import RunExecutor
from easytrip.api.discovery import make_router as discovery_router
from easytrip.api.runs import make_router as runs_router
from easytrip.api.trips import make_router as trips_router
from easytrip.config import Settings
from easytrip.domain.discovery import CandidateDraft, PlaceMention, SourceNote
from easytrip.domain.discovery_tasks import (
    DISCOVERY_STAGES,
    AdoptDirection,
    DiscoveryMessageCreate,
)
from easytrip.domain.models import CreateTrip, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database, NotFound, OperationConflict


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "discovery.sqlite3")
    database.initialize()
    return database


def create(database):
    return database.create_trip(CreateTrip(operation_id=uuid4()))


def enqueue(database, trip, **budget):
    return database.enqueue_run(
        trip["id"],
        CreateRun(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            kind="discover",
            budget=budget,
        ),
    )


async def wait_status(database, run_id, *statuses):
    async with asyncio.timeout(5):
        while True:
            run = await asyncio.to_thread(database.get_run, run_id)
            if run["status"] in statuses:
                return run
            await asyncio.sleep(0.005)


@pytest.mark.parametrize(
    "budget",
    [
        {"max_searches": 0},
        {"target_notes": True},
        {"include_replies": "yes"},
        {"max_images": -1},
        {"max_seconds": 1801},
        {"max_model_calls": 129},
        {"max_searches": 2, "reserved_gap_searches": 2},
    ],
)
def test_discovery_budgets_are_finite_and_typed(budget):
    with pytest.raises(ValidationError):
        CreateRun(
            operation_id=uuid4(), expected_version=1, kind="discover", budget=budget
        )


def test_message_and_discovery_start_are_atomic_and_idempotent(database):
    trip = create(database)
    body = DiscoveryMessageCreate(
        operation_id=uuid4(), expected_version=1, content="周末安静地看山水"
    )
    saved = database.add_discovery_message(trip["id"], body)
    assert database.add_discovery_message(trip["id"], body) == saved
    run = saved["discovery_run"]
    assert run["params"]["messages"] == [{"role": "user", "content": body.content}]
    database.update_trip(trip["id"], TripPatch(expected_version=1, name="新条件"))
    assert run["params"]["request"]["destination"] is None
    assert database.get_run(run["id"])["params"] == run["params"]
    with database.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_run BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT,'write failure'); END"
        )
    with pytest.raises(sqlite3.Error):
        database.add_discovery_message(
            trip["id"],
            DiscoveryMessageCreate(
                operation_id=uuid4(), expected_version=2, content="第二次需求"
            ),
        )
    assert len(database.get_trip(trip["id"])["messages"]) == 1


def test_each_call_is_counted_once_and_cancelled_late_usage_remains(database):
    trip = create(database)
    enqueue(database, trip, max_model_calls=1)
    run = database.claim_run()
    action = database.begin_action(
        run["id"],
        run["generation"],
        "intent:model:1",
        "model",
        {"images_submitted": 2, "max_tokens": 16384},
    )
    assert action and database.get_run(run["id"])["usage"]["images"] == 2
    assert (
        database.begin_action(
            run["id"],
            run["generation"],
            "intent:model:2",
            "model",
            {"images_submitted": 0, "max_tokens": 16384},
        )
        is None
    )
    cancelled = database.cancel_run(run["id"], trip["id"], run["generation"])
    assert cancelled["usage"]["unknown_cost_calls"] == 1
    assert (
        database.begin_action(run["id"], run["generation"], "search:1", "search", {})
        is None
    )
    usage = {
        "usage_complete": True,
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    assert database.finish_action(
        action,
        status="completed",
        result={"model_data": {"verified": True}},
        usage=usage,
    )
    assert database.finish_action(action, status="completed", usage=usage)
    latest = database.get_run(run["id"])
    assert latest["status"] == "cancelled" and latest["result"] is None
    assert (
        latest["usage"]["model_tokens"] == 30
        and latest["usage"]["unknown_cost_calls"] == 0
    )
    assert latest["usage"]["external_calls"] == latest["usage"]["model_calls"] == 1


def test_recovery_marks_unknown_costs_and_keeps_completed_checkpoint(database):
    trip = create(database)
    enqueue(database, trip)
    run = database.claim_run()
    database.commit_discovery_stage(
        run["id"],
        run["generation"],
        "understanding",
        {"intent": {}, "query_plan": []},
        0.1,
    )
    database.begin_action(run["id"], run["generation"], "search:1", "search", {})
    database.begin_action(
        run["id"],
        run["generation"],
        "note:model:1",
        "model",
        {"images_submitted": 1, "max_tokens": 32768},
    )
    database.recover_runs()
    recovered = database.get_run(run["id"])
    assert recovered["status"] == "recoverable" and recovered["stage"] == "discovering"
    assert recovered["usage"]["unknown_result_calls"] == 2
    assert recovered["usage"]["unknown_cost_calls"] == 1
    assert all(
        action["status"] == "unknown" for action in database.list_actions(run["id"])
    )
    assert database.claim_run() is None
    resumed = database.resume_run(run["id"], trip["id"], recovered["generation"])
    assert resumed["usage"]["external_calls"] == 2
    assert not database.commit_discovery_stage(
        run["id"], run["generation"], "discovering", {}, 0
    )


def test_unknown_budget_and_parent_ownership_are_not_reset(database):
    trip, other = create(database), create(database)
    run = enqueue(database, trip, max_steps=1)
    current = database.claim_run()
    database.begin_action(
        current["id"], current["generation"], "search:1", "search", {}
    )
    database.cancel_run(current["id"], trip["id"], current["generation"])
    latest = database.get_run(run["id"])
    with pytest.raises(OperationConflict):
        database.resume_run(run["id"], trip["id"], latest["generation"])
    with pytest.raises(NotFound):
        database.enqueue_run(
            other["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=run["id"],
            ),
        )
    continued = database.enqueue_run(
        trip["id"],
        CreateRun(
            operation_id=uuid4(),
            expected_version=1,
            kind="discover",
            parent_run_id=run["id"],
        ),
    )
    assert (
        continued["usage"]["steps"] == 0
        and continued["params"]["parent_run_id"] == run["id"]
    )
    assert database.trip_usage(trip["id"])["external_calls"] == 1


def test_direction_adoption_requires_current_owned_sourced_direction(database):
    trip, other = create(database), create(database)
    enqueue(database, trip)
    run = database.claim_run()
    note = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceNote(external_id="n1", title="杭州西湖", text="杭州西湖适合周末"),
    )
    mention = PlaceMention(
        source_id=note["id"],
        name="西湖",
        city="杭州",
        category="play",
        quote="杭州西湖适合周末",
    )
    direction = CandidateDraft(
        name="杭州方向",
        kind="direction",
        city="杭州",
        category="play",
        mention_ids=[mention.id],
    )
    database.save_discovery_batch(
        trip["id"],
        run["id"],
        run["generation"],
        mentions=[mention],
        candidates=[direction],
    )
    body = AdoptDirection(operation_id=uuid4(), expected_version=1, run_id=run["id"])
    with pytest.raises(NotFound):
        database.adopt_direction(other["id"], direction.id, body)
    adopted = database.adopt_direction(trip["id"], direction.id, body)
    assert (
        adopted["request"]["destination"] == "杭州" and adopted["request_version"] == 2
    )
    assert database.adopt_direction(trip["id"], direction.id, body) == adopted
    assert not database.run_is_current(run["id"], run["generation"])


def test_pipeline_commits_stages_in_order_and_waits_on_source_pause(database):
    async def check():
        trip = create(database)
        run = enqueue(database, trip)
        seen = []

        async def stage(run, snapshot, client):
            seen.append(run["stage"])
            assert not client.trust_env
            if run["stage"] == "fetching":
                raise DiscoveryPaused("source_network_unknown")
            return {"summary": run["stage"]}

        runner = DiscoveryRunner(
            database, Settings(), stages={name: stage for name in DISCOVERY_STAGES}
        )
        executor = RunExecutor(database, discovery=runner)
        await executor.start()
        try:
            recovered = await wait_status(database, run["id"], "recoverable")
            database.resume_run(run["id"], trip["id"], recovered["generation"])
            executor.wake()
            paused = await wait_status(database, run["id"], "paused")
            assert seen == ["understanding", "discovering", "fetching"]
            assert paused["checkpoint"]["completed_stages"] == [
                "understanding",
                "discovering",
            ]
            assert paused["stop_reason"] == "source_network_unknown"
            seen.clear()

            async def finish(run, snapshot, client):
                seen.append(run["stage"])
                return {"summary": run["stage"]}

            runner.stages = {name: finish for name in DISCOVERY_STAGES}
            database.resume_run(run["id"], trip["id"], paused["generation"])
            executor.wake()
            done = await wait_status(database, run["id"], "completed")
            assert seen == list(DISCOVERY_STAGES[2:])
            assert done["checkpoint"]["completed_stages"] == list(DISCOVERY_STAGES)
        finally:
            await executor.close()

    asyncio.run(check())


def test_atomic_message_api_returns_a_run_and_redacts_configured_secrets(database):
    trip = create(database)
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(secrets={"test-config-secret"})
    app.state.executor = RunExecutor(database)
    app.include_router(trips_router())
    app.include_router(runs_router())
    app.include_router(discovery_router())
    with TestClient(app) as client:
        payload = {
            "operation_id": str(uuid4()),
            "expected_version": 1,
            "content": "看看山水 test-config-secret",
            "start_discovery": True,
        }
        first = client.post(f"/api/trips/{trip['id']}/messages", json=payload)
        assert first.status_code == 201
        assert first.json()["discovery_run"]["kind"] == "discover"
        assert "test-config-secret" not in first.text
        assert (
            client.post(f"/api/trips/{trip['id']}/messages", json=payload).json()
            == first.json()
        )
        run_id = first.json()["discovery_run"]["id"]
        assert (
            client.get(f"/api/runs/{run_id}", params={"trip_id": trip["id"]}).json()[
                "trip_usage"
            ]["cost_cny"]
            is None
        )
