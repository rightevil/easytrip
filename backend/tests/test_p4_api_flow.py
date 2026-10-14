import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_discovery_pipeline import setup
from test_places import raw_poi
from test_planning_runner import response as walking_response
from test_routes import district_payload

from easytrip.agent import discovery
from easytrip.main import create_app


def test_new_trip_discovers_maps_supplements_plans_adopts_and_reopens(
    tmp_path, monkeypatch
):
    _, _, runner, mcp, models = setup(tmp_path, monkeypatch)
    model_client = discovery.direct_client
    original_responder = mcp.responder

    def source_response(name, arguments):
        if name == "search_feeds" and "午餐" in arguments["keyword"]:
            arguments = {**arguments, "keyword": "杭州餐饮"}
        return original_responder(name, arguments)

    mcp.responder = source_response
    amap_calls = []

    @asynccontextmanager
    async def external_client():
        async with model_client() as model:

            async def external_response(request):
                if request.url.host == "model.invalid":
                    return await model.send(request)
                assert request.url.host == "restapi.amap.com"
                amap_calls.append(request)
                path = request.url.path
                if "/place/" in path:
                    meal = request.url.params.get("keywords") == "阿婆饭店" or (
                        request.url.params.get("id") == "meal-poi"
                    )
                    poi = (
                        raw_poi(
                            "meal-poi",
                            "阿婆饭店",
                            location="120.151,30.251",
                            type="餐饮服务;中餐厅",
                        )
                        if meal
                        else raw_poi()
                    )
                    return httpx.Response(
                        200, json={"status": "1", "count": "1", "pois": [poi]}
                    )
                if path == "/v3/config/district":
                    return httpx.Response(200, json=district_payload())
                if "transit" in path:
                    return httpx.Response(
                        200, json={"status": "1", "route": {"transits": []}}
                    )
                assert "direction" in path
                return walking_response(request)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(external_response), trust_env=False
            ) as client:
                yield client

    async def no_delay(_):
        pass

    monkeypatch.setattr("easytrip.main.direct_client", external_client)
    monkeypatch.setattr("easytrip.main._before_amap_request", no_delay)
    monkeypatch.setattr("easytrip.api.places._before_amap_request", no_delay)
    settings = replace(runner.settings, amap_web_key="test-web-key")
    # Only protocol fixtures are reused; the actual app starts from a separate empty DB.
    path = tmp_path / "new-trip.sqlite3"

    def post(client, url, body, status=200):
        response = client.post(url, json=body)
        assert response.status_code == status, response.text
        return response.json()

    def run_to_completion(client, trip, kind):
        queued = post(
            client,
            f"/api/trips/{trip['id']}/runs",
            {
                "operation_id": str(uuid4()),
                "expected_version": trip["request_version"],
                "kind": kind,
                "budget": {
                    "target_notes": 1,
                    "max_images": 0,
                    "max_searches": 1,
                    "reserved_gap_searches": 0,
                    "max_model_calls": 2,
                    "max_tokens": 100000,
                    "max_seconds": 30,
                    **({"max_combinations": 1} if kind == "plan" else {}),
                },
            },
            201,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            run = client.get(
                f"/api/runs/{queued['id']}", params={"trip_id": trip["id"]}
            ).json()
            if run["status"] not in {"pending", "running"}:
                break
            time.sleep(0.01)
        assert run["status"] == "completed", run
        assert run["usage"]["unknown_result_calls"] == 0
        return run

    with TestClient(
        create_app(settings, tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        trip = post(
            client,
            "/api/trips",
            {"operation_id": str(uuid4()), "request": {"destination": "杭州"}},
            201,
        )
        url = f"/api/trips/{trip['id']}"
        found = run_to_completion(client, trip, "discover")
        candidate = next(
            row
            for row in client.get(url + "/candidates").json()["items"]
            if row["kind"] == "place" and row["category"] == "play"
        )
        trip = client.get(url).json()

        def versions():
            current = client.get(url).json()
            return {
                "expected_version": current["request_version"],
                "expected_workspace_version": current["workspace_version"],
            }

        located = post(client, url + "/places/match", versions())
        assert located["matches"][0]["status"] == "unique"
        post(
            client,
            url + "/places/map",
            {**versions(), "action": "select", "candidate_id": candidate["id"]},
        )
        post(
            client,
            url + "/places/origin/confirm",
            {
                **versions(),
                "source": "map",
                "name": "验收给定出发点",
                "location": [120.14, 30.24],
            },
        )
        trip = client.get(url).json()
        request = trip["request"]
        request["start_at"] = (
            (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1))
            .replace(hour=11, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        request["duration_minutes"] = 300
        request["planning"]["origin_city"] = "杭州市"
        request["planning"]["activity_constraints"] = [
            {
                "candidate_id": candidate["id"],
                "duration_minutes": 60,
                "duration_source": "user",
            }
        ]
        updated = client.patch(
            url,
            json={"expected_version": trip["request_version"], "request": request},
        )
        assert updated.status_code == 200, updated.text
        trip = updated.json()
        assert trip["request"]["transport"] == "mixed"
        assert client.get(url + "/planning-input").json()["missing"] == []
        before = (len(mcp.calls), len(models), len(amap_calls))
        planned = run_to_completion(client, trip, "plan")
        plans = client.get(url + "/plans").json()
        assert plans["active_plan_id"] is None
        plan = plans["items"][0]
        assert plan["status"] == "conditional"
        payload = plan["payload"]
        assert payload["conflicts"] == []
        meal = payload["supplements"][0]
        assert meal["name"] == "阿婆饭店" and meal["poi"]["id"] == "meal-poi"
        assert meal["constraint"]["duration_source"] == "estimated"
        assert any(
            row.get("candidate_id") == meal["candidate_id"] and row.get("start_at")
            for row in payload["itinerary"]
        )
        assert "meal_gap" not in {row["code"] for row in payload["unknowns"]}
        usage = planned["usage"]
        assert usage["notes_analyzed"] == usage["notes_obtained"] == 1
        assert usage["comments_analyzed"] == usage["comments_judged"] == 2
        assert usage["searches"] == usage["model_calls"] == 1
        assert usage["poi_calls"] == 2 and usage["route_calls"] >= 2
        assert usage["amap_calls"] == len(amap_calls) - before[2]
        assert usage["external_calls"] == sum(
            after - prior
            for after, prior in zip(
                (len(mcp.calls), len(models), len(amap_calls)), before
            )
        )
        meal_sources = client.get(
            url + "/sources", params={"run_id": planned["id"]}
        ).json()["items"]
        assert any(row["kind"] == "note" for row in meal_sources)
        evidence = client.get(
            url + f"/candidates/{meal['candidate_id']}/evidence"
        ).json()
        assert evidence["items"] and all(
            row["quote"] in row["source"]["text"] for row in evidence["items"]
        )
        adopted = post(
            client,
            url + f"/plans/{plan['id']}/adopt",
            {
                "operation_id": str(uuid4()),
                "expected_version": trip["request_version"],
                "input_hash": plan["input_hash"],
            },
        )
        assert adopted["trip"]["active_plan_id"] == plan["id"]
        saved_counts = (len(mcp.calls), len(models), len(amap_calls))

    with TestClient(
        create_app(settings, tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        reopened = client.get(url).json()
        assert reopened["active_plan_id"] == plan["id"]
        assert reopened["workspace"]["selected_ids"] == [candidate["id"]]
        assert candidate["id"] in reopened["workspace"]["map_visible_ids"]
        assert client.get(url + f"/plans/{plan['id']}").json() == plan
        restored_places = client.get(
            url + "/places", params={"run_id": found["id"]}
        ).json()
        assert restored_places["run_id"] == found["id"]
        assert restored_places["candidates"][0]["id"] == candidate["id"]
        assert saved_counts == (len(mcp.calls), len(models), len(amap_calls))
