"""P5 existing-hotel flows through the real API and mocked provider transport."""

import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_places import raw_poi
from test_planning_runner import response as walking_response
from test_routes import district_payload

from easytrip.config import Settings
from easytrip.main import create_app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    calls = []
    pois = [
        raw_poi(
            f"park-{index}", f"第{index}天公园", location=f"120.15{index},30.25{index}"
        )
        for index in range(1, 4)
    ] + [
        raw_poi(
            f"hotel-{index}",
            f"同名酒店第{index}分店",
            type="住宿服务;宾馆酒店",
            location=f"120.16{index},30.26{index}",
            address=f"杭州第{index}活动区域",
        )
        for index in range(1, 3)
    ]

    def provider(request):
        assert request.url.host == "restapi.amap.com"
        calls.append(request)
        if "/place/" in request.url.path:
            identifier = request.url.params.get("id")
            query = request.url.params.get("keywords")
            poi = next(
                row for row in pois if row["id"] == identifier or row["name"] == query
            )
            return httpx.Response(
                200, json={"status": "1", "count": "1", "pois": [poi]}
            )
        if request.url.path == "/v3/config/district":
            return httpx.Response(200, json=district_payload())
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
    settings = Settings(amap_web_key="mock-p5-key", secrets={"mock-p5-key"})
    path = tmp_path / "p5.sqlite3"

    def open_client():
        return TestClient(
            create_app(settings, tmp_path, database_path=path),
            base_url="http://127.0.0.1",
        )

    return open_client, pois, calls


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


def patch_request(client, url, request):
    response = client.patch(
        url,
        json={
            "expected_version": versions(client, url)["expected_version"],
            "request": request,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, pois, count, *, missing_last_hotel=False):
    trip = post(client, "/api/trips", {"operation_id": str(uuid4())}, 201)
    url = f"/api/trips/{trip['id']}"
    identifiers = {}
    for poi in pois:
        result = client.get(
            url + "/places/manual/search",
            params={
                "expected_version": 1,
                "city": "杭州",
                "query": poi["name"],
            },
        )
        assert result.status_code == 200, result.text
        saved = post(
            client,
            url + "/places/manual/confirm",
            {
                **versions(client, url),
                "operation_id": str(uuid4()),
                "search_id": result.json()["search_id"],
                "poi_id": poi["id"],
                "category": "stay" if poi["id"].startswith("hotel") else "play",
            },
        )
        identifiers[poi["id"]] = saved["candidate_id"]
    for index in range(1, count + 1):
        post(
            client,
            url + "/places/map",
            {
                **versions(client, url),
                "action": "select",
                "candidate_id": identifiers[f"park-{index}"],
            },
        )
    post(
        client,
        url + "/places/origin/confirm",
        {
            **versions(client, url),
            "source": "map",
            "name": "用户确认的出发点",
            "location": [120.14, 30.24],
        },
    )
    start = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=2)).replace(
        hour=9,
        minute=0,
        second=0,
        microsecond=0,
    )
    windows = [
        {
            "date": (start + timedelta(days=index)).date().isoformat(),
            "start_at": (start + timedelta(days=index)).isoformat(),
            "end_at": (start + timedelta(days=index, hours=9)).isoformat(),
        }
        for index in range(count)
    ]
    request = client.get(url).json()["request"]
    request.update(
        start_at=start.isoformat(),
        duration_minutes=(count - 1) * 1440 + 540,
        transport="walking",
        transport_source="user",
    )
    request["planning"].update(
        origin_city="杭州市",
        day_windows=windows,
        overnight_stays=[
            {
                "date": windows[index]["date"],
                "candidate_id": identifiers[f"hotel-{index + 1}"],
            }
            for index in range(count - 1 - int(missing_last_hotel))
        ],
        activity_constraints=[
            {
                "candidate_id": identifiers[f"park-{index + 1}"],
                "date": window["date"],
                "duration_minutes": 20,
                "duration_source": "user",
            }
            for index, window in enumerate(windows)
        ],
    )
    patch_request(client, url, request)
    return url, identifiers, windows


