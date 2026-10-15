from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_day_planning import multiday, run
from test_planning_service import Routes
from test_rail_planning import railway, round_trip

from easytrip.storage.planning import PlanningStore, planning_hash


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(
        "easytrip.services.planning._current_time",
        lambda: datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def parent(value):
    original = deepcopy(run(value)["payload"])
    original["input"]["input_hash"] = planning_hash(original["input"])
    return {
        "id": "parent",
        "trip_id": value["trip_id"],
        "input_hash": original["input"]["input_hash"],
        "payload": original,
    }


def test_second_day_edit_reuses_unchanged_first_day_without_provider_calls():
    value = multiday()
    old = parent(value)
    value["request"]["planning"]["activity_constraints"][1]["duration_minutes"] = 20
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    assert result["payload"]["days"][0] == old["payload"]["days"][0]
    assert all(call[4].date().isoformat() != "2026-10-15" for call in provider.calls)
    assert result["payload"]["diff"][0]["reused_dates"] == ["2026-10-15"]
    value["request"]["planning"]["activity_constraints"][1]["duration_minutes"] = 30
    value["request"]["planning"]["overnight_stays"][1]["candidate_id"] = "hotel-0"
    value["lodgings"] = value["lodgings"][:1]
    provider = Routes()
    changed_hotel = run(value, provider, parent_plan=old)
    assert changed_hotel["status"] == "conditional"
    assert changed_hotel["payload"]["days"][0] == old["payload"]["days"][0]
    assert changed_hotel["payload"]["days"][2]["origin"]["poi_id"] == "hotel-poi-0"
    assert all(call[4].date().isoformat() != "2026-10-15" for call in provider.calls)


def test_next_day_progress_preserves_completed_day_as_history_and_routes_from_actual_position():
    value = multiday()
    old = parent(value)
    value["active_plan_id"] = old["id"]
    planning = value["request"]["planning"]
    planning["activity_constraints"][0]["state"] = "completed"
    planning.update(
        progress_at="2026-10-16T10:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "杭州市",
            "location": [120.13, 30.23],
            "source": "map",
        },
    )
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    assert all(step["historical"] for step in result["payload"]["days"][0]["itinerary"])
    assert any(
        step.get("execution_unknown")
        for step in result["payload"]["days"][0]["itinerary"]
        if step["kind"] == "route"
    )
    assert provider.calls[0][1] == [120.13, 30.23]
    assert all(call[4].date().isoformat() != "2026-10-15" for call in provider.calls)
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_locked_future_activity_keeps_its_original_time_and_cannot_change_date_or_duration():
    value = multiday()
    old = parent(value)
    planning = value["request"]["planning"]
    planning["activity_constraints"][1]["locked"] = True
    result = run(value, parent_plan=old)
    before = next(
        step
        for step in old["payload"]["itinerary"]
        if step.get("candidate_id") == "candidate-1"
    )
    after = next(
        step
        for step in result["payload"]["itinerary"]
        if step.get("candidate_id") == "candidate-1"
    )
    assert after["locked"] and after["start_at"] == before["start_at"]
    planning["activity_constraints"][1]["duration_minutes"] = 99
    assert run(value, parent_plan=old)["status"] == "conflict"
    planning["activity_constraints"][1]["duration_minutes"] = 30
    planning["activity_constraints"][1]["date"] = "2026-10-17"
    assert run(value, parent_plan=old)["status"] == "conflict"


def test_past_activity_state_and_hotel_must_not_be_silently_rewritten():
    value = multiday()
    old = parent(value)
    value["active_plan_id"] = old["id"]
    planning = value["request"]["planning"]
    planning.update(
        progress_at="2026-10-16T10:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "杭州市",
            "location": [120.13, 30.23],
            "source": "map",
        },
    )
    assert run(value, parent_plan=old)["status"] == "conflict"
    planning["activity_constraints"][0]["state"] = "completed"
    planning["overnight_stays"][0]["candidate_id"] = "hotel-1"
    assert run(value, parent_plan=old)["status"] == "conflict"


def test_progress_outside_days_and_wrong_active_parent_are_rejected():
    value = multiday()
    old = parent(value)
    value["request"]["planning"].update(
        progress_at="2026-10-18T10:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "杭州市",
            "location": [120.13, 30.23],
            "source": "map",
        },
    )
    assert run(value, parent_plan=old)["status"] == "conflict"


