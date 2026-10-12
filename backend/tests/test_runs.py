import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from easytrip.agent.runner import RunExecutor
from easytrip.api.runs import make_router
from easytrip.domain.models import (
    ArchiveTrip,
    CreateTrip,
    MessageCreate,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)
from easytrip.domain.tasks import STAGES, CreateRun, RunBudget, RunControl
from easytrip.storage.database import (
    Database,
    NotFound,
    OperationConflict,
    VersionConflict,
)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    return database


def trip(database, **kwargs):
    return database.create_trip(CreateTrip(operation_id=uuid4(), **kwargs))


def enqueue(database, trip_id, **kwargs):
    return database.enqueue_run(
        trip_id, CreateRun(operation_id=uuid4(), expected_version=1, **kwargs)
    )


async def status(database, run_id, expected):
    async with asyncio.timeout(5):
        while True:
            result = await asyncio.to_thread(database.get_run, run_id)
            if result["status"] == expected:
                return result
            await asyncio.sleep(0.005)


def test_run_idempotency_is_atomic_and_freezes_original_input(database):
    saved = trip(database)
    database.add_message(
        saved["id"], MessageCreate(operation_id=uuid4(), content="周末去山水间")
    )
    body = CreateRun(operation_id=uuid4(), expected_version=1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(
            pool.map(lambda _: database.enqueue_run(saved["id"], body), range(8))
        )
    assert len({value["id"] for value in values}) == 1
    original = values[0]
    database.save_workspace(
        saved["id"],
        WorkspacePatch(expected_version=1, workspace=Workspace(active_tab="map")),
    )
    database.update_trip(saved["id"], TripPatch(expected_version=1, name="更新名称"))
    assert database.enqueue_run(saved["id"], body) == original
    assert original["params"]["workspace"]["active_tab"] == "overview"
    assert original["params"]["messages_count"] == 1
    assert len(database.get_trip(saved["id"])["runs"]) == 1
    with pytest.raises(OperationConflict):
        database.enqueue_run(
            saved["id"], body.model_copy(update={"budget": RunBudget(max_steps=1)})
        )
    with pytest.raises(VersionConflict):
        enqueue(database, saved["id"])


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "search"},
        {"expected_version": True},
        {"budget": {"max_steps": 4}},
        {"budget": {"max_seconds": 0}},
        {"budget": {"max_steps": True}},
        {"budget": {"external_calls": 1}},
    ],
)
def test_run_boundary_rejects_unsupported_or_unbounded_tasks(body):
    with pytest.raises(ValidationError):
        CreateRun.model_validate(
            {"operation_id": str(uuid4()), "expected_version": 1, **body}
        )
    with pytest.raises(ValidationError):
        RunControl(trip_id=uuid4(), generation=True)


def test_local_preparation_preserves_workspace_and_reports_missing_conditions(database):
    async def check():
        executor = RunExecutor(database)
        await executor.start()
        try:
            saved = trip(database)
            database.add_message(
                saved["id"], MessageCreate(operation_id=uuid4(), content="安静一点")
            )
            before = database.get_trip(saved["id"])
            run = enqueue(database, saved["id"])
            executor.wake()
            done = await status(database, run["id"], "completed")
            after = database.get_trip(saved["id"])
            assert done["checkpoint"]["completed_stages"] == list(STAGES)
            assert done["usage"]["steps"] == 3
            assert (
                done["usage"]["external_calls"]
                == done["usage"]["model_tokens"]
                == done["usage"]["images"]
                == 0
            )
            assert done["usage"]["elapsed_seconds"] >= 0
            assert done["result"]["conditions"]["missing"] == [
                "origin",
                "start_at",
                "duration_minutes",
            ]
            assert done["result"]["conditions"]["destination"] is None
            assert done["result"]["workspace"]["messages_count"] == 1
            for key in (
                "request",
                "request_version",
                "workspace",
                "workspace_version",
                "messages",
            ):
                assert after[key] == before[key]
        finally:
            await executor.close()

    asyncio.run(check())


