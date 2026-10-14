import asyncio
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from test_planning import ready

from easytrip.agent.planning import PlanningRunner
from easytrip.config import Settings
from easytrip.domain.models import (
    ActivityConstraint,
    TripPatch,
    TripRequest,
    WorkspacePatch,
)
from easytrip.domain.planning import AdoptPlan
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database, OperationConflict


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "runner.sqlite3")
    database.initialize()
    return database


def enqueue(database, budget=None):
    trip, identifier = ready(database)
    request = TripRequest.model_validate(trip["request"])
    request.start_at = datetime.combine(
        datetime.now(timezone.utc).date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    ) + timedelta(hours=1)
    request.duration_minutes = 180
    request.transport = "walking"
    request.transport_source = "user"
    request.planning.origin_city = "杭州市"
    request.planning.activity_constraints = [
        ActivityConstraint(
            candidate_id=identifier,
            duration_minutes=60,
            duration_source="user",
            cost_cny=0,
            cost_source="user",
        )
    ]
    request = TripRequest.model_validate(request.model_dump())
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=trip["request_version"], request=request)
    )
    body = CreateRun(
        operation_id=uuid4(),
        expected_version=trip["request_version"],
        kind="plan",
        budget=budget or {},
    )
    return trip, body, database.enqueue_run(trip["id"], body)


def response(request):
    origin, destination = (
        request.url.params["origin"],
        request.url.params["destination"],
    )
    return httpx.Response(
        200,
        json={
            "status": "1",
            "route": {
                "paths": [
                    {
                        "distance": "1500",
                        "cost": {"duration": "600"},
                        "steps": [{"polyline": f"{origin};{destination}"}],
                    }
                ]
            },
        },
    )


