from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_planning import ready, saved_plan
from test_planning_replacements import saved as saved

from easytrip.domain.models import (
    ActivityConstraint,
    ConfirmedOrigin,
    PlanningConstraints,
    TripPatch,
    TripRequest,
)
from easytrip.domain.planning import AdoptPlan, PlanPayload
from easytrip.domain.routes import RouteLeg
from easytrip.storage import planning as planning_storage
from easytrip.storage.database import Database, OperationConflict


@pytest.fixture
def database(tmp_path):
    store = Database(tmp_path / "progress.sqlite3")
    store.initialize()
    return store


def test_progress_requires_explicit_location_and_aware_time():
    origin = {
        "name": "用户给定位置",
        "location": [120.1, 30.2],
        "source": "map",
        "city": "杭州",
    }
    for values in (
        {"progress_at": "2026-10-15T13:00:00+08:00"},
        {"progress_origin": origin},
        {"progress_origin": origin, "progress_at": "2026-10-15T13:00:00"},
        {
            "progress_origin": {**origin, "source": "amap", "poi_id": "test"},
            "progress_at": "2026-10-15T13:00:00+08:00",
        },
    ):
        with pytest.raises(ValidationError):
            PlanningConstraints(**values)
    assert (
        PlanningConstraints(
            progress_origin=origin, progress_at="2026-10-15T13:00:00+08:00"
        ).progress_origin.source
        == "map"
    )


@pytest.mark.parametrize("changes", [{"excluded": True}, {"state": "skipped"}])
def test_fixed_appointment_cannot_be_silently_removed(changes):
    with pytest.raises(ValidationError, match="固定预约"):
        ActivityConstraint(
            candidate_id="appointment",
            fixed_start_at="2026-10-15T13:00:00+08:00",
            **changes,
        )


@pytest.mark.parametrize("state,locked", [("completed", False), ("pending", True)])
def test_adoption_preserves_protected_facts_and_original_prefix(
    database, state, locked
):
    trip, identifier = ready(database)
    frozen = database.planning_input(trip["id"])
    item = frozen["selected"][0]
    start = TripRequest.model_validate(trip["request"]).start_at
    activity = {
        "kind": "activity",
        "candidate_id": identifier,
        "name": item["name"],
        "poi": item["poi"],
        "category": item["category"],
        "start_at": start.isoformat(),
        "end_at": (start + timedelta(minutes=60)).isoformat(),
        "duration_minutes": 60,
        "duration_source": "user",
        "cost_cny": 0.0,
        "cost_source": "user",
        "state": "pending",
        "locked": False,
        "required": False,
    }
    original, body = saved_plan(database, trip["id"])
    original, _ = saved_plan(
        database,
        trip["id"],
        payload=body.payload.model_copy(update={"itinerary": [activity]}),
    )
    database.adopt_plan(
        trip["id"],
        original["id"],
        AdoptPlan(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            input_hash=original["input_hash"],
        ),
    )
    request = TripRequest.model_validate(trip["request"])
    request.planning.activity_constraints = [
        ActivityConstraint(candidate_id=identifier, state=state, locked=locked)
    ]
    if state == "completed":
        request.planning.progress_at = start + timedelta(minutes=65)
        request.planning.progress_origin = ConfirmedOrigin.model_validate(
            frozen["origin"]
        )
    request = TripRequest.model_validate(request.model_dump())
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    frozen = database.planning_input(trip["id"])
    protected = {**activity, "state": state, "locked": locked}
    if state == "completed":
        protected["historical"] = True
    for changed in (
        {**protected, "name": "篡改历史名称"},
        {**protected, "start_at": (start + timedelta(minutes=1)).isoformat()},
    ):
        payload = PlanPayload(
            input=frozen, itinerary=[changed], valid_until=body.payload.valid_until
        )
        plan, _ = saved_plan(
            database, trip["id"], parent_plan_id=original["id"], payload=payload
        )
        with pytest.raises(OperationConflict, match="历史前缀|完整历史"):
            database.adopt_plan(
                trip["id"],
                plan["id"],
                AdoptPlan(
                    operation_id=uuid4(),
                    expected_version=trip["request_version"],
                    input_hash=plan["input_hash"],
                ),
            )
    payload = PlanPayload(
        input=frozen,
        itinerary=[deepcopy(protected)],
        valid_until=body.payload.valid_until,
    )
    plan, _ = saved_plan(
        database, trip["id"], parent_plan_id=original["id"], payload=payload
    )
    result = database.adopt_plan(
        trip["id"],
        plan["id"],
        AdoptPlan(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            input_hash=plan["input_hash"],
        ),
    )
    assert result["trip"]["active_plan_id"] == plan["id"]


