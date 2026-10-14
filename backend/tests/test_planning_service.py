import asyncio
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from easytrip.domain.models import ActivityConstraint, TripRequest
from easytrip.integrations.amap import AmapBudgetExceeded
from easytrip.services.planning import schedule


def inputs(count=2, **request_changes):
    constraints = [
        ActivityConstraint(
            candidate_id=f"candidate-{index}",
            duration_minutes=30,
            duration_source="user",
            cost_cny=0,
            cost_source="user",
        )
        for index in range(count)
    ]
    request = TripRequest(
        **{
            "origin": "明确起点",
            "start_at": "2026-10-15T09:00:00+08:00",
            "duration_minutes": 360,
            "planning": {"activity_constraints": constraints},
            **request_changes,
        }
    )
    selected = [
        {
            "candidate_id": f"candidate-{index}",
            "name": f"明确活动{index}",
            "category": "play",
            "kind": "place",
            "city": "杭州市",
            "run_id": "run-1",
            "activities": [],
            "sources": [],
            "mentions": [],
            "evidence": [],
            "poi": {
                "id": f"poi-{index}",
                "name": f"明确地点{index}",
                "location": [120.1 + index * 0.01, 30.2],
                "city": "杭州市",
                "read_at": "2026-10-14T09:00:00+08:00",
                "api_version": "v5",
            },
        }
        for index in range(count)
    ]
    return {
        "trip_id": "trip-1",
        "request_version": 1,
        "request": request.model_dump(mode="json"),
        "planning": request.planning.model_dump(mode="json"),
        "origin": {
            "name": "明确起点",
            "location": [120.05, 30.2],
            "source": "map",
            "request_origin": "明确起点",
        },
        "selected_ids": [item["candidate_id"] for item in selected],
        "selected": selected,
        "missing": [],
        "active_plan_id": None,
        "input_hash": "0" * 64,
    }


def constraint(value, index=0, **changes):
    value["request"]["planning"]["activity_constraints"][index].update(changes)
    value["planning"] = deepcopy(value["request"]["planning"])
    return value


class Routes:
    def __init__(self, *, unavailable=(), budget=None, **changes):
        self.calls = []
        self.unavailable = set(unavailable)
        self.changes = changes
        self.budget = budget

    async def route(self, *, mode, origin, destination, city, departure_at):
        if self.budget is not None and len(self.calls) >= self.budget:
            raise AmapBudgetExceeded()
        self.calls.append(
            (mode, deepcopy(origin), deepcopy(destination), city, departure_at)
        )
        duration = 600 if mode == "walking" else 900
        walking = 500 if mode == "walking" else 100
        result = {
            "id": f"leg-{len(self.calls)}",
            "mode": mode,
            "origin": origin,
            "destination": destination,
            "city_code": "0571",
            "departure_at": departure_at.isoformat(),
            "read_at": "2026-10-14T09:00:00+08:00",
            "valid_until": "2026-10-16T09:00:00+08:00",
            "api_version": "v5",
            "status": "unavailable" if mode in self.unavailable else "available",
            "reason": "测试夹具",
            "provider_infocode": None,
            "duration_seconds": duration,
            "distance_meters": 500,
            "walking_distance_meters": walking,
            "geometry": [[origin, destination]],
            "geometry_complete": True,
            "cost_cny": 0,
            "cost_source": "source",
            "duration_source": "amap",
            "conditional": mode in {"bicycling", "electrobike"},
            "conditions": [],
        }
        result.update(self.changes.get(mode, {}))
        return result


def run(value, provider=None, **options):
    return asyncio.run(schedule(value, provider or Routes(), **options))


def codes(plan, group="unknowns"):
    return {item["code"] for item in plan["payload"][group]}


def activities(plan):
    return [item for item in plan["payload"]["itinerary"] if item["kind"] == "activity"]


def legs(plan):
    return [item for item in plan["payload"]["itinerary"] if item["kind"] == "route"]


