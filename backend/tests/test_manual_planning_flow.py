import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from test_places import raw_poi
from test_planning_runner import response as walking_response
from test_routes import district_payload

from easytrip.config import Settings
from easytrip.main import create_app


def test_manual_places_plan_adopt_reopen_and_adjust_without_discovery(
    tmp_path, monkeypatch
):
    calls = []
    pois = [
        raw_poi("park-one", "太子湾公园"),
        raw_poi("park-two", "花港观鱼", location="120.151,30.251"),
    ]

    def provider(request):
        assert request.url.host == "restapi.amap.com"
        calls.append(request)
        if "/place/" in request.url.path:
            identifier = request.url.params.get("id")
            name = request.url.params.get("keywords")
            poi = next(
                row for row in pois if row["id"] == identifier or row["name"] == name
            )
            return httpx.Response(
                200, json={"status": "1", "count": "1", "pois": [poi]}
            )
        if request.url.path == "/v3/config/district":
            return httpx.Response(200, json=district_payload())
        if "transit" in request.url.path:
            return httpx.Response(200, json={"status": "1", "route": {"transits": []}})
        assert "direction" in request.url.path
        return walking_response(request)

    async def no_delay(_):
        pass

    monkeypatch.setattr(
        "easytrip.main.direct_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(provider), trust_env=False
        ),
    )
    monkeypatch.setattr("easytrip.main._before_amap_request", no_delay)
    monkeypatch.setattr("easytrip.api.places._before_amap_request", no_delay)
    settings = Settings(amap_web_key="manual-flow-key")
    path = tmp_path / "manual-flow.sqlite3"

    def post(client, url, body, status=200):
        response = client.post(url, json=body)
        assert response.status_code == status, response.text
        return response.json()

    def versions(client, url):
        trip = client.get(url).json()
        return {
            "expected_version": trip["request_version"],
            "expected_workspace_version": trip["workspace_version"],
        }

    def plan(client, url, parent=None):
        trip = client.get(url).json()
        pending = post(
            client,
            url + "/runs",
            {
                "operation_id": str(uuid4()),
                "expected_version": trip["request_version"],
                "kind": "plan",
                "parent_plan_id": parent,
                "budget": {"max_combinations": 1, "max_seconds": 30},
            },
            201,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            run = client.get(
                f"/api/runs/{pending['id']}", params={"trip_id": trip["id"]}
            ).json()
            if run["status"] not in {"pending", "running"}:
                break
            time.sleep(0.01)
        assert run["status"] == "completed", run
        assert run["usage"]["model_calls"] == run["usage"]["searches"] == 0
        assert run["usage"]["unknown_result_calls"] == 0
        saved = client.get(url + "/plans").json()["items"][0]
        assert saved["status"] == "conditional" and saved["payload"]["conflicts"] == []
        return saved, run

    def adopt(client, url, saved):
        trip = client.get(url).json()
        return post(
            client,
            url + f"/plans/{saved['id']}/adopt",
            {
                "operation_id": str(uuid4()),
                "expected_version": trip["request_version"],
                "input_hash": saved["input_hash"],
            },
        )

    with TestClient(
        create_app(settings, tmp_path, database_path=path), base_url="http://127.0.0.1"
    ) as client:
        trip = post(
            client,
            "/api/trips",
            {"operation_id": str(uuid4()), "request": {"destination": "杭州"}},
            201,
        )
        url = f"/api/trips/{trip['id']}"
        identifiers = []
        for poi in pois:
            searched = client.get(
                url + "/places/manual/search",
                params={"expected_version": 1, "city": "杭州", "query": poi["name"]},
            )
            assert searched.status_code == 200, searched.text
            body = {
                **versions(client, url),
                "operation_id": str(uuid4()),
                "search_id": searched.json()["search_id"],
                "poi_id": poi["id"],
                "category": "play",
            }
            saved = post(client, url + "/places/manual/confirm", body)
            identifiers.append(saved["candidate_id"])
            count = len(calls)
            repeated = post(client, url + "/places/manual/confirm", body)
            assert (
                repeated["candidate_id"] == saved["candidate_id"]
                and len(calls) == count
            )
        snapshot = client.get(url + "/places").json()
        assert snapshot["run_id"] is None and snapshot["candidates"] == []
        assert len(snapshot["manual_candidates"]) == 2
        assert snapshot["trip"]["runs"] == []
        assert snapshot["trip"]["workspace"]["selected_ids"] == []
        assert snapshot["trip"]["workspace"]["map_visible_ids"] == []
        for identifier in identifiers:
            post(
                client,
                url + "/places/map",
                {**versions(client, url), "action": "add", "candidate_id": identifier},
            )
        assert client.get(url).json()["workspace"]["selected_ids"] == []
        post(
            client,
            url + "/places/map",
            {
                **versions(client, url),
                "action": "select",
                "candidate_id": identifiers[0],
            },
        )
        post(
            client,
            url + "/places/origin/confirm",
            {
                **versions(client, url),
                "source": "map",
                "name": "用户给定起点",
                "location": [120.14, 30.24],
            },
        )
        trip = client.get(url).json()
        request = trip["request"]
        start = (
            datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
        ).replace(hour=9, minute=0, second=0, microsecond=0)
        request.update(start_at=start.isoformat(), duration_minutes=90)
        request["planning"]["origin_city"] = "杭州市"
        request["planning"]["activity_constraints"] = [
            {
                "candidate_id": identifiers[0],
                "duration_minutes": 20,
                "duration_source": "user",
            }
        ]
        patched = client.patch(
            url, json={"expected_version": trip["request_version"], "request": request}
        )
        assert patched.status_code == 200, patched.text
        first, _ = plan(client, url)
        adopted = adopt(client, url, first)
        assert adopted["trip"]["active_plan_id"] == first["id"]
        # Adding another saved user candidate changes selection, not the old plan snapshot.
        post(
            client,
            url + "/places/map",
            {
                **versions(client, url),
                "action": "select",
                "candidate_id": identifiers[1],
            },
        )
        stale = client.post(
            url + f"/plans/{first['id']}/adopt",
            json={
                "operation_id": str(uuid4()),
                "expected_version": adopted["trip"]["request_version"],
                "input_hash": first["input_hash"],
            },
        )
        assert stale.status_code == 409
        trip = client.get(url).json()
        request = trip["request"]
        request["planning"]["activity_constraints"].append(
            {
                "candidate_id": identifiers[1],
                "duration_minutes": 20,
                "duration_source": "user",
            }
        )
        assert (
            client.patch(
                url,
                json={"expected_version": trip["request_version"], "request": request},
            ).status_code
            == 200
        )
        second, _ = plan(client, url, first["id"])
        assert len(second["payload"]["input"]["selected"]) == 2
        for place in second["payload"]["input"]["selected"]:
            assert place["source"] == "user" and place["run_id"] is None
            assert (
                place["sources"]
                == place["evidence"]
                == place["mentions"]
                == place["activities"]
                == []
            )
        assert adopt(client, url, second)["trip"]["active_plan_id"] == second["id"]
        count_before_restart = len(calls)

    with TestClient(
        create_app(settings, tmp_path, database_path=path), base_url="http://127.0.0.1"
    ) as client:
        trip = client.get(url).json()
        assert (
            trip["active_plan_id"] == second["id"]
            and len(calls) == count_before_restart
        )
        assert trip["workspace"]["selected_ids"] == identifiers
        assert len(client.get(url + "/places").json()["manual_candidates"]) == 2
        assert all(run["kind"] == "plan" for run in trip["runs"])
        request = trip["request"]
        request["planning"]["activity_constraints"][0]["state"] = "completed"
        request["planning"]["activity_constraints"][1]["duration_minutes"] = 15
        request["planning"]["progress_at"] = (start + timedelta(minutes=45)).isoformat()
        request["planning"]["progress_origin"] = {
            "name": "用户给定当前位置",
            "location": [120.15, 30.25],
            "source": "map",
            "city": "杭州市",
        }
        response = client.patch(
            url, json={"expected_version": trip["request_version"], "request": request}
        )
        assert response.status_code == 200, response.text
        adjusted, run = plan(client, url, second["id"])
        assert run["usage"]["route_calls"] <= 2
        for previous, current in zip(
            second["payload"]["itinerary"][:3], adjusted["payload"]["itinerary"][:3]
        ):
            assert {
                k: v
                for k, v in previous.items()
                if k not in {"historical", "state", "locked", "required"}
            } == {
                k: v
                for k, v in current.items()
                if k not in {"historical", "state", "locked", "required"}
            }
        assert adopt(client, url, adjusted)["trip"]["active_plan_id"] == adjusted["id"]
        assert client.get(url + f"/plans/{second['id']}").json() == second
        assert len(client.get(url + "/places").json()["manual_candidates"]) == 2