def test_single_executor_cancel_stops_new_stages_and_unblocks_next_trip(database):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        first_id = None

        async def hook(run, stage):
            calls.append((run["id"], stage))
            if run["id"] == first_id:
                entered.set()
                await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            first_trip, second_trip = trip(database), trip(database)
            first = enqueue(database, first_trip["id"])
            first_id = first["id"]
            executor.wake()
            await asyncio.wait_for(entered.wait(), 2)
            second = enqueue(database, second_trip["id"])
            executor.wake()
            await asyncio.sleep(0.02)
            assert database.get_run(second["id"])["status"] == "pending"
            running = database.get_run(first["id"])
            cancelled = database.cancel_run(
                first["id"], first_trip["id"], running["generation"]
            )
            executor.interrupt(first["id"], running["generation"])
            await status(database, second["id"], "completed")
            assert database.get_run(first["id"])["status"] == "cancelled"
            assert cancelled["generation"] > running["generation"]
            assert [stage for run_id, stage in calls if run_id == first["id"]] == [
                STAGES[0]
            ]
            assert database.get_trip(first_trip["id"])["request_version"] == 1
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_cancel_and_resume_reuses_committed_stages_and_fences_old_generation(database):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        paused = True

        async def hook(run, stage):
            calls.append(stage)
            if stage == STAGES[1] and paused:
                entered.set()
                await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            await asyncio.wait_for(entered.wait(), 2)
            running = database.get_run(run["id"])
            assert running["checkpoint"]["completed_stages"] == [STAGES[0]]
            cancelled = database.cancel_run(
                run["id"], saved["id"], running["generation"]
            )
            executor.interrupt(run["id"], running["generation"])
            paused = False
            resumed = database.resume_run(
                run["id"], saved["id"], cancelled["generation"]
            )
            executor.wake()
            done = await status(database, run["id"], "completed")
            assert calls.count(STAGES[0]) == 1
            assert done["usage"]["steps"] == 3
            assert not database.commit_run_stage(
                run["id"],
                running["generation"],
                STAGES[0],
                running["checkpoint"][STAGES[0]],
                0.1,
            )
            with pytest.raises(OperationConflict):
                database.cancel_run(run["id"], saved["id"], resumed["generation"])
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_late_interrupt_from_old_cancel_does_not_cancel_resumed_generation(database):
    async def check():
        first_entered, second_entered, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        arrivals = 0

        async def hook(run, stage):
            nonlocal arrivals
            if stage == STAGES[1]:
                arrivals += 1
                (first_entered if arrivals == 1 else second_entered).set()
                await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            await asyncio.wait_for(first_entered.wait(), 2)
            old = database.get_run(run["id"])
            cancelled = database.cancel_run(run["id"], saved["id"], old["generation"])
            executor.interrupt(run["id"], old["generation"])
            database.resume_run(run["id"], saved["id"], cancelled["generation"])
            executor.wake()
            await asyncio.wait_for(second_entered.wait(), 2)
            resumed = database.get_run(run["id"])
            assert resumed["generation"] > old["generation"]
            # The first HTTP handler may deliver its cancellation after this new claim.
            executor.interrupt(run["id"], old["generation"])
            await asyncio.sleep(0.02)
            assert executor._current is not None and not executor._current.cancelled()
            assert database.get_run(run["id"])["status"] == "running"
            release.set()
            done = await status(database, run["id"], "completed")
            assert done["usage"]["steps"] == 3
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_restart_requires_explicit_resume_and_preserves_checkpoint(database):
    async def check():
        entered = asyncio.Event()

        async def hook(run, stage):
            if stage == STAGES[1]:
                entered.set()
                await asyncio.Event().wait()

        first = RunExecutor(database, before_stage=hook)
        await first.start()
        saved = trip(database)
        run = enqueue(database, saved["id"])
        first.wake()
        await asyncio.wait_for(entered.wait(), 2)
        old_generation = database.get_run(run["id"])["generation"]
        await first.close()
        reopened = Database(database.path)
        reopened.initialize()
        second = RunExecutor(reopened)
        await second.start()
        try:
            await asyncio.sleep(0.02)
            recoverable = reopened.get_run(run["id"])
            assert recoverable["status"] == "recoverable"
            assert recoverable["usage"]["steps"] == 1
            assert recoverable["generation"] > old_generation
            reopened.resume_run(run["id"], saved["id"], recoverable["generation"])
            second.wake()
            done = await status(reopened, run["id"], "completed")
            assert done["usage"]["steps"] == 3
        finally:
            await second.close()

    asyncio.run(check())


def test_abrupt_recovery_marks_unknown_time_and_never_auto_executes(database):
    saved = trip(database)
    run = enqueue(database, saved["id"])
    running = database.claim_run()
    database.recover_runs()
    recovered = database.get_run(run["id"])
    assert recovered["status"] == "recoverable"
    assert recovered["usage"]["interrupted_stage_time_unknown"] is True
    assert not database.run_is_current(run["id"], running["generation"])
    assert database.claim_run() is None