def test_real_durations_buffer_and_snapshot_are_preserved():
    value = inputs()
    original = deepcopy(value)
    result = run(value, max_combinations=1)[0]
    first, second = activities(result)
    assert first["start_at"] == "2026-10-15T09:20:00+08:00"
    assert first["end_at"] == "2026-10-15T09:50:00+08:00"
    assert second["start_at"] == "2026-10-15T10:10:00+08:00"
    assert result["status"] == "conditional" and codes(result) == {
        "opening_unknown",
        "origin_city_unknown",
    }
    assert result["payload"]["valid_until"] == "2026-10-16T09:00:00+08:00"
    assert value == original
    assert legs(result)[0]["leg"]["duration_source"] == "amap"


def test_missing_inputs_do_not_query_or_call_it_a_feasible_schedule():
    value = inputs()
    value["missing"] = [{"code": "origin_missing", "message": "缺起点"}]
    provider = Routes()
    result = run(value, provider)[0]
    assert result["status"] == "draft" and result["payload"]["itinerary"] == []
    assert provider.calls == []


def test_single_day_city_and_generic_place_boundaries():
    for change, expected in (
        ("city", "conflict"),
        ("midnight", "partial"),
        ("generic", "draft"),
    ):
        value = inputs()
        if change == "city":
            value["selected"][1]["poi"]["city"] = "苏州市"
        elif change == "midnight":
            value["request"]["start_at"] = "2026-10-15T23:00:00+08:00"
        else:
            value["selected"][0]["kind"] = "generic"
        provider = Routes()
        assert run(value, provider)[0]["status"] == expected
        assert not provider.calls


def test_disabled_modes_are_never_queried_or_silently_substituted():
    value = inputs(transport="mixed")
    value["request"]["planning"]["disabled_modes"] = ["bicycling", "electrobike"]
    provider = Routes(unavailable=("walking", "transit"))
    result = run(value, provider)[0]
    assert result["status"] == "partial"
    assert {call[0] for call in provider.calls} == {"walking", "transit"}
    value = inputs(transport="bicycling")
    value["request"]["planning"]["disabled_modes"] = ["bicycling"]
    provider = Routes()
    assert run(value, provider)[0]["status"] == "conflict" and not provider.calls


def test_empty_transit_routes_have_no_arrival_or_downstream_guess():
    provider = Routes(unavailable=("transit",))
    plan = run(inputs(transport="transit"), provider, max_combinations=1)[0]
    assert plan["status"] == "partial" and "route_unavailable" in codes(plan)
    assert all(
        item["start_at"] is None and item["end_at"] is None for item in activities(plan)
    )
    assert not legs(plan) and len(provider.calls) == 1


def test_unknown_duration_stops_later_route_times_but_keeps_required_items():
    value = constraint(
        inputs(), duration_minutes=None, duration_source="unknown", required=True
    )
    constraint(value, 1, required=True)
    provider = Routes()
    plan = run(value, provider, max_combinations=1)[0]
    first, second = activities(plan)
    assert plan["status"] == "partial" and "duration_unknown" in codes(plan)
    assert first["start_at"] and first["end_at"] is None
    assert second["start_at"] is None and second["end_at"] is None
    assert first["required"] and second["required"]
    assert len(provider.calls) == 2


def test_source_estimates_are_not_user_confirmations_or_current_prices():
    value = constraint(
        inputs(), duration_source="source", cost_cny=25, cost_source="estimated"
    )
    plan = run(value, max_combinations=1)[0]
    assert {"duration_source_unverified", "activity_cost_unverified"} <= codes(plan)
    assert activities(plan)[0]["duration_source"] == "source"
    assert plan["payload"]["usage"]["estimated_cost_cny"] == 25
    assert plan["status"] == "conditional"


def test_fixed_appointment_and_window_conflicts_do_not_delete_the_activity():
    value = constraint(
        inputs(),
        required=True,
        fixed_start_at="2026-10-15T09:15:00+08:00",
        not_after="2026-10-15T09:40:00+08:00",
    )
    plan = run(value, max_combinations=1)[0]
    assert plan["status"] == "conflict"
    assert {"appointment_late", "activity_window_conflict"} <= codes(plan, "conflicts")
    assert activities(plan)[0]["candidate_id"] == "candidate-0"


def test_wait_for_known_earliest_start_and_appointment():
    value = constraint(inputs(1), fixed_start_at="2026-10-15T10:00:00+08:00")
    plan = run(value)[0]
    assert activities(plan)[0]["start_at"] == "2026-10-15T10:00:00+08:00"
    assert (
        len([item for item in plan["payload"]["itinerary"] if item["kind"] == "wait"])
        == 2
    )