def plan(client, url, calls, *, parent=None, max_routes=40):
    before = len(calls)
    pending = post(
        client,
        url + "/runs",
        {
            "operation_id": str(uuid4()),
            "expected_version": versions(client, url)["expected_version"],
            "kind": "plan",
            "parent_plan_id": parent,
            "budget": {
                "max_combinations": 1,
                "max_seconds": 30,
                "max_routes": max_routes,
            },
        },
        201,
    )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        run = client.get(
            f"/api/runs/{pending['id']}", params={"trip_id": pending["trip_id"]}
        ).json()
        if run["status"] not in {"pending", "running"}:
            break
        time.sleep(0.01)
    assert run["status"] in {"completed", "partial"}, run
    assert run["usage"]["model_calls"] == run["usage"]["searches"] == 0
    actual = calls[before:]
    assert run["usage"]["amap_calls"] == len(actual) <= max_routes
    assert run["usage"]["route_calls"] == sum(
        "direction" in call.url.path for call in actual
    )
    assert run["result"]["plan_ids"], run
    saved = client.get(url + f"/plans/{run['result']['plan_ids'][0]}").json()
    assert saved["run_id"] == pending["id"]
    return saved, run


def adopt(client, url, saved):
    return post(
        client,
        url + f"/plans/{saved['id']}/adopt",
        {
            "operation_id": str(uuid4()),
            "expected_version": versions(client, url)["expected_version"],
            "input_hash": saved["input_hash"],
        },
    )


def assert_days(saved, identifiers, windows):
    days = saved["payload"]["days"]
    assert [day["date"] for day in days] == [window["date"] for window in windows]
    assert [item for day in days for item in day["itinerary"]] == saved["payload"][
        "itinerary"
    ]
    for index, day in enumerate(days):
        assert all(item["date"] == day["date"] for item in day["itinerary"])
        activities = [item for item in day["itinerary"] if item["kind"] == "activity"]
        assert any(
            item["candidate_id"] == identifiers[f"park-{index + 1}"]
            for item in activities
        )
        if index < len(days) - 1:
            assert day["lodging_candidate_id"] == identifiers[f"hotel-{index + 1}"]
            assert (
                day["endpoint"]["poi_id"]
                == days[index + 1]["origin"]["poi_id"]
                == f"hotel-{index + 1}"
            )
    for lodging in saved["payload"]["input"]["lodgings"]:
        assert lodging["source"] == "user" and lodging["run_id"] is None
        assert (
            lodging["sources"]
            == lodging["mentions"]
            == lodging["evidence"]
            == lodging["activities"]
            == []
        )
    assert any(
        issue["code"] == "lodging_details_unverified"
        for issue in saved["payload"]["unknowns"]
    )
    assert saved["status"] == "conditional"
    assert saved["payload"]["conflicts"] == []


def test_two_days_one_night_can_adopt_reopen_and_adjust_second_day(workspace):
    open_client, pois, calls = workspace
    with open_client() as client:
        url, identifiers, windows = prepare(client, pois, 2)
        first, _ = plan(client, url, calls)
        assert_days(first, identifiers, windows)
        adopted = adopt(client, url, first)["trip"]
        assert adopted["active_plan_id"] == first["id"]
        before_restart = len(calls)

    with open_client() as client:
        trip = client.get(url).json()
        assert len(calls) == before_restart and trip["active_plan_id"] == first["id"]
        assert trip["request"]["planning"]["day_windows"] == windows
        assert len(trip["workspace"]["selected_ids"]) == 2
        request = trip["request"]
        request["planning"]["activity_constraints"][1]["duration_minutes"] = 35
        patch_request(client, url, request)
        adjusted, _ = plan(client, url, calls, parent=first["id"])
        assert_days(adjusted, identifiers, windows)
        assert adjusted["input_hash"] != first["input_hash"]
        assert adopt(client, url, adjusted)["trip"]["active_plan_id"] == adjusted["id"]
        assert client.get(url + f"/plans/{first['id']}").json() == first
        assert all(run["kind"] == "plan" for run in client.get(url).json()["runs"])


