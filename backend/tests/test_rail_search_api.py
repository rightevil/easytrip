from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_places import raw_poi

from easytrip.api import planning as planning_api
from easytrip.api.trips import make_router as trips_router
from easytrip.config import Settings
from easytrip.domain.models import ArchiveTrip, CreateTrip, TripPatch
from easytrip.domain.places import ConfirmManualPoi
from easytrip.integrations.rail import RailError
from easytrip.services.places import normalize_poi
from easytrip.storage.database import Database, encode, now


def station(database, trip_id, poi_id, name, *, kind="交通设施服务;火车站"):
    trip = database.get_trip(trip_id)
    poi = normalize_poi(raw_poi(poi_id, name, type=kind), now())
    search = database.save_manual_search(
        trip_id, trip["request_version"], name, "杭州", [poi]
    )
    body = ConfirmManualPoi(
        operation_id=uuid4(),
        expected_version=trip["request_version"],
        expected_workspace_version=trip["workspace_version"],
        search_id=search["search_id"],
        poi_id=poi_id,
        category="play",
    )
    return database.confirm_manual_poi(trip_id, body, poi)["candidate_id"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    database = Database(tmp_path / "rail.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    departure = station(database, trip["id"], "station-1", "杭州东站")
    arrival = station(database, trip["id"], "station-2", "杭州南站")
    calls, mode = [], {}

    async def search(url, client, **query):
        calls.append(query)
        if mode.get("late"):
            mode["late"]()
        if mode.get("error"):
            raise RailError("12306 MCP 连接或查询失败，请检查服务状态后重试")
        return {
            "source": "12306_mcp",
            "queried_at": "2026-10-15T09:00:00+08:00",
            "departure_station": {"name": "杭州东", "code": "HGH"},
            "arrival_station": {"name": "杭州南", "code": "XHH"},
            "items": [],
        }

    monkeypatch.setattr(planning_api, "search_rail", search)
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(rail_mcp="http://rail.invalid/mcp?token=local-secret")
    app.state.http = httpx.AsyncClient()
    app.include_router(planning_api.make_router())
    app.include_router(trips_router())
    body = {
        "expected_version": trip["request_version"],
        "date": "2026-10-16",
        "departure_station_id": departure,
        "arrival_station_id": arrival,
    }
    with TestClient(app) as client:
        yield client, database, app, trip, body, calls, mode
        client.portal.call(app.state.http.aclose)


def query(api, **changes):
    client, _, _, trip, body, _, _ = api
    return client.post(f"/api/trips/{trip['id']}/rail/search", json={**body, **changes})


def test_query_uses_saved_station_names_and_does_not_modify_trip(api):
    _, database, _, trip, body, calls, _ = api
    before = database.get_trip(trip["id"])
    response = query(api)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["trip_id"] == trip["id"] and result["request_version"] == 1
    assert result["date"] == body["date"] and result["items"] == []
    assert result["departure_station_id"] == body["departure_station_id"]
    assert result["arrival_station_id"] == body["arrival_station_id"]
    assert result["source"] == "12306_mcp"
    assert "departure_poi" not in result and "local-secret" not in response.text
    assert len(calls) == 1 and calls[0]["departure_name"] == "杭州东站"
    assert calls[0]["arrival_name"] == "杭州南站"
    assert calls[0]["date"].isoformat() == body["date"]
    assert database.get_trip(trip["id"]) == before


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"expected_version": 2}, 409),
        ({"departure_station_id": "unknown"}, 409),
        ({"date": "2026-13-16"}, 422),
        ({"expected_version": True}, 422),
        ({"url": "http://untrusted.invalid"}, 422),
    ],
)
def test_invalid_input_never_contacts_mcp(api, changes, status):
    assert query(api, **changes).status_code == status
    assert api[5] == []


def test_same_stations_and_foreign_candidates_are_rejected(api):
    _, database, _, trip, body, calls, _ = api
    assert (
        query(api, arrival_station_id=body["departure_station_id"]).status_code == 422
    )
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    foreign = station(database, other["id"], "foreign", "杭州站")
    assert query(api, arrival_station_id=foreign).status_code == 409
    nonstation = station(database, trip["id"], "park", "西湖", kind="风景名胜")
    assert query(api, arrival_station_id=nonstation).status_code == 409
    assert not calls


def test_unconfigured_service_and_provider_failure_are_explicit(api):
    _, _, app, _, _, calls, mode = api
    address = app.state.settings.rail_mcp
    app.state.settings.rail_mcp = ""
    assert query(api).status_code == 503 and not calls
    app.state.settings.rail_mcp = address
    mode["error"] = True
    response = query(api)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "rail_query_failed"
    assert "local-secret" not in response.text and "http:" not in response.text


@pytest.mark.parametrize("change", ["request", "archive", "delete", "poi"])
def test_late_result_rechecks_trip_and_exact_station_snapshot(api, change):
    _, database, _, trip, body, calls, mode = api

    def late():
        if change == "request":
            database.update_trip(
                trip["id"], TripPatch(expected_version=1, name="新条件")
            )
        elif change == "archive":
            database.archive_trip(
                trip["id"], ArchiveTrip(expected_version=1, archived=True)
            )
        elif change == "delete":
            database.delete_trip(trip["id"], 1)
        else:
            with database.connection(write=True) as connection:
                row = connection.execute(
                    "SELECT poi_json FROM manual_candidates WHERE id=?",
                    (body["arrival_station_id"],),
                ).fetchone()
                import json

                poi = json.loads(row["poi_json"])
                poi["name"] = "已改变的车站"
                connection.execute(
                    "UPDATE manual_candidates SET poi_json=? WHERE id=?",
                    (encode(poi), body["arrival_station_id"]),
                )

    mode["late"] = late
    response = query(api)
    assert response.status_code == (404 if change == "delete" else 409)
    assert len(calls) == 1


def test_archived_trip_never_queries(api):
    _, database, _, trip, _, calls, _ = api
    database.archive_trip(trip["id"], ArchiveTrip(expected_version=1, archived=True))
    assert query(api, expected_version=2).status_code == 409
    assert not calls