def test_changed_request_keeps_history_and_rejects_late_commit(database):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(run, stage):
            if stage == STAGES[1]:
                entered.set()
                await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved, other = trip(database), trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            await asyncio.wait_for(entered.wait(), 2)
            old = database.get_run(run["id"])
            updated = database.update_trip(
                saved["id"],
                TripPatch(expected_version=1, request=TripRequest(destination="苏州")),
            )
            release.set()
            stale = await status(database, run["id"], "stale")
            assert stale["checkpoint"]["completed_stages"] == [STAGES[0]]
            assert stale["result"] is None
            assert not database.run_is_current(run["id"], old["generation"])
            with pytest.raises(VersionConflict):
                database.resume_run(run["id"], saved["id"], stale["generation"])
            assert database.get_trip(saved["id"])["request"] == updated["request"]
            assert database.get_trip(other["id"])["runs"] == []
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_workspace_edit_during_run_does_not_invalidate_or_override_snapshot(database):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(run, stage):
            if stage == STAGES[1]:
                entered.set()
                await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            await asyncio.wait_for(entered.wait(), 2)
            edited = database.save_workspace(
                saved["id"],
                WorkspacePatch(
                    expected_version=1,
                    workspace=Workspace(
                        active_tab="plan", selected_ids=["saved-choice"]
                    ),
                ),
            )
            release.set()
            done = await status(database, run["id"], "completed")
            assert done["result"]["workspace"]["workspace_version"] == 1
            assert done["result"]["workspace"]["selected_count"] == 0
            assert database.get_trip(saved["id"])["workspace"] == edited["workspace"]
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_delete_running_trip_removes_tasks_and_prevents_late_writes(database):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(run, stage):
            entered.set()
            await release.wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved, other = trip(database), trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            await asyncio.wait_for(entered.wait(), 2)
            old = database.get_run(run["id"])
            database.delete_trip(saved["id"], 1)
            release.set()
            await asyncio.sleep(0.02)
            assert not database.run_is_current(run["id"], old["generation"])
            assert not database.stop_run(
                run["id"], old["generation"], "failed", "local_stage_failed"
            )
            with pytest.raises(NotFound):
                database.get_run(run["id"])
            assert database.get_trip(other["id"])["runs"] == []
            with database.connection() as connection:
                assert (
                    connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
                )
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


def test_step_budget_stops_with_checkpoint_and_cannot_be_reset_by_resume(database):
    async def check():
        executor = RunExecutor(database)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"], budget=RunBudget(max_steps=1))
            executor.wake()
            partial = await status(database, run["id"], "partial")
            assert partial["stop_reason"] == "step_budget_exhausted"
            assert partial["checkpoint"]["completed_stages"] == [STAGES[0]]
            assert partial["result"] is None and partial["usage"]["steps"] == 1
            with pytest.raises(OperationConflict):
                database.resume_run(run["id"], saved["id"], partial["generation"])
        finally:
            await executor.close()

    asyncio.run(check())


def test_time_budget_timeout_records_consumption_and_stops_later_steps(database):
    async def check():
        calls = []

        async def hook(run, stage):
            calls.append(stage)
            await asyncio.Event().wait()

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"], budget=RunBudget(max_seconds=1))
            executor.wake()
            partial = await status(database, run["id"], "partial")
            assert partial["stop_reason"] == "time_budget_exhausted"
            # Windows may fire a one-second asyncio timer a clock tick early.
            assert partial["usage"]["elapsed_seconds"] > 0.9
            assert partial["usage"]["steps"] == 0
            assert calls == [STAGES[0]]
            with pytest.raises(OperationConflict):
                database.resume_run(run["id"], saved["id"], partial["generation"])
        finally:
            await executor.close()

    asyncio.run(check())


