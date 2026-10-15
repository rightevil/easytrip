import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_places import raw_poi, seed
from test_rail_mcp import ARRIVAL, DEPARTURE, STATIONS, TICKET, result
from test_rail_search_api import station

from easytrip.api import places, planning
from easytrip.api.trips import make_router as trips_router
from easytrip.config import Settings
from easytrip.domain.models import CreateTrip, TripPatch
from easytrip.integrations import rail
from easytrip.storage.database import Database


@pytest.fixture
def api(tmp_path, monkeypatch):
    database = Database(tmp_path / "usage.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    calls, mode = [], {}

    def provider(request):
        calls.append(request)
        if mode.get("on_request"):
            mode["on_request"]()
        if mode.get("network"):
            raise httpx.ReadTimeout("provider-secret")
        if mode.get("reject"):
            return httpx.Response(200, json={"status": "0", "infocode": "10012"})
        if request.url.path.endswith("detail"):
            pois = [raw_poi()]
        elif mode.get("pages") and request.url.params["page_num"] == "1":
            pois = [raw_poi(f"poi-{i}") for i in range(20)]
        else:
            pois = mode.get("pois", [raw_poi()])
        return httpx.Response(
            200, json={"status": "1", "count": str(len(pois)), "pois": pois}
        )

    async def no_gate(application):
        pass

    monkeypatch.setattr(places, "_before_amap_request", no_gate)
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(
        amap_web_key="provider-secret", rail_mcp="http://rail.invalid/mcp"
    )
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app.include_router(places.make_router())
    app.include_router(planning.make_router())
    app.include_router(trips_router())
    with TestClient(app) as client:
        yield client, database, trip, calls, mode
        client.portal.call(app.state.http.aclose)


def actions(database):
    with database.connection() as connection:
        return [
            dict(row)
            for row in connection.execute("SELECT * FROM api_actions ORDER BY rowid")
        ]


def search(api, suffix="manual", query="西湖"):
    client, _, trip, _, _ = api
    return client.get(
        f"/api/trips/{trip['id']}/places/{suffix}/search",
        params={"expected_version": 1, "city": "杭州", "query": query},
    )


def test_amap_pages_cache_and_manual_detail_are_counted_separately(api):
    client, database, trip, calls, mode = api
    mode["pages"] = True
    response = search(api)
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert search(api).status_code == 200
    assert len(calls) == 2
    body = {
        "operation_id": str(uuid4()),
        "expected_version": 1,
        "expected_workspace_version": 1,
        "search_id": response.json()["search_id"],
        "poi_id": "poi-1",
        "category": "play",
    }
    assert (
        client.post(
            f"/api/trips/{trip['id']}/places/manual/confirm", json=body
        ).status_code
        == 200
    )
    rows = actions(database)
    assert [row["operation"] for row in rows] == ["poi_search"] * 3 + ["poi_detail"]
    assert [row["cached"] for row in rows] == [0, 0, 1, 0]
    assert all(
        row["status"] == "completed" and row["elapsed_seconds"] >= 0 for row in rows
    )
    assert all(
        json.loads(row["metadata_json"]) == {"api_version": "v5"} for row in rows
    )
    assert len(calls) == 3 and "provider-secret" not in json.dumps(rows)


@pytest.mark.parametrize("entry", ["origin", "match"])
def test_origin_and_p3_match_share_physical_search_and_cache_accounting(api, entry):
    client, database, trip, calls, _ = api
    if entry == "match":
        seed(database, trip)

        def invoke():
            return client.post(
                f"/api/trips/{trip['id']}/places/match",
                json={
                    "expected_version": 1,
                    "expected_workspace_version": database.get_trip(trip["id"])[
                        "workspace_version"
                    ],
                },
            )
    else:

        def invoke():
            return search(api, "origin")

    assert invoke().status_code == 200
    assert invoke().status_code == 200
    assert len(calls) == 1
    assert [(row["operation"], row["cached"]) for row in actions(database)] == [
        ("poi_search", 0),
        ("poi_search", 1),
    ]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [("network", "unknown"), ("reject", "failed"), ("pois", "failed")],
)
def test_amap_network_provider_and_invalid_payload_outcomes(api, failure, expected):
    _, database, _, calls, mode = api
    mode[failure] = {} if failure == "pois" else True
    response = search(api)
    assert response.status_code == 502
    assert len(calls) == 1
    assert [row["status"] for row in actions(database)] == [expected]
    assert "provider-secret" not in response.text


