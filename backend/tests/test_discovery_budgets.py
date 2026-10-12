from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database, OperationConflict


@pytest.fixture
def database(tmp_path):
    result = Database(tmp_path / "budgets.sqlite3")
    result.initialize()
    return result


def running(database, **budget):
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(
        trip["id"],
        CreateRun(
            operation_id=uuid4(), expected_version=1, kind="discover", budget=budget
        ),
    )
    return trip, database.claim_run()


def begin(database, run, key="model:1", *, max_tokens=600, images=0):
    return database.begin_action(
        run["id"],
        run["generation"],
        key,
        "model",
        {"max_tokens": max_tokens, "images_submitted": images},
    )


def known_usage(prompt=10, completion=20):
    return {
        "usage_complete": True,
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def test_request_output_reservation_is_checked_before_any_call(database):
    _, run = running(database, max_tokens=1)
    assert begin(database, run, max_tokens=16384) is None
    latest = database.get_run(run["id"])
    assert latest["usage"]["reserved_tokens"] == 0
    assert latest["usage"]["external_calls"] == latest["usage"]["model_calls"] == 0
    assert database.list_actions(run["id"]) == []


@pytest.mark.parametrize("maximum", [None, 0, 65537, True, "600", 600.0])
def test_output_reservation_must_be_a_bounded_integer(database, maximum):
    _, run = running(database)
    with pytest.raises(ValueError, match="token预算"):
        begin(database, run, max_tokens=maximum)
    assert not database.list_actions(run["id"])


def test_known_usage_and_unsettled_reservations_both_reduce_available_tokens(database):
    _, run = running(database, max_tokens=1000)
    first = begin(database, run)
    assert first
    assert database.get_run(run["id"])["usage"]["reserved_tokens"] == 600
    assert begin(database, run, "model:2", max_tokens=401) is None
    second = begin(database, run, "model:2", max_tokens=400)
    assert second
    database.finish_action(first, status="completed", usage=known_usage(50, 50))
    usage = database.get_run(run["id"])["usage"]
    assert usage["model_tokens"] == 100 and usage["reserved_tokens"] == 400
    assert begin(database, run, "model:3", max_tokens=501) is None
    assert begin(database, run, "model:3", max_tokens=500)
    assert database.get_run(run["id"])["usage"]["model_calls"] == 3


def test_unknown_usage_keeps_reservation_until_cancelled_late_result_settles_once(
    database,
):
    trip, run = running(database, max_tokens=1000)
    action = begin(database, run)
    database.finish_action(
        action,
        status="unknown",
        usage={
            "usage_complete": False,
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        },
    )
    usage = database.get_run(run["id"])["usage"]
    assert usage["reserved_tokens"] == 600 and usage["model_tokens"] == 10
    assert usage["unknown_cost_calls"] == usage["unknown_result_calls"] == 1
    assert begin(database, run, "model:2", max_tokens=400) is None
    cancelled = database.cancel_run(run["id"], trip["id"], run["generation"])
    assert cancelled["usage"]["unknown_cost_calls"] == 1
    database.resume_run(run["id"], trip["id"], cancelled["generation"])
    current = database.claim_run()
    assert database.finish_action(action, status="completed", usage=known_usage())
    assert database.finish_action(action, status="completed", usage=known_usage())
    usage = database.get_run(run["id"])["usage"]
    assert (
        usage["reserved_tokens"]
        == usage["unknown_cost_calls"]
        == usage["unknown_result_calls"]
        == 0
    )
    assert usage["model_tokens"] == 30 and usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 20
    assert database.get_run(run["id"])["generation"] == current["generation"]
    assert begin(database, current, "model:2", max_tokens=970)
    assert database.get_run(run["id"])["usage"]["reserved_tokens"] == 970


def test_restart_and_repeated_unknown_callback_do_not_reset_reservation(database):
    _, run = running(database, max_tokens=600)
    action = begin(database, run)
    database.recover_runs()
    assert database.get_run(run["id"])["usage"]["reserved_tokens"] == 600
    for _ in range(2):
        database.finish_action(
            action, status="unknown", usage={"usage_complete": False}
        )
    latest = database.get_run(run["id"])
    assert latest["status"] == "recoverable"
    assert (
        latest["usage"]["unknown_result_calls"]
        == latest["usage"]["unknown_cost_calls"]
        == 1
    )
    assert latest["usage"]["reserved_tokens"] == 600
    database.finish_action(action, status="failed", usage=known_usage())
    usage = database.get_run(run["id"])["usage"]
    assert (
        usage["reserved_tokens"]
        == usage["unknown_cost_calls"]
        == usage["unknown_result_calls"]
        == 0
    )
    assert usage["model_tokens"] == 30


def test_incomplete_known_result_can_settle_without_duplicate_tokens_or_losing_cache(
    database,
):
    _, run = running(database)
    action = begin(database, run)
    database.finish_action(
        action,
        status="completed",
        result={"model_data": {"verified": True}},
        usage={
            "usage_complete": False,
            "usage": {"prompt_tokens": 10, "total_tokens": 30},
        },
    )
    assert database.get_run(run["id"])["usage"]["reserved_tokens"] == 600
    database.finish_action(action, status="completed", usage=known_usage())
    database.finish_action(action, status="completed", usage=known_usage(100, 200))
    usage = database.get_run(run["id"])["usage"]
    assert usage["reserved_tokens"] == usage["unknown_cost_calls"] == 0
    assert usage["model_tokens"] == 30
    assert database.list_actions(run["id"])[0]["result"]["model_data"] == {
        "verified": True
    }


def test_usage_complete_flag_without_valid_counts_does_not_release_tokens(database):
    _, run = running(database)
    action = begin(database, run)
    database.finish_action(
        action,
        status="failed",
        usage={"usage_complete": True, "usage": {"total_tokens": True}},
    )
    usage = database.get_run(run["id"])["usage"]
    assert usage["reserved_tokens"] == 600 and usage["unknown_cost_calls"] == 1
    database.finish_action(action, status="failed", usage=known_usage())
    usage = database.get_run(run["id"])["usage"]
    assert usage["reserved_tokens"] == usage["unknown_cost_calls"] == 0
    assert usage["model_tokens"] == 30


def test_settled_usage_cannot_be_downgraded_by_a_late_unknown_callback(database):
    _, run = running(database)
    action = begin(database, run)
    database.finish_action(action, status="unknown", usage=known_usage())
    database.finish_action(action, status="unknown", usage={"usage_complete": False})
    database.finish_action(action, status="completed", usage=known_usage())
    usage = database.get_run(run["id"])["usage"]
    assert (
        usage["reserved_tokens"]
        == usage["unknown_cost_calls"]
        == usage["unknown_result_calls"]
        == 0
    )
    assert usage["model_tokens"] == 30


def test_concurrent_late_callbacks_settle_each_action_only_once(database):
    trip, run = running(database)
    actions = [begin(database, run, "model:1"), begin(database, run, "model:2")]
    database.cancel_run(run["id"], trip["id"], run["generation"])

    def finish(action):
        return database.finish_action(action, status="completed", usage=known_usage())

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert all(workers.map(finish, actions * 4))
    latest = database.get_run(run["id"])
    assert latest["status"] == "cancelled"
    assert latest["usage"]["model_tokens"] == 60
    assert (
        latest["usage"]["reserved_tokens"]
        == latest["usage"]["unknown_cost_calls"]
        == latest["usage"]["unknown_result_calls"]
        == 0
    )


def test_unpredicted_input_tokens_may_overshoot_but_stop_further_calls(database):
    _, run = running(database, max_tokens=1000)
    action = begin(database, run)
    database.finish_action(action, status="completed", usage=known_usage(800, 600))
    usage = database.get_run(run["id"])["usage"]
    assert usage["model_tokens"] == 1400 and usage["reserved_tokens"] == 0
    assert begin(database, run, "model:2", max_tokens=1) is None
    assert (
        database.begin_action(run["id"], run["generation"], "search:1", "search", {})
        is None
    )


@pytest.mark.parametrize("exhausted", ["calls", "tokens", "reserved"])
def test_resume_rechecks_model_call_and_token_budgets(database, exhausted):
    trip, run = running(
        database, max_model_calls=1 if exhausted == "calls" else 2, max_tokens=600
    )
    action = begin(database, run)
    if exhausted == "calls":
        database.finish_action(action, status="completed", usage=known_usage())
    elif exhausted == "tokens":
        database.finish_action(action, status="completed", usage=known_usage(300, 300))
    cancelled = database.cancel_run(run["id"], trip["id"], run["generation"])
    with pytest.raises(OperationConflict, match="模型预算"):
        database.resume_run(run["id"], trip["id"], cancelled["generation"])
    assert database.get_run(run["id"])["status"] == "cancelled"


def test_late_settlement_allows_budget_to_be_rechecked_after_a_partial_stop(database):
    trip, run = running(database, max_tokens=600)
    action = begin(database, run)
    database.finish_action(action, status="unknown", usage={"usage_complete": False})
    database.stop_run(run["id"], run["generation"], "partial", "model_budget_exhausted")
    with pytest.raises(OperationConflict):
        database.resume_run(run["id"], trip["id"], run["generation"])
    database.finish_action(action, status="completed", usage=known_usage())
    resumed = database.resume_run(run["id"], trip["id"], run["generation"])
    assert resumed["status"] == "pending" and resumed["usage"]["reserved_tokens"] == 0


@pytest.mark.parametrize("maximum_images", [0, 1])
def test_image_budget_does_not_block_resume_or_text_only_calls(
    database, maximum_images
):
    trip, run = running(database, max_images=maximum_images)
    if maximum_images:
        action = begin(database, run, images=1)
        database.finish_action(action, status="completed", usage=known_usage())
    cancelled = database.cancel_run(run["id"], trip["id"], run["generation"])
    database.resume_run(run["id"], trip["id"], cancelled["generation"])
    current = database.claim_run()
    assert begin(database, current, "text-only", images=0)


@pytest.mark.parametrize("kind", ["login", "search", "detail", "model"])
def test_prepare_workspace_refuses_external_action_ledger_cleanly(database, kind):
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1)
    )
    run = database.claim_run()
    params = {"max_tokens": 16384, "images_submitted": 0} if kind == "model" else {}
    assert (
        database.begin_action(run["id"], run["generation"], "external", kind, params)
        is None
    )
    assert database.get_run(run["id"])["usage"]["external_calls"] == 0
    assert not database.list_actions(run["id"])
