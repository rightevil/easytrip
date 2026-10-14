from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_places import seed, unique

from easytrip.api.planning import make_router
from easytrip.domain.models import (
    ActivityConstraint,
    CreateTrip,
    PlanningConstraints,
    TripPatch,
    TripRequest,
    WorkspacePatch,
)
from easytrip.domain.places import ConfirmOrigin, MapAction, MatchPlaces
from easytrip.domain.planning import AdoptPlan, PlanPayload, SavePlan
from easytrip.storage.database import (
    Database,
    NotFound,
    OperationConflict,
    VersionConflict,
)


@pytest.fixture
def database(tmp_path):
    store = Database(tmp_path / "planning.sqlite3")
    store.initialize()
    return store


def ready(database):
    trip, run, candidates, _, _ = seed(database)
    body = MatchPlaces(
        expected_version=1, expected_workspace_version=1, run_id=run["id"]
    )
    session = database.begin_place_match(trip["id"], body)
    snapshot = database.finish_place_match(
        trip["id"], body, session, {candidates[0].id: unique()}
    )
    trip = database.apply_map_action(
        trip["id"],
        MapAction(
            expected_version=1,
            expected_workspace_version=snapshot["trip"]["workspace_version"],
            action="select",
            candidate_id=candidates[0].id,
        ),
    )["trip"]
    trip = database.confirm_origin(
        trip["id"],
        ConfirmOrigin(
            expected_version=1,
            expected_workspace_version=trip["workspace_version"],
            source="map",
            name="明确起点",
            location=[120.14, 30.24],
        ),
    )
    request = TripRequest.model_validate(trip["request"])
    request.start_at = datetime.now(timezone.utc) + timedelta(days=1)
    request.duration_minutes = 360
    return database.update_trip(
        trip["id"], TripPatch(expected_version=1, request=request)
    ), candidates[0].id