def test_late_amap_result_retains_original_version_usage(api):
    _, database, trip, calls, mode = api
    mode["on_request"] = lambda: database.update_trip(
        trip["id"], TripPatch(expected_version=1, name="新条件")
    )
    assert search(api).status_code == 409
    rows = actions(database)
    assert len(calls) == len(rows) == 1
    assert rows[0]["trip_id"] == trip["id"] and rows[0]["request_version"] == 1
    assert rows[0]["status"] == "completed"


def test_amap_checks_version_after_waiting_for_qps_slot(api, monkeypatch):
    _, database, trip, calls, _ = api

    async def changed_while_waiting(application):
        database.update_trip(
            trip["id"], TripPatch(expected_version=1, name="等待时已改条件")
        )

    monkeypatch.setattr(places, "_before_amap_request", changed_while_waiting)
    assert search(api).status_code == 409
    assert calls == [] and actions(database) == []


@pytest.mark.parametrize("phase", ["begin", "finish", "finish_false"])
def test_amap_ledger_failure_stops_p3_before_any_following_request(
    api, monkeypatch, phase
):
    client, database, trip, calls, mode = api
    seed(database, trip, names=("西湖", "断桥"))
    mode["reject"] = True

    def fail(*args, **kwargs):
        if phase == "finish_false":
            return False
        raise sqlite3.OperationalError("sensitive database error")

    monkeypatch.setattr(
        database, "begin_api_action" if phase == "begin" else "finish_api_action", fail
    )
    response = client.post(
        f"/api/trips/{trip['id']}/places/match",
        json={"expected_version": 1, "expected_workspace_version": 1},
    )
    assert response.status_code == 503
    assert len(calls) == (0 if phase == "begin" else 1)
    assert "sensitive" not in response.text


@pytest.fixture
def mcp(api, monkeypatch):
    client, database, trip, _, mode = api
    departure = station(database, trip["id"], "station-1", DEPARTURE["station_name"])
    arrival = station(database, trip["id"], "station-2", ARRIVAL["station_name"])
    tools = [
        SimpleNamespace(name=name, inputSchema={"type": "object"})
        for name in rail.READ_ONLY
    ]
    session = SimpleNamespace(
        initialize=AsyncMock(
            return_value=SimpleNamespace(
                protocolVersion="2025-03-26",
                serverInfo=SimpleNamespace(version="0.3.10"),
            )
        ),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=tools)),
        call_tool=AsyncMock(side_effect=[result(STATIONS), result([TICKET])]),
    )

    @asynccontextmanager
    async def transport(*args, **kwargs):
        yield object(), object(), lambda: None

    @asynccontextmanager
    async def client_session(*args, **kwargs):
        yield session

    monkeypatch.setattr(rail, "streamable_http_client", transport)
    monkeypatch.setattr(rail, "ClientSession", client_session)
    body = {
        "expected_version": 1,
        "date": "2026-10-15",
        "departure_station_id": departure,
        "arrival_station_id": arrival,
    }

    def invoke():
        return client.post(f"/api/trips/{trip['id']}/rail/search", json=body)

    return session, invoke, tools


def test_rail_tools_are_individually_accounted_with_version_and_schema(api, mcp):
    _, database, trip, _, _ = api
    session, invoke, _ = mcp
    assert invoke().status_code == 200
    rows = actions(database)
    assert [row["operation"] for row in rows] == [
        "get-station-code-by-names",
        "get-tickets",
    ]
    assert session.call_tool.await_count == 2
    for row in rows:
        assert row["trip_id"] == trip["id"] and row["status"] == "completed"
        metadata = json.loads(row["metadata_json"])
        assert (
            metadata["api_version"] == "2025-03-26"
            and metadata["server_version"] == "0.3.10"
        )
        assert len(metadata["schema_fingerprint"]) == 64
        assert set(metadata) == {"api_version", "server_version", "schema_fingerprint"}


