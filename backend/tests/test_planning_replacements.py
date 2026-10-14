from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_places import map_action, raw_poi, seed, unique
from test_planning import saved_plan

from easytrip.api.planning import make_router
from easytrip.api.trips import make_router as trips_router
from easytrip.domain.models import ActivityConstraint, TripPatch, TripRequest
from easytrip.domain.places import ConfirmOrigin, MatchPlaces
from easytrip.domain.planning import AdoptPlan
from easytrip.services.places import normalize_poi
from easytrip.storage.database import Database, now


@pytest.fixture
def saved(tmp_path):
    database = Database(tmp_path / "replacements.sqlite3")
    database.initialize()
    trip, run, candidates, *_ = seed(
        database,
        names=(
            "上午公园",
            "原下午公园",
            "傍晚公园",
            "替换公园",
            "歧义公园",
            "跨城公园",
            "未定位公园",
        ),
    )
    matches = {
        candidate.id: unique(
            normalize_poi(raw_poi(f"poi-{index}", candidate.name), now())
        )
        for index, candidate in enumerate(candidates[:4])
    }
    matches[candidates[4].id] = {
        "status": "ambiguous",
        "options": [normalize_poi(raw_poi("ambiguous-poi"), now())],
        "reason": "分店未确认",
    }
    matches[candidates[5].id] = unique(
        normalize_poi(
            raw_poi("other-city-poi", candidates[5].name, cityname="宁波市"), now()
        )
    )
    matches[candidates[6].id] = {"status": "unmatched", "reason": "无地点"}
    body = MatchPlaces(
        expected_version=1, expected_workspace_version=1, run_id=run["id"]
    )
    session = database.begin_place_match(trip["id"], body)
    database.finish_place_match(trip["id"], body, session, matches)
    for candidate in candidates[:3]:
        trip = map_action(database, trip, "select", candidate_id=candidate.id)["trip"]
    trip = database.confirm_origin(
        trip["id"],
        ConfirmOrigin(
            expected_version=trip["request_version"],
            expected_workspace_version=trip["workspace_version"],
            source="map",
            name="用户起点",
            location=[120.14, 30.24],
        ),
    )
    request = TripRequest.model_validate(trip["request"])
    request.start_at = datetime.now(timezone.utc) + timedelta(days=1)
    request.duration_minutes = 360
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    return database, trip, candidates


@pytest.fixture
def api(saved):
    database, trip, candidates = saved
    app = FastAPI()
    app.state.database = database
    app.include_router(make_router())
    app.include_router(trips_router())
    with TestClient(app) as client:
        yield client, database, trip, candidates


def replacement_body(client, trip_id, candidate_id, replacement_id):
    value = client.get(f"/api/trips/{trip_id}/planning-input").json()
    trip = client.get(f"/api/trips/{trip_id}").json()
    return {
        "operation_id": str(uuid4()),
        "expected_version": trip["request_version"],
        "expected_workspace_version": trip["workspace_version"],
        "input_hash": value["input_hash"],
        "candidate_id": candidate_id,
        "replacement_id": replacement_id,
    }


def test_historical_unique_same_city_replacements_survive_time_edits(api):
    client, database, trip, candidates = api
    url = f"/api/trips/{trip['id']}/planning-replacements"
    result = client.get(url)
    assert result.status_code == 200
    assert [item["candidate_id"] for item in result.json()["candidates"]] == [
        candidates[3].id
    ]
    assert result.json()["trip_id"] == trip["id"]
    request = TripRequest.model_validate(trip["request"])
    request.start_at += timedelta(hours=1)
    changed = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    result = client.get(url).json()
    assert result["request_version"] == changed["request_version"]
    assert result["workspace_version"] == changed["workspace_version"]
    assert [item["candidate_id"] for item in result["candidates"]] == [candidates[3].id]


def test_replacement_is_atomic_persistent_and_preserves_map_and_adopted_plan(api):
    client, database, trip, candidates = api
    plan, _ = saved_plan(database, trip["id"])
    database.adopt_plan(
        trip["id"],
        plan["id"],
        AdoptPlan(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            input_hash=plan["input_hash"],
        ),
    )
    before = database.get_trip(trip["id"])
    body = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    response = client.post(f"/api/trips/{trip['id']}/planning-replace", json=body)
    assert response.status_code == 200
    assert response.json()["operation_applied"] is True
    # Verify persistence through a new repository connection and a fresh server read.
    persisted = Database(database.path).get_trip(trip["id"])
    assert client.get(f"/api/trips/{trip['id']}").json() == persisted
    assert persisted["workspace"]["selected_ids"] == [
        candidates[0].id,
        candidates[3].id,
        candidates[2].id,
    ]
    assert (
        persisted["workspace"]["map_visible_ids"]
        == before["workspace"]["map_visible_ids"]
    )
    assert (
        persisted["workspace"]["confirmed_origin"]
        == before["workspace"]["confirmed_origin"]
    )
    assert persisted["active_plan_id"] == before["active_plan_id"] == plan["id"]
    assert persisted["request_version"] == before["request_version"]
    assert persisted["workspace_version"] == before["workspace_version"] + 1
    assert (
        client.get(f"/api/trips/{trip['id']}/planning-input").json()["input_hash"]
        != body["input_hash"]
    )
    stale = {**body, "operation_id": str(uuid4())}
    assert (
        client.post(f"/api/trips/{trip['id']}/planning-replace", json=stale).status_code
        == 409
    )
    assert database.get_trip(trip["id"]) == persisted


