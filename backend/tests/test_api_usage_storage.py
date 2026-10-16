import json
from uuid import uuid4

import pytest

from easytrip.domain.models import ArchiveTrip, CreateTrip, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import (
    Database,
    NotFound,
    OperationConflict,
    VersionConflict,
)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "usage.sqlite3")
    database.initialize()
    return database


def trip(database):
    return database.create_trip(CreateTrip(operation_id=uuid4()))


def run(database, owner):
    database.enqueue_run(
        owner["id"],
        CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"),
    )
    return database.claim_run()


def test_api_calls_are_counted_before_send_and_isolated_by_trip(database):
    owner, other = trip(database), trip(database)
    action = database.begin_api_action(owner["id"], 1, "rail", "get-tickets")
    assert database.trip_usage(owner["id"])["external_calls"] == 1
    assert database.trip_usage(owner["id"])["started_calls"] == 1
    assert database.trip_usage(other["id"])["external_calls"] == 0
    assert database.finish_api_action(action, status="completed", elapsed_seconds=2.5)
    assert database.finish_api_action(action, status="completed", elapsed_seconds=50)
    assert not database.finish_api_action(action, status="failed", elapsed_seconds=50)
    report = Database(database.path).usage_report(owner["id"])
    assert (
        report["external_calls"]
        == report["api"]["external_calls"]
        == report["providers"]["rail"]["external_calls"]
        == 1
    )
    assert report["elapsed_seconds"] == report["api"]["elapsed_seconds"] == 2.5
    assert len(report["actions"]) == 1
    assert report["actions"][0]["elapsed_seconds"] == 2.5
    assert report["cost_cny"] is None and report["pricing_status"] == "unverified"
    assert report["account_total_known"] is False
    with pytest.raises(NotFound):
        database.usage_report(str(uuid4()))


def test_cache_hit_is_not_a_physical_request(database):
    owner = trip(database)
    action = database.begin_api_action(
        owner["id"], 1, "amap", "poi_search", cached=True
    )
    database.finish_api_action(
        action, status="completed", elapsed_seconds=0, metadata={"api_version": "v5"}
    )
    report = database.usage_report(owner["id"])
    assert report["external_calls"] == report["api"]["external_calls"] == 0
    assert report["cache_hits"] == report["providers"]["amap"]["cache_hits"] == 1
    assert report["actions"][0]["cached"] is True


def test_stale_or_archived_begin_fails_but_late_response_keeps_usage(database):
    owner = trip(database)
    action = database.begin_api_action(owner["id"], 1, "amap", "poi_detail")
    database.update_trip(owner["id"], TripPatch(expected_version=1, name="changed"))
    with pytest.raises(VersionConflict):
        database.begin_api_action(owner["id"], 1, "amap", "poi_search")
    assert database.finish_api_action(action, status="completed", elapsed_seconds=1)
    assert database.trip_usage(owner["id"])["external_calls"] == 1
    database.archive_trip(owner["id"], ArchiveTrip(expected_version=2, archived=True))
    with pytest.raises(OperationConflict):
        database.begin_api_action(owner["id"], 3, "rail", "get-tickets")


def test_recovery_marks_uncertain_once_and_never_replays(database):
    owner = trip(database)
    unknown = database.begin_api_action(owner["id"], 1, "rail", "get-tickets")
    failed = database.begin_api_action(
        owner["id"], 1, "rail", "get-station-code-by-names"
    )
    database.finish_api_action(failed, status="failed", elapsed_seconds=0.1)
    assert database.recover_api_actions() == 1
    assert database.recover_api_actions() == 0
    report = database.usage_report(owner["id"])
    assert (
        report["external_calls"] == 2
        and report["unknown_result_calls"] == report["failed_calls"] == 1
    )
    assert report["started_calls"] == 0
    assert (
        next(action for action in report["actions"] if action["id"] == unknown)[
            "elapsed_seconds"
        ]
        is None
    )
    assert report["providers"]["rail"]["elapsed_unknown_calls"] == 1
    assert not database.finish_api_action(
        unknown, status="completed", elapsed_seconds=5
    )
    assert len(database.usage_report(owner["id"])["actions"]) == 2