@pytest.mark.parametrize(
    "failure", ["network", "station", "tickets", "ticket_fields", "provider"]
)
def test_rail_network_and_parsing_failure_outcomes(api, mcp, failure):
    _, database, _, _, _ = api
    session, invoke, _ = mcp
    if failure == "network":
        session.call_tool.side_effect = httpx.ReadTimeout("provider-secret")
    elif failure == "station":
        session.call_tool.side_effect = [result({})]
    elif failure == "provider":
        session.call_tool.side_effect = [result({}, error=True)]
    else:
        session.call_tool.side_effect = [
            result(STATIONS),
            result({} if failure == "tickets" else [{**TICKET, "lishi": "wrong"}]),
        ]
    response = invoke()
    assert response.status_code == 502
    rows = actions(database)
    assert rows[-1]["status"] == ("unknown" if failure == "network" else "failed")
    assert session.call_tool.await_count == len(rows)
    assert "provider-secret" not in response.text


@pytest.mark.parametrize("phase", ["begin", "finish", "finish_false"])
def test_rail_ledger_failure_never_continues_to_next_tool(api, mcp, monkeypatch, phase):
    _, database, _, _, _ = api
    session, invoke, _ = mcp

    def fail(*args, **kwargs):
        if phase == "finish_false":
            return False
        raise sqlite3.OperationalError("sensitive database error")

    monkeypatch.setattr(
        database, "begin_api_action" if phase == "begin" else "finish_api_action", fail
    )
    response = invoke()
    assert response.status_code == 503
    assert session.call_tool.await_count == (0 if phase == "begin" else 1)
    assert "sensitive" not in response.text


def test_invalid_tool_schema_is_not_billed_or_called(api, mcp):
    _, database, _, _, _ = api
    session, invoke, tools = mcp
    for tool in tools:
        tool.inputSchema = {"$ref": "https://hostile.invalid/schema"}
    assert invoke().status_code == 502
    assert actions(database) == [] and session.call_tool.await_count == 0


@pytest.mark.parametrize("change_after", [1, 2])
def test_late_rail_result_is_billed_to_original_trip_without_more_stale_calls(
    api, mcp, change_after
):
    _, database, trip, _, _ = api
    session, invoke, _ = mcp

    async def provider(name, arguments):
        if session.call_tool.await_count == change_after:
            database.update_trip(
                trip["id"], TripPatch(expected_version=1, name="新条件")
            )
        return result(STATIONS if name == "get-station-code-by-names" else [TICKET])

    session.call_tool.side_effect = provider
    assert invoke().status_code == 409
    rows = actions(database)
    assert len(rows) == session.call_tool.await_count == change_after
    assert all(
        row["request_version"] == 1 and row["status"] == "completed" for row in rows
    )


@pytest.mark.parametrize("ledger_failure", [False, True])
def test_cancelled_rail_call_records_unknown_and_preserves_cancellation(
    api, mcp, ledger_failure
):
    _, database, trip, _, _ = api
    session, _, _ = mcp
    session.call_tool.side_effect = asyncio.CancelledError()

    async def before(name):
        return database.begin_api_action(trip["id"], 1, "rail", name)

    async def after(action_id, **usage):
        if ledger_failure:
            raise sqlite3.OperationalError("ledger unavailable")
        database.finish_api_action(action_id, **usage)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await rail.search_rail(
                "http://rail.invalid/mcp",
                object(),
                date=date(2026, 10, 15),
                departure_name="杭州东",
                arrival_name="上海虹桥",
                before_call=before,
                after_call=after,
            )

    asyncio.run(run())
    if ledger_failure:
        assert [row["status"] for row in actions(database)] == ["started"]
        database.recover_api_actions()
    assert [row["status"] for row in actions(database)] == ["unknown"]