def test_shared_vehicle_only_appointment_is_a_conflict():
    value = constraint(
        inputs(1, transport="bicycling"), fixed_start_at="2026-10-15T10:00:00+08:00"
    )
    plan = run(value)[0]
    assert plan["status"] == "conflict" and "shared_only_appointment" in codes(
        plan, "conflicts"
    )
    assert "shared_vehicle_unverified" in codes(plan)


def test_shared_vehicle_only_hard_deadline_is_not_promised_as_feasible():
    value = inputs(1, transport="electrobike")
    value["request"]["planning"]["end_at"] = "2026-10-15T11:00:00+08:00"
    plan = run(value)[0]
    assert plan["status"] == "conflict"
    assert "shared_only_appointment" in codes(plan, "conflicts")


def test_transit_total_duration_does_not_double_count_walking():
    provider = Routes(transit={"duration_seconds": 600, "walking_distance_meters": 700})
    plan = run(inputs(1, transport="transit"), provider)[0]
    assert legs(plan)[0]["end_at"] == "2026-10-15T09:10:00+08:00"
    assert activities(plan)[0]["start_at"] == "2026-10-15T09:20:00+08:00"
    assert plan["payload"]["usage"]["walking_distance_meters"] == 700


def test_known_walking_limit_chooses_safe_transit_and_checks_cumulative_amount():
    value = inputs()
    value["request"]["planning"]["max_walk_meters"] = 250
    plan = run(value, max_combinations=1)[0]
    assert [item["leg"]["mode"] for item in legs(plan)] == ["transit", "transit"]
    assert plan["payload"]["usage"]["walking_distance_meters"] == 200
    value["request"]["planning"]["max_walk_meters"] = 50
    plan = run(value, max_combinations=1)[0]
    assert "walking_limit" in codes(plan, "conflicts")


def test_missing_transit_walking_cannot_satisfy_a_hard_walk_limit():
    value = inputs(1, transport="transit")
    value["request"]["planning"]["max_walk_meters"] = 250
    plan = run(value, Routes(transit={"walking_distance_meters": None}))[0]
    assert "walking_unknown" in codes(plan)
    assert (
        not plan["payload"]["usage"]["walking_known"]
        and plan["status"] == "conditional"
    )


def test_return_and_specified_endpoint_are_real_routes_checked_against_deadline():
    value = inputs(1)
    value["request"]["planning"].update(
        return_to_origin=True, end_at="2026-10-15T09:55:00+08:00"
    )
    plan = run(value)[0]
    assert (
        len(legs(plan)) == 2
        and legs(plan)[-1]["leg"]["destination"] == value["origin"]["location"]
    )
    assert plan["status"] == "conflict" and "deadline_conflict" in codes(
        plan, "conflicts"
    )
    value = inputs()
    value["request"]["planning"]["end_candidate_id"] = "candidate-0"
    plan = run(value, max_combinations=1)[0]
    assert len(legs(plan)) == 3 and legs(plan)[-1]["to_candidate_id"] == "candidate-0"


def test_known_cost_excess_conflicts_and_unknown_cost_never_becomes_free():
    value = constraint(inputs(1, budget_cny=10), cost_cny=11, cost_source="user")
    assert "budget_conflict" in codes(run(value)[0], "conflicts")
    constraint(value, cost_cny=None, cost_source="unknown")
    plan = run(value, Routes(walking={"cost_cny": None}, transit={"cost_cny": None}))[0]
    assert {
        "activity_cost_unknown",
        "route_cost_unknown",
        "budget_unverified",
    } <= codes(plan)
    assert activities(plan)[0]["cost_cny"] is None


def test_meal_gaps_require_a_meal_at_the_corresponding_local_time():
    value = inputs(1)
    value["request"]["start_at"] = "2026-10-15T10:30:00+08:00"
    constraint(value, duration_minutes=120)
    plan = run(value)[0]
    assert [
        item["period"]
        for item in plan["payload"]["unknowns"]
        if item["code"] == "meal_gap"
    ] == ["lunch"]
    value["selected"][0]["category"] = "eat"
    assert "meal_gap" not in codes(run(value)[0])