def test_uuid_retry_returns_current_selection_without_reapplying_and_body_conflicts(
    api,
):
    client, database, trip, candidates = api
    url = f"/api/trips/{trip['id']}/planning-replace"
    first = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    assert client.post(url, json=first).status_code == 200
    later = replacement_body(client, trip["id"], candidates[3].id, candidates[1].id)
    assert client.post(url, json=later).status_code == 200
    current = database.get_trip(trip["id"])
    retried = client.post(url, json=first)
    assert retried.status_code == 200
    assert retried.json() == {"trip": current, "operation_applied": True}
    assert database.get_trip(trip["id"]) == current
    changed_body = {**first, "replacement_id": candidates[2].id}
    assert client.post(url, json=changed_body).status_code == 409
    assert database.get_trip(trip["id"]) == current
    with database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM planning_replacements").fetchone()[
                0
            ]
            == 2
        )


@pytest.mark.parametrize(
    "case,status",
    [
        ("request_version", 409),
        ("workspace_version", 409),
        ("hash", 409),
        ("not_selected", 409),
        ("already_selected", 409),
        ("ambiguous", 409),
        ("unmatched", 409),
        ("cross_city", 409),
        ("foreign", 404),
    ],
)
def test_invalid_replacements_do_not_change_any_trip_state(api, case, status):
    client, database, trip, candidates = api
    body = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    if case == "request_version":
        body["expected_version"] -= 1
    elif case == "workspace_version":
        body["expected_workspace_version"] -= 1
    elif case == "hash":
        body["input_hash"] = "0" * 64
    elif case == "not_selected":
        body["candidate_id"] = candidates[3].id
    elif case == "already_selected":
        body["replacement_id"] = candidates[2].id
    elif case == "foreign":
        other, _, foreign, *_ = seed(database, names=("他人行程公园",))
        body["replacement_id"] = foreign[0].id
        assert (
            client.get(f"/api/trips/{other['id']}/planning-replacements").json()[
                "candidates"
            ]
            == []
        )
    else:
        body["replacement_id"] = candidates[
            {"ambiguous": 4, "cross_city": 5, "unmatched": 6}[case]
        ].id
    before = database.get_trip(trip["id"])
    response = client.post(f"/api/trips/{trip['id']}/planning-replace", json=body)
    assert response.status_code == status
    assert database.get_trip(trip["id"]) == before
    with database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM planning_replacements").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize(
    "constraint",
    [
        {"state": "completed"},
        {"locked": True},
        {"required": True},
        {"fixed_start_at": "2030-10-15T15:00:00+08:00"},
    ],
)
def test_protected_original_cannot_be_replaced(api, constraint):
    client, database, trip, candidates = api
    request = TripRequest.model_validate(trip["request"])
    request.planning.activity_constraints = [
        ActivityConstraint(candidate_id=candidates[1].id, **constraint)
    ]
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    body = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    response = client.post(f"/api/trips/{trip['id']}/planning-replace", json=body)
    assert response.status_code == 409
    assert database.get_trip(trip["id"]) == trip


@pytest.mark.parametrize("constraint", [{"excluded": True}, {"state": "skipped"}])
def test_replacement_cannot_bypass_its_existing_exclusion(api, constraint):
    client, database, trip, candidates = api
    request = TripRequest.model_validate(trip["request"])
    request.planning.activity_constraints = [
        ActivityConstraint(candidate_id=candidates[3].id, **constraint)
    ]
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    body = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    response = client.post(f"/api/trips/{trip['id']}/planning-replace", json=body)
    assert response.status_code == 409
    assert database.get_trip(trip["id"]) == trip


def test_receipt_write_failure_rolls_back_selection_update(api):
    client, database, trip, candidates = api
    body = replacement_body(client, trip["id"], candidates[1].id, candidates[3].id)
    before = deepcopy(database.get_trip(trip["id"]))
    with database.connection(write=True) as connection:
        connection.execute(
            "CREATE TRIGGER reject_replacement BEFORE INSERT ON planning_replacements BEGIN SELECT RAISE(ABORT, 'write failed'); END"
        )
    response = client.post(f"/api/trips/{trip['id']}/planning-replace", json=body)
    assert response.status_code == 503
    assert database.get_trip(trip["id"]) == before
    with database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM planning_replacements").fetchone()[
                0
            ]
            == 0
        )