def test_run_ledger_is_not_added_twice_and_metadata_has_no_raw_payload(database):
    owner = trip(database)
    current = run(database, owner)
    model = database.begin_action(
        current["id"],
        current["generation"],
        "model:1",
        "model",
        {
            "max_tokens": 100,
            "images_submitted": 2,
            "model_id": "agnes-model",
            "task_type": "text_extraction",
            "prompt": "PRIVATE PROMPT",
        },
    )
    database.finish_action(
        model,
        status="completed",
        result={"model_data": {"raw": "PRIVATE RESULT"}},
        usage={
            "usage_complete": True,
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "error": "PRIVATE ERROR",
        },
    )
    source = database.begin_action(
        current["id"],
        current["generation"],
        "source:1",
        "search",
        {"tool": "search_feeds", "query": "PRIVATE QUERY", "Cookie": "PRIVATE COOKIE"},
    )
    database.finish_action(source, status="failed", result={"raw": "PRIVATE NOTE"})
    api = database.begin_api_action(owner["id"], 1, "amap", "poi_search")
    database.finish_api_action(
        api,
        status="completed",
        elapsed_seconds=0.5,
        metadata={
            "api_version": "v5",
            "schema_fingerprint": "a" * 64,
            "server_version": "0.3.10",
            "params": {"api_key": "PRIVATE KEY"},
            "error": "PRIVATE URL",
        },
    )
    report = database.usage_report(owner["id"])
    assert (
        report["external_calls"] == 3
        and report["model_tokens"] == 30
        and report["images"] == 2
    )
    assert report["runs"][0]["external_calls"] == 2
    assert report["api"]["external_calls"] == 1
    assert report["providers"]["model"]["external_calls"] == 1
    assert report["providers"]["xhs"]["failed_calls"] == 1
    assert report["prompt_tokens"] == 10 and report["completion_tokens"] == 20
    assert report["runs"][0]["first_candidates_at"] is None
    exported = json.dumps(report)
    assert (
        "PRIVATE" not in exported
        and "params_json" not in exported
        and "result_json" not in exported
    )
    assert report["actions"][0]["model_id"] == "agnes-model"
    assert set(report["actions"][-1]["metadata"]) == {
        "api_version",
        "server_version",
        "schema_fingerprint",
    }
    assert "actions" not in database.trip_usage(owner["id"])


def test_unknown_model_usage_and_legacy_attribution_stay_explicit(database):
    owner = trip(database)
    current = run(database, owner)
    action = database.begin_action(
        current["id"], current["generation"], "model:old", "model", {"max_tokens": 100}
    )
    database.finish_action(
        action,
        status="unknown",
        usage={"usage_complete": False, "usage": {"prompt_tokens": 10}},
    )
    database.cancel_run(current["id"], owner["id"], current["generation"])
    with database.connection(write=True) as connection:
        connection.execute(
            "UPDATE runs SET usage_json=json_set(usage_json,'$.external_calls',3) WHERE id=?",
            (current["id"],),
        )
    report = database.usage_report(owner["id"])
    assert report["external_calls"] == 3 and report["unknown_cost_calls"] == 1
    assert report["providers"]["unknown"]["external_calls"] == 2
    assert report["actions"][0]["model_id"] is None
    assert report["actions"][0]["total_tokens"] is None
    assert report["actions"][0]["usage_complete"] is False
    assert report["providers"]["model"]["unknown_cost_calls"] == 1


@pytest.mark.parametrize(
    "task_type", ["evidence_text", "evidence_multimodal", "output_repair"]
)
def test_actual_model_task_types_are_retained_in_safe_usage_report(database, task_type):
    owner = trip(database)
    current = run(database, owner)
    database.begin_action(
        current["id"],
        current["generation"],
        "model:actual",
        "model",
        {"max_tokens": 32768, "task_type": task_type},
    )
    assert database.usage_report(owner["id"])["actions"][0]["operation"] == task_type