def test_stage_commits_validate_shape_order_and_generation(database):
    saved = trip(database)
    run = enqueue(database, saved["id"])
    running = database.claim_run()
    output = {
        "conditions_complete": False,
        "missing": ["origin"],
        "destination": None,
        "transport": "mixed",
    }
    with pytest.raises(ValidationError):
        database.commit_run_stage(
            run["id"], running["generation"], STAGES[0], {"unknown": "unsafe"}, 0
        )
    assert database.commit_run_stage(
        run["id"], running["generation"], STAGES[0], output, 0.01
    )
    with pytest.raises(OperationConflict):
        database.commit_run_stage(
            run["id"], running["generation"], STAGES[0], output, 0.01
        )
    cancelled = database.cancel_run(run["id"], saved["id"], running["generation"])
    database.resume_run(run["id"], saved["id"], cancelled["generation"])
    fresh = database.claim_run()
    assert fresh["generation"] > running["generation"]
    assert not database.commit_run_stage(
        run["id"], running["generation"], STAGES[0], output, 0
    )
    assert database.get_run(run["id"])["usage"]["steps"] == 1


def test_failed_local_stage_retains_checkpoint_and_does_not_leak_exception(database):
    async def check():
        should_fail = True

        async def hook(run, stage):
            if should_fail and stage == STAGES[1]:
                raise RuntimeError("Bearer private-key must not be stored")

        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            failed = await status(database, run["id"], "failed")
            assert failed["error_code"] == "local_stage_failed"
            assert "private-key" not in str(failed)
            assert failed["checkpoint"]["completed_stages"] == [STAGES[0]]
            should_fail = False
            database.resume_run(run["id"], saved["id"], failed["generation"])
            executor.wake()
            done = await status(database, run["id"], "completed")
            assert done["usage"]["steps"] == 3 and done["error_code"] is None
        finally:
            await executor.close()

    asyncio.run(check())


def test_disk_write_failure_never_reports_checkpoint_success(database, monkeypatch):
    async def check():
        original = database.commit_run_stage

        def fail(*args):
            raise sqlite3.OperationalError("disk full / secret path")

        monkeypatch.setattr(database, "commit_run_stage", fail)
        executor = RunExecutor(database)
        await executor.start()
        try:
            saved = trip(database)
            run = enqueue(database, saved["id"])
            executor.wake()
            failed = await status(database, run["id"], "failed")
            assert failed["error_code"] == "storage_unavailable"
            assert failed["checkpoint"]["completed_stages"] == []
            assert failed["usage"]["steps"] == 0 and failed["result"] is None
            monkeypatch.setattr(database, "commit_run_stage", original)
            database.resume_run(run["id"], saved["id"], failed["generation"])
            executor.wake()
            await status(database, run["id"], "completed")
        finally:
            await executor.close()

    asyncio.run(check())


def test_invalid_pending_data_is_retained_and_worker_continues(database):
    async def check():
        executor = RunExecutor(database)
        await executor.start()
        try:
            saved = trip(database)
            bad = enqueue(database, saved["id"])
            with database.connection(write=True) as connection:
                connection.execute(
                    "UPDATE runs SET budget_json=? WHERE id=?",
                    ('{"max_steps":999}', bad["id"]),
                )
            good = enqueue(database, saved["id"])
            executor.wake()
            await status(database, good["id"], "completed")
            failed = database.get_run(bad["id"])
            assert (
                failed["status"] == "failed"
                and failed["error_code"] == "invalid_run_data"
            )
            assert failed["budget"] == {"max_steps": 999}
            assert failed["result"] is None
            with pytest.raises(OperationConflict):
                database.resume_run(bad["id"], saved["id"], failed["generation"])
        finally:
            await executor.close()

    asyncio.run(check())


def test_failure_recording_error_does_not_kill_worker_for_next_run(
    database, monkeypatch
):
    async def check():
        saved = trip(database)
        first_id = None

        async def hook(run, stage):
            if run["id"] == first_id:
                raise RuntimeError("local stage failed")

        original = database.stop_run

        def stop(*args, **kwargs):
            if args[0] == first_id:
                raise sqlite3.OperationalError("failure recording unavailable")
            return original(*args, **kwargs)

        monkeypatch.setattr(database, "stop_run", stop)
        executor = RunExecutor(database, before_stage=hook)
        await executor.start()
        try:
            first = enqueue(database, saved["id"])
            first_id = first["id"]
            second = enqueue(database, saved["id"])
            executor.wake()
            await status(database, second["id"], "completed")
            assert database.get_run(first["id"])["status"] == "running"
            assert database.get_run(first["id"])["checkpoint"]["completed_stages"] == []
        finally:
            await executor.close()
        assert database.get_run(first["id"])["status"] == "recoverable"

    asyncio.run(check())


