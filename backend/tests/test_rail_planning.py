import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_day_planning import multiday
from test_planning_service import Routes, inputs

from easytrip.domain.models import TripRequest
from easytrip.domain.planning import PlanPayload
from easytrip.services.day_planning import schedule
from easytrip.storage.database import OperationConflict
from easytrip.storage.planning import PlanningStore, planning_hash


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(
        "easytrip.services.planning._current_time",
        lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def railway(value, *, overnight=False):
    stations = []
    for name, city, location in [
        ("杭州东站", "杭州市", [120.2, 30.2]),
        ("上海虹桥站", "上海市", [121.3, 31.2]),
    ]:
        place = deepcopy(value["selected"][0])
        place.update(candidate_id=name, name=name, city=city)
        place["poi"].update(
            id=name,
            name=name,
            city=city,
            location=location,
            type="交通设施服务;火车站",
            address=f"{name}具体地址",
        )
        stations.append(place)
    value["stations"] = stations
    value["origin"].update(city="杭州市")
    value["request"]["planning"].update(
        origin_city="杭州市",
        rail_segments=[
            {
                "id": "outbound",
                "date": "2026-10-15",
                "departure_station_id": "杭州东站",
                "arrival_station_id": "上海虹桥站",
                "train_number": "用户确认车次A",
                "departure_at": "2026-10-15T22:00:00+08:00"
                if overnight
                else "2026-10-15T10:00:00+08:00",
                "arrival_at": "2026-10-16T07:00:00+08:00"
                if overnight
                else "2026-10-15T11:00:00+08:00",
                "confirmed": True,
                "cost_cny": 80,
            }
        ],
    )
    if not overnight:
        value["request"]["planning"]["rail_segments"].append(
            {
                "id": "inbound",
                "date": "2026-10-15",
                "departure_station_id": "上海虹桥站",
                "arrival_station_id": "杭州东站",
                "train_number": "用户确认车次B",
                "departure_at": "2026-10-15T17:00:00+08:00",
                "arrival_at": "2026-10-15T18:00:00+08:00",
                "confirmed": True,
                "cost_cny": 90,
            }
        )
        value["request"]["planning"]["return_to_origin"] = True
    value["input_hash"] = planning_hash(value)
    value["planning"] = deepcopy(value["request"]["planning"])
    return value


def round_trip():
    value = inputs(1, duration_minutes=600, transport="walking")
    value["selected"][0]["poi"].update(city="上海市", location=[121.31, 31.21])
    return railway(value)


def run(value, provider=None):
    return asyncio.run(schedule(value, provider or Routes(), max_combinations=1))[0]


def test_single_day_round_trip_keeps_rail_separate_and_checks_both_city_sides():
    value = round_trip()
    provider = Routes()
    result = run(value, provider)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    rail = [step for step in result["payload"]["itinerary"] if step["kind"] == "rail"]
    assert [step["rail_id"] for step in rail] == ["outbound", "inbound"]
    assert all(step["source"] == "user" and "leg" not in step for step in rail)
    assert {call[3] for call in provider.calls} == {"杭州", "上海"}
    assert len(provider.calls) == 4
    assert result["payload"]["days"][0]["endpoint"] == value["origin"]
    PlanPayload.model_validate(result["payload"])
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_unconfirmed_train_or_subway_station_never_generates_a_fake_rail_timetable():
    value = round_trip()
    value["request"]["planning"]["rail_segments"][0]["confirmed"] = False
    provider = Routes()
    result = run(value, provider)
    assert result["status"] == "partial" and provider.calls == []
    assert "rail_timetable_unknown" in {
        item["code"] for item in result["payload"]["unknowns"]
    }
    value["request"]["planning"]["rail_segments"][0]["confirmed"] = True
    value["stations"][0]["poi"]["type"] = "交通设施服务;地铁站"
    result = run(value, provider)
    assert result["status"] == "partial" and provider.calls == []
    assert "rail_station_unconfirmed" in {
        item["code"] for item in result["payload"]["unknowns"]
    }


def test_train_deadline_conflict_and_whole_trip_budget_are_hard_constraints():
    value = round_trip()
    value["request"]["planning"]["rail_segments"][0]["departure_at"] = (
        "2026-10-15T09:20:00+08:00"
    )
    result = run(value)
    assert result["status"] == "conflict"
    assert "train_connection_conflict" in {
        item["code"] for item in result["payload"]["conflicts"]
    }
    value = round_trip()
    value["request"]["budget_cny"] = 160
    result = run(value)
    assert (
        result["status"] == "conflict"
        and result["payload"]["usage"]["known_cost_cny"] == 170
    )


def test_cross_midnight_retains_real_dates_without_inventing_a_hotel_night():
    value = multiday(2)
    value["request"]["planning"]["overnight_stays"] = []
    value["request"]["planning"]["day_windows"][0]["end_at"] = (
        "2026-10-15T23:59:00+08:00"
    )
    value["request"]["planning"]["day_windows"][1]["start_at"] = (
        "2026-10-16T06:00:00+08:00"
    )
    value["selected"][1]["poi"].update(city="上海市", location=[121.31, 31.21])
    railway(value, overnight=True)
    result = run(value)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    first, second = result["payload"]["days"]
    assert first["overnight_rail_id"] == second["continuing_rail_id"] == "outbound"
    assert first["endpoint"] is None and first["lodging_candidate_id"] is None
    assert second["origin"]["poi_id"] == "上海虹桥站"
    assert first["itinerary"][-1]["end_at"] == "2026-10-16T07:00:00+08:00"
    assert datetime.fromisoformat(
        second["itinerary"][0]["start_at"]
    ) >= datetime.fromisoformat("2026-10-16T07:20:00+08:00")
    assert not any(
        issue["code"] == "lodging_gap" for issue in result["payload"]["unknowns"]
    )
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_adoption_rejects_a_changed_timetable_or_disconnected_city_route():
    value = round_trip()
    result = run(value)
    for edit in ("timetable", "connection"):
        payload = deepcopy(result["payload"])
        if edit == "timetable":
            next(
                step
                for step in payload["days"][0]["itinerary"]
                if step["kind"] == "rail"
            )["train_number"] = "假车次"
        else:
            next(
                step
                for step in payload["days"][0]["itinerary"]
                if step["kind"] == "route"
            )["leg"]["origin"] = [121, 31]
        payload["itinerary"] = [
            step for day in payload["days"] for step in day["itinerary"]
        ]
        with pytest.raises(OperationConflict):
            PlanningStore._validate_daily_adoption(payload, value)


def test_rail_times_require_timezone_and_explicit_user_confirmation():
    value = round_trip()["request"]
    value["planning"]["rail_segments"][0]["arrival_at"] = "2026-10-15T08:00:00+08:00"
    with pytest.raises(ValueError):
        TripRequest.model_validate(value)


def test_fixed_appointment_and_budget_failure_are_not_conflated():
    value = round_trip()
    value["request"]["planning"]["activity_constraints"][0]["fixed_start_at"] = (
        "2026-10-15T10:00:00+08:00"
    )
    result = run(value)
    assert result["status"] == "conflict"
    assert "appointment_late" in {
        issue["code"] for issue in result["payload"]["conflicts"]
    }
    result = run(round_trip(), Routes(budget=0))
    assert result["status"] == "partial"
    assert "rail_connection_unverified" in {
        issue["code"] for issue in result["payload"]["unknowns"]
    }
    assert not result["payload"]["conflicts"]


def test_rail_buffer_cannot_be_bypassed_by_deleting_wait_steps_from_a_saved_plan():
    value = round_trip()
    payload = deepcopy(run(value)["payload"])
    itinerary = payload["days"][0]["itinerary"]
    rail = next(step for step in itinerary if step["kind"] == "rail")
    route = next(
        step
        for step in itinerary
        if step["kind"] == "route" and step["start_at"] > rail["end_at"]
    )
    route["start_at"] = rail["end_at"]
    payload["days"][0]["itinerary"] = [
        step for step in itinerary if step["kind"] != "wait"
    ]
    payload["itinerary"] = payload["days"][0]["itinerary"]
    with pytest.raises(OperationConflict):
        PlanningStore._validate_daily_adoption(payload, value)