def test_same_day_completed_prefix_resumes_at_user_position_then_keeps_future_days():
    value = multiday(2)
    old = parent(value)
    value["active_plan_id"] = old["id"]
    planning = value["request"]["planning"]
    planning["activity_constraints"][0]["state"] = "completed"
    planning.update(
        progress_at="2026-10-15T11:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "杭州市",
            "location": [120.13, 30.23],
            "source": "map",
        },
    )
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    historical = [
        step for step in result["payload"]["itinerary"] if step.get("historical")
    ]
    assert historical and historical[-1]["state"] == "completed"
    assert provider.calls[0][1] == [120.13, 30.23]
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_cross_midnight_rail_history_is_preserved_and_second_day_uses_current_location():
    value = multiday(2)
    value["request"]["planning"]["overnight_stays"] = []
    value["request"]["planning"]["day_windows"][0]["end_at"] = (
        "2026-10-15T23:59:00+08:00"
    )
    value["selected"][1]["poi"].update(city="上海市", location=[121.31, 31.21])
    railway(value, overnight=True)
    old = parent(value)
    value["active_plan_id"] = old["id"]
    planning = value["request"]["planning"]
    planning["activity_constraints"][0]["state"] = "completed"
    planning.update(
        progress_at="2026-10-16T10:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "上海市",
            "location": [121.32, 31.22],
            "source": "map",
        },
    )
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    assert provider.calls[0][1] == [121.32, 31.22]
    assert result["payload"]["days"][0]["itinerary"][-1]["historical"]
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_locked_activity_after_rail_retains_original_time_when_return_train_changes():
    value = round_trip()
    old = parent(value)
    value["request"]["planning"]["activity_constraints"][0]["locked"] = True
    value["request"]["planning"]["rail_segments"][1].update(
        departure_at="2026-10-15T16:00:00+08:00", arrival_at="2026-10-15T17:00:00+08:00"
    )
    result = run(value, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    old_activity = next(
        step for step in old["payload"]["itinerary"] if step["kind"] == "activity"
    )
    activity = next(
        step for step in result["payload"]["itinerary"] if step["kind"] == "activity"
    )
    assert activity["locked"] and activity["start_at"] == old_activity["start_at"]


def test_same_day_after_rail_and_completed_activity_keeps_a_contiguous_historical_prefix():
    value = round_trip()
    old = parent(value)
    value["active_plan_id"] = old["id"]
    planning = value["request"]["planning"]
    planning["activity_constraints"][0]["state"] = "completed"
    planning.update(
        progress_at="2026-10-15T14:00:00+08:00",
        progress_origin={
            "name": "当前位置",
            "city": "上海市",
            "location": [121.32, 31.22],
            "source": "map",
        },
    )
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    historical = [
        step for step in result["payload"]["itinerary"] if step.get("historical")
    ]

    def facts(step):
        return {
            key: val
            for key, val in step.items()
            if key
            not in {
                "historical",
                "execution_unknown",
                "untravelled",
                "state",
                "locked",
                "required",
            }
        }

    assert [facts(step) for step in historical] == [
        facts(step) for step in old["payload"]["itinerary"][: len(historical)]
    ]
    assert provider.calls[0][1] == [121.32, 31.22]
    PlanningStore._validate_daily_adoption(result["payload"], value)


def test_reusing_a_partial_parent_cannot_turn_missing_hotel_or_time_into_a_feasible_plan():
    value = multiday()
    value["request"]["planning"]["overnight_stays"].pop()
    old = parent(value)
    result = run(value, parent_plan=old)
    assert result["status"] == "partial"
    assert result["payload"]["days"][2]["origin"] is None


def test_current_time_during_train_does_not_claim_boarding_or_a_real_time_arrival():
    value = round_trip()
    old = parent(value)
    value["active_plan_id"] = old["id"]
    value["request"]["planning"].update(
        progress_at="2026-10-15T10:30:00+08:00",
        progress_origin={
            "name": "用户位置",
            "city": "杭州市",
            "location": [120.2, 30.2],
            "source": "map",
        },
    )
    result = run(value, parent_plan=old)
    assert result["status"] == "partial"
    assert "rail_in_progress_unverified" in {
        issue["code"] for issue in result["payload"]["unknowns"]
    }


def test_changing_second_day_train_preserves_the_first_day_and_old_version():
    value = multiday()
    for place in value["selected"][1:]:
        place["poi"].update(city="上海市", location=[121.31, 31.21])
    value["lodgings"][1]["poi"].update(city="上海市", location=[121.33, 31.23])
    railway(value, overnight=True)
    value["request"]["planning"]["rail_segments"][0].update(
        date="2026-10-16",
        departure_at="2026-10-16T10:00:00+08:00",
        arrival_at="2026-10-16T11:00:00+08:00",
    )
    old = parent(value)
    snapshot = deepcopy(old)
    value["request"]["planning"]["rail_segments"][0].update(
        departure_at="2026-10-16T10:30:00+08:00", arrival_at="2026-10-16T11:30:00+08:00"
    )
    provider = Routes()
    result = run(value, provider, parent_plan=old)
    assert result["status"] == "conditional", result["payload"]["conflicts"]
    assert result["payload"]["days"][0] == old["payload"]["days"][0]
    assert all(call[4].date().isoformat() != "2026-10-15" for call in provider.calls)
    assert old == snapshot