def test_first_candidate_and_plan_amap_classification_are_not_guessed(database):
    owner = trip(database)
    current = run(database, owner)
    action = database.begin_action(
        current["id"], current["generation"], "amap:1", "detail", {"service": "amap"}
    )
    database.finish_action(
        action,
        status="completed",
        usage={"amap_kind": "route", "elapsed_seconds": 1.25},
    )
    first = "2026-10-16T01:23:45+00:00"
    database.save_run_item(
        current["id"], current["generation"], "first_candidates_at", first
    )
    database.save_run_item(
        current["id"],
        current["generation"],
        "source:capability",
        {
            "protocol_version": "2025-03-26",
            "server_info": {"version": "1.0", "name": "PRIVATE SERVER"},
            "schema_fingerprint": "b" * 64,
            "tool_schemas": {"PRIVATE TOOL": {"secret": "PRIVATE BODY"}},
        },
    )
    report = database.usage_report(owner["id"])
    assert report["providers"]["amap"]["external_calls"] == 1
    assert report["providers"]["xhs"]["external_calls"] == 0
    assert report["providers"]["amap"]["known_action_elapsed_seconds"] == 1.25
    assert report["runs"][0]["first_candidates_at"] == first
    assert report["runs"][0]["source_metadata"] == {
        "api_version": "2025-03-26",
        "server_version": "1.0",
        "schema_fingerprint": "b" * 64,
    }
    assert "PRIVATE" not in json.dumps(report)
    assert report["actions"][0]["operation"] == "route"
    database.save_run_item(
        current["id"],
        current["generation"],
        "first_candidates_at",
        {"raw": "PRIVATE NOTE"},
    )
    assert database.usage_report(owner["id"])["runs"][0]["first_candidates_at"] is None


def test_delete_cascades_usage_and_finish_cannot_recreate_it(database):
    owner, other = trip(database), trip(database)
    action = database.begin_api_action(owner["id"], 1, "rail", "get-tickets")
    database.delete_trip(owner["id"], 1)
    assert not database.finish_api_action(action, status="completed", elapsed_seconds=1)
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM api_actions").fetchone()[0] == 0
    assert database.trip_usage(other["id"])["external_calls"] == 0


@pytest.mark.parametrize(
    "provider,operation,cached",
    [
        ("model", "get-tickets", False),
        ("amap", "get-tickets", False),
        ("rail", "poi_detail", False),
        ("amap", "poi_search", 1),
    ],
)
def test_api_ledger_rejects_unrecognized_operations(
    database, provider, operation, cached
):
    owner = trip(database)
    with pytest.raises(ValueError):
        database.begin_api_action(owner["id"], 1, provider, operation, cached=cached)
    assert database.trip_usage(owner["id"])["external_calls"] == 0


@pytest.mark.parametrize("elapsed", [-1, float("nan"), float("inf"), True, "1"])
def test_invalid_duration_does_not_discard_started_record(database, elapsed):
    owner = trip(database)
    action = database.begin_api_action(owner["id"], 1, "amap", "poi_search")
    with pytest.raises(ValueError):
        database.finish_api_action(action, status="completed", elapsed_seconds=elapsed)
    assert database.trip_usage(owner["id"])["started_calls"] == 1


def test_allowed_metadata_cannot_include_url_or_cookie_body(database):
    owner = trip(database)
    action = database.begin_api_action(owner["id"], 1, "rail", "get-tickets")
    database.finish_api_action(
        action,
        status="completed",
        elapsed_seconds=0,
        metadata={
            "api_version": "https://private/?token=secret",
            "server_version": "Cookie: PRIVATE",
            "schema_fingerprint": "not a fingerprint",
        },
    )
    assert database.usage_report(owner["id"])["actions"][0]["metadata"] == {}
