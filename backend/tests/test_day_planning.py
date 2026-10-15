import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
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


def multiday(count=3):
    value = inputs(
        count, duration_minutes=(count - 1) * 1440 + 540, transport="walking"
    )
    planning = value["request"]["planning"]
    planning["day_windows"] = [
        {
            "date": f"2026-10-{15 + index}",
            "start_at": f"2026-10-{15 + index}T09:00:00+08:00",
            "end_at": f"2026-10-{15 + index}T18:00:00+08:00",
        }
        for index in range(count)
    ]
    hotels = []
    for index in range(count - 1):
        hotel = deepcopy(value["selected"][index])
        hotel.update(candidate_id=f"hotel-{index}", category="stay", name="同名酒店")
        hotel["poi"].update(
            id=f"hotel-poi-{index}", location=[120.15 + index / 100, 30.25]
        )
        hotels.append(hotel)
    planning["overnight_stays"] = [
        {"date": f"2026-10-{15 + index}", "candidate_id": hotel["candidate_id"]}
        for index, hotel in enumerate(hotels)
    ]
    for index, constraint in enumerate(planning["activity_constraints"]):
        constraint["date"] = f"2026-10-{15 + index}"
    value["origin"]["city"] = "杭州市"
    value["lodgings"] = hotels
    value["planning"] = deepcopy(planning)
    value["input_hash"] = planning_hash(value)
    return value


def run(value, provider=None, **kwargs):
    return asyncio.run(schedule(value, provider or Routes(), **kwargs))[0]


def test_legacy_serialization_and_hash_remain_identical():
    old = inputs()
    serialized = TripRequest.model_validate(old["request"]).model_dump(mode="json")
    assert serialized == old["request"]
    assert "day_windows" not in serialized["planning"]
    assert "date" not in serialized["planning"]["activity_constraints"][0]
    extended = {**old, "lodgings": [], "lodging_candidates": multiday()["lodgings"]}
    assert planning_hash(extended) == planning_hash(old)


def test_fourteen_elapsed_days_can_cover_fifteen_local_dates_and_fourteen_nights():
    start = datetime.fromisoformat("2026-10-15T09:00:00+08:00")
    windows = []
    for index in range(15):
        day = start + timedelta(days=index)
        windows.append(
            {
                "date": day.date().isoformat(),
                "start_at": day.replace(hour=8 if index == 14 else 9).isoformat(),
                "end_at": day.replace(hour=9 if index == 14 else 18).isoformat(),
            }
        )
    request = TripRequest(
        start_at=start,
        duration_minutes=20160,
        planning={
            "day_windows": windows,
            "overnight_stays": [
                {"date": day["date"], "candidate_id": "same-explicit-hotel"}
                for day in windows[:-1]
            ],
        },
    )
    assert len(request.planning.day_windows) == 15
    assert len(request.planning.overnight_stays) == 14


@pytest.mark.parametrize(
    "change", ["duplicate", "gap", "timezone", "night", "appointment"]
)
def test_day_date_and_night_boundaries_are_validated(change):
    request = multiday()["request"]
    planning = request["planning"]
    if change == "duplicate":
        planning["day_windows"][1]["date"] = "2026-10-15"
    elif change == "gap":
        planning["day_windows"].pop(1)
    elif change == "timezone":
        planning["day_windows"][0]["start_at"] = "2026-10-14T20:00:00Z"
    elif change == "night":
        planning["overnight_stays"][0]["date"] = "2026-10-17"
    else:
        planning["activity_constraints"][0]["fixed_start_at"] = (
            "2026-10-16T10:00:00+08:00"
        )
    with pytest.raises(ValidationError):
        TripRequest.model_validate(request)


def test_budget_is_for_whole_trip_and_each_night_cost_is_unknown():
    value = multiday()
    value["request"]["budget_cny"] = 100
    for constraint in value["request"]["planning"]["activity_constraints"]:
        constraint.update(cost_cny=40, cost_source="user")
    result = run(value)
    assert result["status"] == "conflict"
    assert result["payload"]["usage"]["known_cost_cny"] == 120
    assert result["payload"]["usage"]["unknown_cost_items"] == 2
    assert "budget_conflict" in {
        issue["code"] for issue in result["payload"]["conflicts"]
    }
    assert len(result["payload"]["days"]) == 3