def test_three_days_two_hotels_keep_branch_anchors_and_share_one_route_budget(
    workspace,
):
    open_client, pois, calls = workspace
    with open_client() as client:
        url, identifiers, windows = prepare(client, pois, 3)
        limited, run = plan(client, url, calls, max_routes=3)
        assert run["usage"]["amap_calls"] == 3
        assert limited["status"] == "partial"
        saved, _ = plan(client, url, calls)
        assert_days(saved, identifiers, windows)
        assert len(saved["payload"]["input"]["lodgings"]) == 2
        assert saved["payload"]["days"][1]["origin"]["poi_id"] == "hotel-1"
        assert saved["payload"]["days"][1]["endpoint"]["poi_id"] == "hotel-2"
        assert adopt(client, url, saved)["trip"]["active_plan_id"] == saved["id"]


def test_unassigned_second_night_stays_missing_even_with_confirmed_hotel_in_pool(
    workspace,
):
    open_client, pois, calls = workspace
    with open_client() as client:
        url, identifiers, windows = prepare(client, pois, 3, missing_last_hotel=True)
        pool = client.get(url + "/places").json()["manual_candidates"]
        assert identifiers["hotel-2"] in {item["id"] for item in pool}
        saved, _ = plan(client, url, calls)
        days = saved["payload"]["days"]
        assert saved["status"] == "partial"
        assert days[0]["lodging_candidate_id"] == identifiers["hotel-1"]
        assert (
            days[1]["date"] == windows[1]["date"]
            and days[1]["lodging_candidate_id"] is None
        )
        assert days[2]["origin"] is None
        assert any(issue["code"] == "lodging_gap" for issue in days[1]["unknowns"])
        assert any(
            issue["code"] == "day_origin_unknown" for issue in days[2]["unknowns"]
        )
        assert len(saved["payload"]["input"]["lodgings"]) == 1
        response = client.post(
            url + f"/plans/{saved['id']}/adopt",
            json={
                "operation_id": str(uuid4()),
                "expected_version": versions(client, url)["expected_version"],
                "input_hash": saved["input_hash"],
            },
        )
        assert response.status_code == 409
        assert client.get(url).json()["active_plan_id"] is None


def test_lodging_suggestion_only_becomes_a_dated_stay_after_explicit_edit_and_replanning(
    workspace,
):
    open_client, pois, calls = workspace
    with open_client() as client:
        url, identifiers, windows = prepare(client, pois, 3, missing_last_hotel=True)
        first, _ = plan(client, url, calls)
        suggestions = [
            item for item in first["payload"]["supplements"] if "lodging_window" in item
        ]
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion["lodging_window"]["date"] == windows[1]["date"]
        assert first["status"] == "partial"
        assert client.get(url).json()["request"]["planning"]["overnight_stays"] == [
            {"date": windows[0]["date"], "candidate_id": identifiers["hotel-1"]}
        ]
        request = client.get(url).json()["request"]
        request["planning"]["overnight_stays"].append(
            {"date": windows[1]["date"], "candidate_id": suggestion["candidate_id"]}
        )
        patch_request(client, url, request)
        second, _ = plan(client, url, calls, parent=first["id"])
        assert second["status"] == "conditional"
        assert (
            second["payload"]["days"][1]["lodging_candidate_id"]
            == suggestion["candidate_id"]
        )
        assert (
            second["payload"]["days"][2]["origin"]["poi_id"] == suggestion["poi"]["id"]
        )
        assert adopt(client, url, second)["trip"]["active_plan_id"] == second["id"]
        assert client.get(url + f"/plans/{first['id']}").json() == first