def test_archiving_invalidates_running_task_and_blocks_resume(database):
    saved = trip(database)
    run = enqueue(database, saved["id"])
    running = database.claim_run()
    database.archive_trip(saved["id"], ArchiveTrip(expected_version=1, archived=True))
    cancelled = database.get_run(run["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["stop_reason"] == "trip_archived"
    assert not database.run_is_current(run["id"], running["generation"])
    with pytest.raises(OperationConflict):
        database.resume_run(run["id"], saved["id"], cancelled["generation"])


def test_slow_database_does_not_block_executor_event_loop(database, monkeypatch):
    async def check():
        entered, release, heartbeat = (
            threading.Event(),
            threading.Event(),
            asyncio.Event(),
        )
        original = database.claim_run

        def slow_claim():
            entered.set()
            release.wait(1)
            return original()

        monkeypatch.setattr(database, "claim_run", slow_claim)
        executor = RunExecutor(database)
        await executor.start()
        try:

            async def tick():
                while not entered.is_set():
                    await asyncio.sleep(0.001)
                heartbeat.set()
                release.set()

            await asyncio.wait_for(tick(), 0.5)
            assert heartbeat.is_set()
        finally:
            release.set()
            await executor.close()

    asyncio.run(check())


class PassiveExecutor:
    def wake(self):
        pass

    def interrupt(self, run_id, generation):
        pass


def api_app(database):
    application = FastAPI()
    application.state.database = database
    application.state.executor = PassiveExecutor()
    application.include_router(make_router())
    return application


def test_http_tasks_require_owner_and_generation_and_return_safe_storage_errors(
    database, monkeypatch
):
    saved, other = trip(database), trip(database)
    application = api_app(database)
    with TestClient(application, base_url="http://127.0.0.1") as client:
        payload = {
            "operation_id": str(uuid4()),
            "expected_version": 1,
            "kind": "prepare_workspace",
        }
        first = client.post(f"/api/trips/{saved['id']}/runs", json=payload)
        assert first.status_code == 201
        run = first.json()
        assert (
            client.post(f"/api/trips/{saved['id']}/runs", json=payload).json()["id"]
            == run["id"]
        )
        assert client.get(f"/api/runs/{run['id']}").status_code == 422
        assert (
            client.get(
                f"/api/runs/{run['id']}", params={"trip_id": other["id"]}
            ).status_code
            == 404
        )
        for action in ("cancel", "resume"):
            assert (
                client.post(
                    f"/api/runs/{run['id']}/{action}",
                    json={"trip_id": other["id"], "generation": run["generation"]},
                ).status_code
                == 404
            )
        cancelled = client.post(
            f"/api/runs/{run['id']}/cancel",
            json={"trip_id": saved["id"], "generation": run["generation"]},
        ).json()
        assert cancelled["status"] == "cancelled"
        assert (
            client.post(
                f"/api/runs/{run['id']}/resume",
                json={"trip_id": saved["id"], "generation": run["generation"]},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/runs/{run['id']}/resume",
                json={"trip_id": saved["id"], "generation": cancelled["generation"]},
            ).json()["status"]
            == "pending"
        )

        def fail(*args):
            raise sqlite3.OperationalError("API_KEY must not appear")

        monkeypatch.setattr(database, "enqueue_run", fail)
        failure = client.post(f"/api/trips/{saved['id']}/runs", json=payload)
        assert failure.status_code == 503
        assert failure.json()["detail"]["code"] == "database_unavailable"
        assert "API_KEY" not in failure.text


def test_async_task_api_does_not_block_other_requests_on_slow_write(
    database, monkeypatch
):
    async def check():
        entered, release = threading.Event(), threading.Event()
        saved = trip(database)
        original = database.enqueue_run

        def slow_enqueue(*args):
            entered.set()
            release.wait(1)
            return original(*args)

        monkeypatch.setattr(database, "enqueue_run", slow_enqueue)
        application = api_app(database)

        @application.get("/heartbeat")
        async def heartbeat():
            return {"responsive": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://127.0.0.1",
            trust_env=False,
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    f"/api/trips/{saved['id']}/runs",
                    json={"operation_id": str(uuid4()), "expected_version": 1},
                )
            )
            try:
                async with asyncio.timeout(0.5):
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                    response = await client.get("/heartbeat")
                    assert response.json()["responsive"] is True
                    assert not pending.done()
            finally:
                release.set()
            assert (await pending).status_code == 201

    asyncio.run(check())