def test_bounded_combinations_omit_only_optional_items_and_explain_gentle_choice():
    value = constraint(inputs(4), 3, required=True)
    results = run(value)
    assert len(results) == 3
    assert all(
        "candidate-3" in {item["candidate_id"] for item in activities(plan)}
        for plan in results
    )
    assert results[-1]["payload"]["diff"][0]["code"] == "gentle_selection"
    assert len(run(value, max_combinations=1)) == 1
    with pytest.raises(ValueError):
        run(value, max_combinations=4)


def test_twenty_activity_limit_keeps_mandatory_items_visible_and_partial():
    value = constraint(inputs(21, duration_minutes=800), 20, required=True)
    plan = run(value, max_combinations=1)[0]
    assert len(activities(plan)) == 20
    assert "candidate-20" in {item["candidate_id"] for item in activities(plan)}
    assert "activity_limit" in codes(plan) and plan["status"] != "validated"
    for index in range(21):
        constraint(value, index, required=True)
    plan = run(value)[0]
    assert plan["status"] == "partial" and "activity_limit" in codes(plan)


def test_budget_exhaustion_keeps_the_computed_prefix_and_stops_new_combinations():
    value = inputs(3)
    provider = Routes(budget=3)
    results = run(value, provider)
    assert len(results) == 1
    plan = results[0]
    assert plan["status"] == "partial" and "route_budget_exhausted" in codes(plan)
    assert len(activities(plan)) == 3 and len(legs(plan)) == 2
    assert activities(plan)[1]["end_at"] and activities(plan)[2]["start_at"] is None


def test_cancellation_propagates_and_does_not_produce_a_plan():
    class Cancelled:
        async def route(self, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(inputs(), Cancelled())


def test_completed_and_locked_without_parent_fail_before_queries():
    for change in ({"state": "completed"}, {"locked": True}):
        provider = Routes()
        plan = run(constraint(inputs(), **change), provider)[0]
        assert plan["status"] == "conflict" and "protected_snapshot_missing" in codes(
            plan, "conflicts"
        )
        assert not provider.calls


def test_completed_snapshot_is_retained_without_requerying_its_route():
    value = inputs()
    previous = run(value, max_combinations=1)[0]
    parent = {"trip_id": value["trip_id"], "payload": previous["payload"]}
    constraint(value, state="completed")
    value["request"]["start_at"] = "2026-10-15T10:00:00+08:00"
    value["origin"]["location"] = [120.11, 30.2]
    provider = Routes()
    plan = run(value, provider, parent_plan=parent)[0]
    assert activities(plan)[0] == {**activities(previous)[0], "state": "completed"}
    assert {call[2][0] for call in provider.calls} == {
        value["selected"][1]["poi"]["location"][0]
    }
    constraint(value, duration_minutes=90)
    assert "protected_activity_changed" in codes(
        run(value, parent_plan=parent)[0], "conflicts"
    )


def test_locked_parent_appointment_is_not_moved_to_hide_late_arrival():
    value = inputs(1)
    previous = run(value)[0]
    parent = {"trip_id": value["trip_id"], "payload": previous["payload"]}
    constraint(value, locked=True)
    value["request"]["start_at"] = "2026-10-15T10:00:00+08:00"
    result = run(value, parent_plan=parent)[0]
    assert result["status"] == "conflict" and "appointment_late" in codes(
        result, "conflicts"
    )
    assert activities(result)[0]["locked"]


def test_every_computed_step_has_aware_monotonic_times():
    value = constraint(inputs(2), fixed_start_at="2026-10-15T10:00:00+08:00")
    for plan in run(value):
        last = datetime.fromisoformat(value["request"]["start_at"])
        for item in plan["payload"]["itinerary"]:
            start = datetime.fromisoformat(item["start_at"])
            end = datetime.fromisoformat(item["end_at"])
            assert start.utcoffset() is not None and start >= last and end >= start
            last = end
        assert last <= datetime.fromisoformat(value["request"]["start_at"]) + timedelta(
            hours=6
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_seconds": -1},
        {"duration_seconds": float("nan")},
        {"duration_seconds": True},
        {"distance_meters": None},
        {"origin": [121, 31]},
        {"departure_at": "2026-10-15T10:00:00+08:00"},
        {"duration_source": "estimated"},
    ],
)
def test_invalid_or_unrelated_provider_routes_never_become_timed_itineraries(changes):
    provider = Routes(walking=changes)
    plan = run(inputs(1, transport="walking"), provider)[0]
    assert plan["status"] == "partial" and not legs(plan)
    assert activities(plan)[0]["start_at"] is None