def adopt(database, trip, plan):
    return database.adopt_plan(
        trip["id"],
        plan["id"],
        AdoptPlan(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            input_hash=plan["input_hash"],
        ),
    )["trip"]


@pytest.fixture
def mixed_progress(saved, monkeypatch):
    database, trip, candidates = saved
    frozen = database.planning_input(trip["id"])
    timestamp = datetime.now(timezone.utc)
    old_expiry, future_expiry = (
        timestamp + timedelta(minutes=5),
        timestamp + timedelta(hours=1),
    )
    cursor = TripRequest.model_validate(trip["request"]).start_at
    position, itinerary = frozen["origin"]["location"], []
    for index, item in enumerate(frozen["selected"]):
        arrival = cursor + timedelta(minutes=10)
        leg = RouteLeg(
            id=f"{index:064x}",
            mode="walking",
            origin=position,
            destination=item["poi"]["location"],
            departure_at=cursor,
            read_at=timestamp,
            valid_until=old_expiry if index < 2 else future_expiry,
            status="available",
            reason="脱敏测试路线",
            duration_seconds=600,
            distance_meters=100.0,
            walking_distance_meters=100.0,
            geometry=[[position, item["poi"]["location"]]],
            geometry_complete=True,
            cost_cny=0.0,
            cost_source="source",
        ).model_dump(mode="json")
        itinerary.append(
            {
                "kind": "route",
                "start_at": cursor.isoformat(),
                "end_at": arrival.isoformat(),
                "leg": leg,
            }
        )
        cursor = arrival + timedelta(minutes=30)
        itinerary.append(
            {
                "kind": "activity",
                "candidate_id": item["candidate_id"],
                "name": item["name"],
                "poi": item["poi"],
                "category": item["category"],
                "start_at": arrival.isoformat(),
                "end_at": cursor.isoformat(),
                "duration_minutes": 30,
                "duration_source": "user",
                "cost_cny": 0.0,
                "cost_source": "user",
                "state": "pending",
                "locked": False,
                "required": False,
            }
        )
        position = item["poi"]["location"]
    parent, _ = saved_plan(
        database,
        trip["id"],
        payload=PlanPayload(input=frozen, itinerary=itinerary, valid_until=old_expiry),
    )
    trip = adopt(database, trip, parent)
    request = TripRequest.model_validate(trip["request"])
    request.planning.activity_constraints = [
        ActivityConstraint(candidate_id=candidates[0].id, state="skipped"),
        ActivityConstraint(candidate_id=candidates[1].id, state="completed"),
    ]
    request.planning.progress_at = datetime.fromisoformat(itinerary[3]["end_at"])
    request.planning.progress_origin = ConfirmedOrigin(
        name="用户提供完成后的位置",
        location=itinerary[3]["poi"]["location"],
        source="map",
        city="杭州市",
    )
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    preserved = deepcopy(itinerary)
    for item in preserved[:4]:
        item["historical"] = True
    preserved[0].update(execution_unknown=True, untravelled=True)
    preserved[1].update(state="skipped", execution_unknown=True, untravelled=True)
    preserved[2]["execution_unknown"] = True
    preserved[3]["state"] = "completed"
    payload = PlanPayload(
        input=database.planning_input(trip["id"]),
        itinerary=preserved,
        unknowns=[
            {
                "code": "historical_execution_unknown",
                "message": "跳过项目的原路段未作为实际发生记录",
            }
        ],
        valid_until=future_expiry,
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (timestamp + timedelta(minutes=6)).astimezone(tz)

    monkeypatch.setattr("easytrip.storage.planning.datetime", Clock)
    return database, trip, parent, payload


def test_mixed_skipped_completed_history_is_adoptable_without_refreshing_old_routes(
    mixed_progress,
):
    database, trip, parent, payload = mixed_progress
    assert (
        datetime.fromisoformat(parent["payload"]["itinerary"][0]["leg"]["valid_until"])
        < planning_storage.datetime.now(timezone.utc)
        < payload.valid_until
    )
    plan, _ = saved_plan(
        database, trip["id"], parent_plan_id=parent["id"], payload=payload
    )
    adopted = adopt(database, trip, plan)
    assert adopted["active_plan_id"] == plan["id"]
    assert database.get_plan(trip["id"], parent["id"]) == parent
    actual = database.get_plan(trip["id"], plan["id"])["payload"]["itinerary"]
    assert actual == payload.itinerary
    for index in (0, 2):
        assert actual[index]["leg"] == parent["payload"]["itinerary"][index]["leg"]
    assert actual[0]["untravelled"] and actual[2]["execution_unknown"]
    assert actual[1]["state"] == "skipped" and actual[3]["state"] == "completed"


@pytest.mark.parametrize(
    "changed", ["route_duration", "route_geometry", "completed_time", "completed_cost"]
)
def test_mixed_history_cannot_rewrite_original_physical_facts(mixed_progress, changed):
    database, trip, parent, payload = mixed_progress
    payload = payload.model_copy(deep=True)
    if changed == "route_duration":
        payload.itinerary[0]["leg"]["duration_seconds"] += 1
    elif changed == "route_geometry":
        payload.itinerary[2]["leg"]["geometry"][0][0][0] += 0.01
    elif changed == "completed_time":
        payload.itinerary[3]["end_at"] = (
            datetime.fromisoformat(payload.itinerary[3]["end_at"])
            + timedelta(minutes=1)
        ).isoformat()
    else:
        payload.itinerary[3]["cost_cny"] = 1.0
    plan, _ = saved_plan(
        database, trip["id"], parent_plan_id=parent["id"], payload=payload
    )
    with pytest.raises(OperationConflict, match="历史前缀|完整历史"):
        adopt(database, trip, plan)
    assert database.get_trip(trip["id"])["active_plan_id"] == parent["id"]
    assert database.get_plan(trip["id"], parent["id"]) == parent


def test_future_route_expiration_still_blocks_adoption_with_valid_history(
    mixed_progress,
):
    database, trip, parent, payload = mixed_progress
    payload.itinerary[4]["leg"]["valid_until"] = parent["payload"]["itinerary"][0][
        "leg"
    ]["valid_until"]
    plan, _ = saved_plan(
        database, trip["id"], parent_plan_id=parent["id"], payload=payload
    )
    with pytest.raises(OperationConflict, match="交通段.*过期"):
        adopt(database, trip, plan)
    assert database.get_trip(trip["id"])["active_plan_id"] == parent["id"]


@pytest.mark.parametrize("change", ["remove", "pending"])
def test_adopted_completion_cannot_be_erased_by_request_edits(mixed_progress, change):
    database, trip, parent, payload = mixed_progress
    completed, _ = saved_plan(
        database, trip["id"], parent_plan_id=parent["id"], payload=payload
    )
    trip = adopt(database, trip, completed)
    request = TripRequest.model_validate(trip["request"])
    if change == "remove":
        request.planning.activity_constraints = [
            item
            for item in request.planning.activity_constraints
            if item.state != "completed"
        ]
    else:
        request.planning.activity_constraints = [
            item.model_copy(update={"state": "pending"})
            if item.state == "completed"
            else item
            for item in request.planning.activity_constraints
        ]
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    payload = payload.model_copy(
        update={"input": database.planning_input(trip["id"])}, deep=True
    )
    candidate, _ = saved_plan(
        database, trip["id"], parent_plan_id=completed["id"], payload=payload
    )
    with pytest.raises(OperationConflict, match="移除或重置.*完成事实"):
        adopt(database, trip, candidate)
    assert database.get_trip(trip["id"])["active_plan_id"] == completed["id"]
