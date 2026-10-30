import asyncio
from copy import deepcopy

import pytest
from test_day_planning import multiday
from test_day_planning import run as schedule
from test_meals import MockAmap, install_sources
from test_meals import runtime as meal_runtime

from easytrip.services.lodgings import lodging_queries, supplement_lodgings
from easytrip.services.meals import _supplement_places, meal_queries

runtime = meal_runtime


class Hotels(MockAmap):
    def poi(self, name, identifier):
        return {**super().poi(name, identifier), "type": "住宿服务;宾馆酒店"}


def missing_nights():
    frozen = multiday()
    frozen["request"]["planning"]["overnight_stays"] = []
    return frozen


def test_lodging_queries_bind_missing_nights_to_real_regions_without_binding_a_hotel():
    value = missing_nights()
    suggestions = [schedule(value)]
    queries = lodging_queries(value, suggestions)
    assert [item["date"] for item in queries] == ["2026-10-15", "2026-10-16"]
    assert queries[0]["anchor_candidate_id"] == "candidate-0"
    assert queries[1]["anchor_candidate_id"] == "candidate-1"
    assert all(
        item["purpose"] == "lodging_gap" and item["city"] == "杭州市"
        for item in queries
    )
    assert value["request"]["planning"]["overnight_stays"] == []


def test_meal_contexts_do_not_borrow_the_previous_days_activity():
    value = missing_nights()
    suggestions = [schedule(value)]
    for suggestion in suggestions:
        suggestion["payload"]["unknowns"] = [
            {
                "code": "meal_gap",
                "period": "lunch",
                "window_start": "2026-10-16T11:00:00+08:00",
                "window_end": "2026-10-16T14:00:00+08:00",
            },
            {
                "code": "meal_gap",
                "period": "lunch",
                "window_start": "2026-10-17T11:00:00+08:00",
                "window_end": "2026-10-17T14:00:00+08:00",
            },
        ]
    contexts = meal_queries(value, suggestions)
    # Unscheduled next-day activities must not be replaced by a prior day's location.
    assert contexts == []


@pytest.mark.parametrize("ambiguous", [False, True])
def test_lodging_supplement_keeps_sources_comments_and_selected_poi_without_choosing_nights(
    runtime, monkeypatch, ambiguous
):
    runner, task = runtime
    calls = install_sources(monkeypatch, category="stay", comment_evidence=True)
    value = missing_nights()
    original = deepcopy(value)
    contexts = lodging_queries(value, [schedule(value)])
    result = asyncio.run(
        _supplement_places(runner, task, None, value, Hotels(ambiguous=ambiguous), contexts, "stay")
    )
    assert len(result["candidates"]) == 2 and not result["gaps"]
    assert calls["models"] == 2 and sum(calls["comment_limits"]) == 5
    for item, context in zip(result["candidates"], contexts):
        assert item["category"] == "stay" and "constraint" not in item
        assert item["lodging_window"]["date"] == context["date"]
        assert item["sources"]
        with runner.database.connection() as connection:
            match = runner.database._place_match(
                connection, task["trip_id"], item["candidate_id"]
            )
            assert match.status == "unique" and match.poi.id == item["poi"]["id"]
            assert len(match.options) == (2 if ambiguous else 1)
    assert value == original
    assert result["candidates"][0]["evidence"]
    assert not result["candidates"][1][
        "evidence"
    ]  # The shared five-comment allowance is spent on the first note.
    again = asyncio.run(
        _supplement_places(runner, task, None, value, Hotels(), contexts, "stay")
    )
    assert again == result and calls["models"] == 2


@pytest.mark.parametrize(
    "options",
    [{"wrong_city": True}, {"far": True}, {"wrong_detail": True}, {"exhausted": True}],
)
def test_lodging_failures_keep_dated_gaps(runtime, monkeypatch, options):
    runner, task = runtime
    install_sources(monkeypatch, category="stay")
    value = missing_nights()
    contexts = lodging_queries(value, [schedule(value)])
    result = asyncio.run(
        _supplement_places(
            runner, task, None, value, Hotels(**options), contexts, "stay"
        )
    )
    assert not result["candidates"]
    assert {gap["date"] for gap in result["gaps"]} == {"2026-10-15", "2026-10-16"}


def test_existing_local_hotel_is_offered_per_night_without_source_or_model_requests(
    runtime, monkeypatch
):
    runner, task = runtime
    value = missing_nights()
    value["lodgings"][0]["poi"].update(location=[120.1, 30.2], address="具体分店地址")
    value["lodgings"][1]["poi"].update(location=[120.11, 30.2], address="另一分店地址")
    calls = install_sources(monkeypatch, category="stay")
    original_db = runner.db

    async def db(method, *args, **kwargs):
        if method == "planning_input":
            return {**value, "lodging_candidates": value["lodgings"]}
        return await original_db(method, *args, **kwargs)

    monkeypatch.setattr(runner, "db", db)
    result = asyncio.run(
        supplement_lodgings(runner, task, None, value, [schedule(value)], MockAmap())
    )
    assert len(result["candidates"]) == 2
    assert calls["models"] == 0 and calls["queries"] == []
    assert value["request"]["planning"]["overnight_stays"] == []


def test_lodgings_and_meals_share_source_and_model_allowances(runtime, monkeypatch):
    from test_meals import frozen_input, suggestions

    runner, task = runtime
    calls = install_sources(monkeypatch, category="stay")
    value = missing_nights()
    contexts = lodging_queries(value, [schedule(value)])
    first = asyncio.run(
        _supplement_places(runner, task, None, value, Hotels(), contexts, "stay")
    )
    assert len(first["candidates"]) == 2
    second = asyncio.run(
        _supplement_places(
            runner,
            task,
            None,
            frozen_input(),
            Hotels(),
            meal_queries(frozen_input(), suggestions()),
            "eat",
        )
    )
    assert not second["candidates"] and calls["models"] == len(calls["queries"]) == 2
    assert second["gaps"][0]["reason"] == "budget_exhausted"


def test_unknown_lodging_model_result_is_never_replayed(runtime, monkeypatch):
    runner, task = runtime
    calls = install_sources(monkeypatch, category="stay", model_unknown=True)
    value = missing_nights()
    contexts = lodging_queries(value, [schedule(value)])
    first = asyncio.run(
        _supplement_places(runner, task, None, value, Hotels(), contexts, "stay")
    )
    assert not first["candidates"] and calls["models"] == 1
    again = asyncio.run(
        _supplement_places(runner, task, None, value, Hotels(), contexts, "stay")
    )
    assert again == first and calls["models"] == 1


def test_unknown_overnight_region_does_not_trigger_citywide_search(
    runtime, monkeypatch
):
    runner, task = runtime
    calls = install_sources(monkeypatch, category="stay")
    value = missing_nights()
    suggestion = schedule(value)
    for day in suggestion["payload"]["days"]:
        day["itinerary"] = []
        day["origin"] = None
    result = asyncio.run(
        supplement_lodgings(runner, task, None, value, [suggestion], Hotels())
    )
    assert {gap["date"] for gap in result["gaps"]} == {"2026-10-15", "2026-10-16"}
    assert all(gap["code"] == "lodging_region_unknown" for gap in result["gaps"])
    assert not calls["queries"] and calls["models"] == 0