def test_return_is_to_trip_origin_and_same_named_branches_are_not_merged():
    value = multiday()
    value["request"]["planning"]["return_to_origin"] = True
    result = run(value)
    days = result["payload"]["days"]
    assert days[1]["origin"]["poi_id"] == "hotel-poi-0"
    assert days[1]["endpoint"]["poi_id"] == "hotel-poi-1"
    assert days[2]["endpoint"] == value["origin"]
    assert days[2]["itinerary"][-1]["leg"]["destination"] == value["origin"]["location"]
    PlanPayload.model_validate(result["payload"])
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_no_hotel_is_implicitly_reused_for_a_missing_night():
    value = multiday()
    value["request"]["planning"]["overnight_stays"].pop()
    result = run(value)
    assert result["status"] == "partial"
    assert result["payload"]["days"][1]["lodging_candidate_id"] is None
    assert result["payload"]["days"][2]["origin"] is None
    assert any(
        issue["code"] == "lodging_gap" for issue in result["payload"]["unknowns"]
    )
    with pytest.raises(OperationConflict):
        PlanningStore._validate_daily_adoption(result["payload"], value)


def test_daily_adoption_accepts_normalized_coordinates_and_rejects_broken_connections():
    class Rounded(Routes):
        async def route(self, **kwargs):
            leg = await super().route(**kwargs)
            for key in ("origin", "destination"):
                leg[key] = [round(value, 6) for value in leg[key]]
            return leg

    value = multiday(2)
    value["origin"]["location"][0] = 120.051234567
    result = run(value, Rounded())
    PlanningStore._validate_daily_adoption(result["payload"], value)
    payload = deepcopy(result["payload"])
    payload["days"][1]["itinerary"][0]["leg"]["origin"] = [121, 31]
    payload["itinerary"] = [
        step for day in payload["days"] for step in day["itinerary"]
    ]
    with pytest.raises(OperationConflict, match="连续"):
        PlanningStore._validate_daily_adoption(payload, value)


def test_different_city_hotel_needs_reliable_rail_without_a_cross_city_amap_route():
    value = multiday(2)
    value["lodgings"][0]["poi"]["city"] = "上海市"
    provider = Routes()
    result = run(value, provider)
    assert result["status"] == "partial"
    assert "rail_timetable_unknown" in {
        issue["code"] for issue in result["payload"]["unknowns"]
    }
    assert not any(
        call[2] == value["lodgings"][0]["poi"]["location"] for call in provider.calls
    )


def test_route_budget_is_shared_and_a_missing_day_never_silently_drops_an_overnight():
    value = multiday()
    result = run(value, Routes(budget=2))
    assert result["status"] == "partial"
    assert "route_budget_exhausted" in {
        issue["code"] for issue in result["payload"]["unknowns"]
    }
    value["request"]["planning"]["day_windows"].pop()
    value["request"]["planning"]["overnight_stays"].pop()
    value["request"]["planning"]["activity_constraints"].pop()
    result = run(value)
    assert result["status"] == "partial"
    assert result["payload"]["unknowns"][0]["code"] == "day_coverage_missing"


def test_user_hotel_time_constraints_wait_or_report_conflict_instead_of_assuming_checkin_policy():
    value = multiday(2)
    stay = value["request"]["planning"]["overnight_stays"][0]
    stay["check_in_not_before"] = "2026-10-15T14:00:00+08:00"
    result = run(value)
    assert result["status"] == "conditional"
    assert (
        result["payload"]["days"][0]["itinerary"][-1]["end_at"]
        == stay["check_in_not_before"]
    )
    assert result["payload"]["days"][0]["itinerary"][-1]["source"] == "user"
    stay["check_out_by"] = "2026-10-16T08:00:00+08:00"
    result = run(value)
    assert result["status"] == "conflict"
    assert "lodging_checkout_conflict" in {
        issue["code"] for issue in result["payload"]["conflicts"]
    }
    stay.pop("check_out_by")
    stay["check_in_not_before"] = "2026-10-15T19:00:00+08:00"
    assert run(value)["status"] == "conflict"


def test_user_nightly_total_counts_towards_trip_budget_and_unknowns_are_not_serialized_as_zero():
    value = multiday(2)
    serialized = TripRequest.model_validate(value["request"]).model_dump(mode="json")
    assert set(serialized["planning"]["overnight_stays"][0]) == {"date", "candidate_id"}
    value["request"]["budget_cny"] = 50
    value["request"]["planning"]["overnight_stays"][0]["nightly_cost_cny"] = 100
    result = run(value)
    assert result["status"] == "conflict"
    assert result["payload"]["usage"]["known_cost_cny"] == 100
    value["request"]["planning"]["overnight_stays"][0]["check_out_by"] = (
        "2026-10-15T10:00:00+08:00"
    )
    with pytest.raises(ValidationError):
        TripRequest.model_validate(value["request"])