def test_earliest_window_cannot_silently_move_a_fixed_appointment():
    value = constraint(
        inputs(1),
        fixed_start_at="2026-10-15T10:00:00+08:00",
        not_before="2026-10-15T11:00:00+08:00",
    )
    assert "appointment_late" in codes(run(value)[0], "conflicts")


def test_later_combination_budget_exhaustion_keeps_first_complete_suggestion():
    results = run(inputs(), Routes(budget=5))
    assert len(results) == 2 and results[0]["status"] == "conditional"
    assert len(legs(results[0])) == 2
    assert results[1]["status"] == "partial" and "route_budget_exhausted" in codes(
        results[1]
    )


def test_excluded_endpoint_and_all_disabled_defaults_are_known_conflicts():
    value = constraint(inputs(), excluded=True)
    value["request"]["planning"]["end_candidate_id"] = "candidate-0"
    assert "endpoint_excluded" in codes(run(value)[0], "conflicts")
    value = inputs()
    value["request"]["planning"]["disabled_modes"] = [
        "walking",
        "transit",
        "bicycling",
        "electrobike",
    ]
    provider = Routes()
    assert "transport_disabled" in codes(run(value, provider)[0], "conflicts")
    assert not provider.calls


def test_public_transit_fare_accounts_for_participants_as_a_labelled_estimate():
    value = inputs(1, transport="transit", participants=3, budget_cny=5)
    plan = run(value, Routes(transit={"cost_cny": 2}))[0]
    assert plan["payload"]["usage"]["estimated_cost_cny"] == 6
    assert {"route_fare_estimated", "budget_unverified"} <= codes(plan)
    fare = next(
        item
        for item in plan["payload"]["unknowns"]
        if item["code"] == "route_fare_estimated"
    )
    assert fare["participants"] == 3 and fare["per_person_cny"] == 2
    assert fare["estimated_total_cny"] == 6 and plan["status"] == "conditional"


def test_route_validity_is_the_earliest_selected_provider_expiration():
    class Expirations(Routes):
        async def route(self, **kwargs):
            result = await super().route(**kwargs)
            result["valid_until"] = (
                "2026-10-16T01:00:00+08:00"
                if len(self.calls) > 2
                else "2026-10-16T09:00:00+08:00"
            )
            return result

    assert (
        run(inputs(), Expirations(), max_combinations=1)[0]["payload"]["valid_until"]
        == "2026-10-16T01:00:00+08:00"
    )


def test_amap_origin_city_is_used_without_guessing_from_coordinates():
    value = inputs(1)
    value["origin"].update(source="amap", poi_id="origin-poi", city="杭州市")
    plan = run(value)[0]
    assert "origin_city_unknown" not in codes(plan) and plan["status"] == "conditional"
    value["origin"]["city"] = "上海市"
    provider = Routes()
    assert "cross_city" in codes(run(value, provider)[0], "conflicts")
    assert not provider.calls


def test_map_origin_city_is_only_available_after_explicit_user_confirmation():
    value = inputs(1)
    assert "origin_city_unknown" in codes(run(value)[0])
    value["request"]["planning"]["origin_city"] = "杭州"
    assert "origin_city_unknown" not in codes(run(value)[0])
    value["request"]["planning"]["origin_city"] = "苏州市"
    provider = Routes()
    assert "cross_city" in codes(run(value, provider)[0], "conflicts")
    assert not provider.calls


def test_provider_and_user_origin_city_conflict_requires_correction():
    value = inputs(1)
    value["origin"].update(source="amap", poi_id="origin-poi", city="杭州市")
    value["request"]["planning"]["origin_city"] = "上海"
    provider = Routes()
    plan = run(value, provider)[0]
    assert plan["status"] == "conflict" and "origin_city_conflict" in codes(
        plan, "conflicts"
    )
    assert not provider.calls