def test_runs_real_normalizer_with_mock_transport_saves_and_adopts(database):
    async def exercise():
        trip, body, pending = enqueue(database)
        assert database.enqueue_run(trip["id"], body)["id"] == pending["id"]
        run = database.claim_run()
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        saved = database.get_run(run["id"])
        assert saved["status"] == "completed"
        assert saved["usage"]["amap_calls"] == saved["usage"]["route_calls"] == 1
        assert saved["usage"]["model_tokens"] == 0
        actions = database.list_actions(run["id"])
        assert len(actions) == 1 and actions[0]["status"] == "completed"
        plans = database.list_plans(trip["id"])["items"]
        assert plans and plans[0]["payload"]["itinerary"][0]["kind"] == "route"
        assert plans[0]["status"] == "conditional"
        assert database.get_trip(trip["id"])["active_plan_id"] is None
        plan = plans[0]
        adopted = database.adopt_plan(
            trip["id"],
            plan["id"],
            AdoptPlan(
                operation_id=uuid4(),
                expected_version=trip["request_version"],
                input_hash=plan["input_hash"],
            ),
        )
        assert adopted["trip"]["active_plan_id"] == plan["id"]

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout", [False, True])
def test_meal_failure_retains_routes_and_truthful_gap(database, monkeypatch, timeout):
    async def exercise():
        trip, _, pending = enqueue(database)
        database.cancel_run(pending["id"], trip["id"], pending["generation"])
        request = TripRequest.model_validate(trip["request"])
        request.start_at += timedelta(hours=2)
        trip_updated = database.update_trip(
            trip["id"],
            TripPatch(expected_version=trip["request_version"], request=request),
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=trip_updated["request_version"],
                kind="plan",
            ),
        )
        run = database.claim_run()
        if timeout:

            async def failed_lookup(*args):
                raise TimeoutError()

            monkeypatch.setattr(
                "easytrip.agent.planning.supplement_meals", failed_lookup
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        result = database.get_run(run["id"])
        assert result["status"] == "completed"
        plans = database.list_plans(trip["id"])["items"]
        assert plans and plans[0]["status"] == "conditional"
        payload = plans[0]["payload"]
        assert any(item["kind"] == "route" for item in payload["itinerary"])
        assert payload["supplements"] == []
        codes = {issue["code"] for issue in payload["unknowns"]}
        assert "meal_gap" in codes
        assert (
            "meal_lookup_timeout" if timeout else "meal_supplement_unfilled"
        ) in codes
        assert result["usage"]["model_calls"] == result["usage"]["searches"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("max_routes", [2, 40])
def test_meal_candidate_is_revalidated_without_losing_initial_routes(
    database, monkeypatch, max_routes
):
    async def lookup(runner, run, client, frozen, suggestions, amap):
        await amap.search_candidates("模拟补查餐馆", "杭州")
        meal = deepcopy(frozen["selected"][0])
        meal.update(candidate_id="mock-meal", name="模拟补查餐馆", category="eat")
        meal["poi"].update(
            id="mock-meal-poi", name=meal["name"], location=[120.16, 30.25]
        )
        meal["meal_window"] = {
            "period": "lunch",
            "start_at": frozen["request"]["start_at"],
            "end_at": (
                datetime.fromisoformat(frozen["request"]["start_at"])
                + timedelta(hours=3)
            ).isoformat(),
            "anchor_candidate_id": frozen["selected"][0]["candidate_id"],
        }
        meal["constraint"] = {
            "candidate_id": "mock-meal",
            "duration_minutes": 45,
            "duration_source": "estimated",
            "not_before": meal["meal_window"]["start_at"],
            "not_after": meal["meal_window"]["end_at"],
        }
        return {"candidates": [meal], "queries": [], "gaps": []}

    async def exercise():
        trip, _, pending = enqueue(database)
        database.cancel_run(pending["id"], trip["id"], pending["generation"])
        request = TripRequest.model_validate(trip["request"])
        request.start_at += timedelta(hours=2)
        request.duration_minutes = 300
        trip = database.update_trip(
            trip["id"],
            TripPatch(expected_version=trip["request_version"], request=request),
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=trip["request_version"],
                kind="plan",
                budget={"max_routes": max_routes, "max_combinations": 1},
            ),
        )
        run = database.claim_run()
        monkeypatch.setattr("easytrip.agent.planning.supplement_meals", lookup)

        def handler(request):
            return (
                httpx.Response(200, json={"status": "1", "count": "0", "pois": []})
                if "/place/" in request.url.path
                else response(request)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        saved = database.get_run(run["id"])
        assert saved["status"] == "completed"
        assert saved["usage"]["poi_calls"] == 1
        payload = database.list_plans(trip["id"])["items"][0]["payload"]
        assert any(item["kind"] == "route" for item in payload["itinerary"])
        assert payload["supplements"][0]["candidate_id"] == "mock-meal"
        if max_routes == 2:
            assert {item["code"] for item in payload["unknowns"]} >= {
                "meal_gap",
                "meal_route_uncomputed",
            }
            assert not any(
                item.get("candidate_id") == "mock-meal" for item in payload["itinerary"]
            )
        else:
            assert any(
                item.get("candidate_id") == "mock-meal"
                and item.get("start_at")
                and item.get("end_at")
                for item in payload["itinerary"]
            )

    asyncio.run(exercise())


def test_changed_selection_after_http_does_not_publish_or_adopt(database):
    async def exercise():
        trip, _, _ = enqueue(database)
        run = database.claim_run()

        async def handler(request):
            current = database.get_trip(trip["id"])
            from easytrip.domain.places import MapAction

            database.apply_map_action(
                trip["id"],
                MapAction(
                    expected_version=current["request_version"],
                    expected_workspace_version=current["workspace_version"],
                    action="unselect",
                    candidate_id=current["workspace"]["selected_ids"][0],
                ),
            )
            return response(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        assert database.get_run(run["id"])["status"] == "stale"
        assert database.list_plans(trip["id"])["items"] == []
        assert database.get_trip(trip["id"])["active_plan_id"] is None

    asyncio.run(exercise())


def test_cancellation_retains_unknown_call_without_retry(database):
    async def exercise():
        trip, _, _ = enqueue(database)
        run = database.claim_run()
        called = asyncio.Event()

        async def handler(_request):
            called.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(
                PlanningRunner(
                    database, Settings(amap_web_key="test-key"), client=client
                ).execute(run)
            )
            await asyncio.wait_for(called.wait(), 2)
            database.cancel_run(run["id"], trip["id"], run["generation"])
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        cancelled = database.get_run(run["id"])
        assert cancelled["status"] == "cancelled"
        assert cancelled["usage"]["unknown_result_calls"] == 1
        assert database.list_actions(run["id"])[0]["status"] == "unknown"
        with pytest.raises(OperationConflict):
            database.resume_run(run["id"], trip["id"], cancelled["generation"])

    asyncio.run(exercise())


def test_viewport_does_not_stale_run_and_restart_keeps_frozen_input(database):
    trip, _, pending = enqueue(database)
    workspace = trip["workspace"] | {"map_zoom": 15}
    database.save_workspace(
        trip["id"],
        WorkspacePatch(expected_version=trip["workspace_version"], workspace=workspace),
    )
    run = database.claim_run()
    assert run["id"] == pending["id"]
    database.recover_runs()
    recovered = database.get_run(run["id"])
    assert recovered["params"]["input_hash"] == run["params"]["input_hash"]
    database.resume_run(run["id"], trip["id"], recovered["generation"])
    assert database.claim_run()["id"] == run["id"]


def test_publication_resume_uses_same_snapshot_without_duplicate_plan(database):
    async def exercise():
        trip, _, _ = enqueue(database)
        run = database.claim_run()
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        saved = database.get_run(run["id"])
        checkpoint = saved["checkpoint"]
        checkpoint["completed_stages"] = ["planning_input", "routing"]
        checkpoint["scheduled"] = None
        with database.connection(write=True) as connection:
            connection.execute(
                "UPDATE runs SET status='recoverable',stage='scheduled',checkpoint_json=? WHERE id=?",
                (json.dumps(checkpoint), run["id"]),
            )
        database.resume_run(run["id"], trip["id"], saved["generation"])
        resumed = database.claim_run()
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(resumed)
        assert database.get_run(run["id"])["status"] == "completed"
        assert len(database.list_plans(trip["id"])["items"]) == 1

    asyncio.run(exercise())


def test_cached_resume_never_refetches_and_keeps_snapshot_usage(database):
    async def exercise():
        trip, _, _ = enqueue(database)
        run = database.claim_run()
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        first = database.get_run(run["id"])
        parent = database.list_plans(trip["id"])["items"][0]
        next_run = database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=trip["request_version"],
                kind="plan",
                parent_plan_id=parent["id"],
            ),
        )
        run = database.claim_run()

        async def forbidden(_request):
            raise AssertionError("cache should avoid network")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(forbidden)
        ) as client:
            await PlanningRunner(
                database, Settings(amap_web_key="test-key"), client=client
            ).execute(run)
        second = database.get_run(next_run["id"])
        assert first["usage"]["amap_calls"] == 1 and second["usage"]["amap_calls"] == 0
        assert second["usage"]["cache_hits"] == 0
        assert (
            database.list_plans(trip["id"])["items"][0]["payload"]["usage"][
                "reused_route_count"
            ]
            == 1
        )

    asyncio.run(exercise())