def saved_plan(database, trip_id, **changes):
    value = database.planning_input(trip_id)
    params = dict(
        operation_id=uuid4(),
        request_version=value["request_version"],
        input_hash=value["input_hash"],
        status="conditional",
        payload=PlanPayload(
            input=value,
            itinerary=[
                {
                    "kind": "activity",
                    "candidate_id": value["selected"][0]["candidate_id"],
                }
            ],
            unknowns=[{"code": "test", "message": "模拟交通；非真实验收"}],
            valid_until=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    params.update(changes)
    body = SavePlan(**params)
    return database.save_plan(trip_id, body), body


def test_missing_inputs_are_truthful_and_not_a_plan(database):
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    value = database.planning_input(trip["id"])
    assert {item["code"] for item in value["missing"]} == {
        "origin_missing",
        "start_missing",
        "deadline_missing",
        "selection_missing",
    }
    assert database.list_plans(trip["id"]) == {"items": [], "active_plan_id": None}


def test_timezone_constraints_and_unknowns_validate():
    assert PlanningConstraints().timezone == "Asia/Shanghai"
    for values in (
        {"timezone": "not/a-zone"},
        {"return_to_origin": True, "end_candidate_id": "a"},
        {"disabled_modes": ["walking", "walking"]},
    ):
        with pytest.raises(ValidationError):
            PlanningConstraints(**values)
    for values in (
        {"duration_minutes": 60},
        {"cost_cny": 0},
        {"required": True, "excluded": True},
        {"locked": True, "state": "skipped"},
        {"fixed_start_at": "2026-10-15T12:00:00"},
    ):
        with pytest.raises(ValidationError):
            ActivityConstraint(candidate_id="a", **values)
    assert (
        ActivityConstraint(
            candidate_id="a", duration_minutes=60, duration_source="user"
        ).duration_minutes
        == 60
    )


def test_frozen_selection_survives_time_constraint_version_changes(database):
    trip, identifier = ready(database)
    value = database.planning_input(trip["id"])
    assert value["request_version"] == 2 and not value["missing"]
    assert value["selected"][0]["candidate_id"] == identifier
    assert value["selected"][0]["activities"] and value["selected"][0]["sources"]


def test_viewport_does_not_invalidate_input_but_origin_does(database):
    trip, _ = ready(database)
    before = database.planning_input(trip["id"])["input_hash"]
    workspace = deepcopy(trip["workspace"])
    workspace.update(map_center=[120.2, 30.3], map_zoom=15, active_tab="plan")
    trip = database.save_workspace(
        trip["id"],
        WorkspacePatch(expected_version=trip["workspace_version"], workspace=workspace),
    )
    assert database.planning_input(trip["id"])["input_hash"] == before
    database.confirm_origin(
        trip["id"],
        ConfirmOrigin(
            expected_version=trip["request_version"],
            expected_workspace_version=trip["workspace_version"],
            source="map",
            name="新起点",
            location=[120.1, 30.2],
        ),
    )
    assert database.planning_input(trip["id"])["input_hash"] != before


def test_plan_is_append_only_owned_and_idempotent(database):
    trip, _ = ready(database)
    plan, body = saved_plan(database, trip["id"])
    assert database.save_plan(trip["id"], body) == plan
    second, _ = saved_plan(database, trip["id"], parent_plan_id=plan["id"])
    assert second["version"] == 2 and database.get_plan(trip["id"], plan["id"]) == plan
    changed = body.model_copy(update={"status": "draft"})
    with pytest.raises(OperationConflict):
        database.save_plan(trip["id"], changed)
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    with pytest.raises(NotFound):
        database.get_plan(other["id"], plan["id"])


def test_adopt_checks_version_inputs_expiry_and_duplicate_id(database):
    trip, _ = ready(database)
    plan, _ = saved_plan(database, trip["id"])
    body = AdoptPlan(
        operation_id=uuid4(),
        expected_version=trip["request_version"],
        input_hash=plan["input_hash"],
    )
    first = database.adopt_plan(trip["id"], plan["id"], body)
    assert first["trip"]["active_plan_id"] == plan["id"]
    assert database.adopt_plan(trip["id"], plan["id"], body) == first
    with pytest.raises(VersionConflict):
        database.adopt_plan(
            trip["id"], plan["id"], body.model_copy(update={"expected_version": 1, "operation_id": uuid4()})
        )
    expired, _ = saved_plan(
        database,
        trip["id"],
        payload=PlanPayload(
            input=plan["payload"]["input"],
            itinerary=plan["payload"]["itinerary"],
            valid_until=datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    )
    with pytest.raises(OperationConflict):
        database.adopt_plan(
            trip["id"], expired["id"], body.model_copy(update={"operation_id": uuid4()})
        )
    latest = database.get_trip(trip["id"])
    database.confirm_origin(
        trip["id"],
        ConfirmOrigin(
            expected_version=latest["request_version"],
            expected_workspace_version=latest["workspace_version"],
            source="map",
            name="其他起点",
            location=[120.19, 30.25],
        ),
    )
    with pytest.raises(OperationConflict):
        database.adopt_plan(trip["id"], plan["id"], body.model_copy(update={"operation_id": uuid4()}))
    assert database.adopt_plan(trip["id"], plan["id"], body)["plan"] == plan


def test_no_client_endpoint_can_forge_validated_route(database):
    trip, _ = ready(database)
    app = FastAPI()
    app.state.database = database
    app.include_router(make_router())
    with TestClient(app) as client:
        assert client.get(f"/api/trips/{trip['id']}/planning-input").status_code == 200
        assert (
            client.post(
                f"/api/trips/{trip['id']}/plans", json={"status": "validated"}
            ).status_code
            == 405
        )
        assert client.get(f"/api/trips/{trip['id']}/plans").json()["items"] == []
    with pytest.raises(ValueError):
        saved_plan(database, trip["id"], status="validated")


def test_bad_selection_is_missing_and_cannot_leak_other_trip(database):
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    other, identifier = ready(database)
    workspace = deepcopy(trip["workspace"])
    workspace["selected_ids"] = [identifier, "does-not-exist"]
    database.save_workspace(
        trip["id"], WorkspacePatch(expected_version=1, workspace=workspace)
    )
    value = database.planning_input(trip["id"])
    assert value["selected"] == [] and all(
        item.get("candidate_id") in {None, identifier, "does-not-exist"}
        for item in value["missing"]
    )
    assert database.planning_input(other["id"])["selected"]


def test_parent_and_run_are_part_of_idempotent_operation(database):
    trip, _ = ready(database)
    _, body = saved_plan(database, trip["id"])
    other, _ = saved_plan(database, trip["id"])
    for changes in ({"parent_plan_id": other["id"]}, {"run_id": uuid4()}):
        with pytest.raises(OperationConflict):
            database.save_plan(trip["id"], body.model_copy(update=changes))


def test_global_source_reread_does_not_invalidate_frozen_input(database):
    trip, _ = ready(database)
    before = database.planning_input(trip["id"])
    with database.connection(write=True) as connection:
        connection.execute(
            "UPDATE sources SET last_read_at=? WHERE trip_id=?",
            (datetime.now(timezone.utc).isoformat(), trip["id"]),
        )
    after = database.planning_input(trip["id"])
    assert after["input_hash"] == before["input_hash"]
    assert "last_read_at" not in after["selected"][0]["sources"][0]


def test_free_text_destination_is_not_falsely_rejected_as_wrong_city(database):
    trip, _ = ready(database)
    request = TripRequest.model_validate(trip["request"])
    request.destination = "西湖"
    database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    assert not database.planning_input(trip["id"])["missing"]


def test_empty_conditional_plan_and_foreign_input_cannot_be_adopted_or_saved(database):
    trip, _ = ready(database)
    value = database.planning_input(trip["id"])
    plan, body = saved_plan(
        database,
        trip["id"],
        payload=PlanPayload(
            input=value, valid_until=datetime.now(timezone.utc) + timedelta(hours=1)
        ),
    )
    with pytest.raises(OperationConflict):
        database.adopt_plan(
            trip["id"],
            plan["id"],
            AdoptPlan(
                operation_id=uuid4(),
                expected_version=trip["request_version"],
                input_hash=value["input_hash"],
            ),
        )
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    with pytest.raises(ValueError):
        database.save_plan(other["id"], body)
