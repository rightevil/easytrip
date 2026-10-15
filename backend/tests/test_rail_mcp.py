import asyncio
import copy
import json
from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from easytrip.config import direct_client
from easytrip.integrations import rail

DEPARTURE = {"station_name": "杭州东", "station_code": "HGH"}
ARRIVAL = {"station_name": "上海虹桥", "station_code": "AOH"}
STATIONS = {"杭州东": DEPARTURE, "上海虹桥": ARRIVAL}
TICKET = {
    "start_train_code": "D123",
    "start_date": "2026-10-14",
    "arrive_date": "2026-10-15",
    "start_time": "23:45",
    "arrive_time": "01:15",
    "lishi": "01:30",
    "from_station": "杭州东",
    "from_station_telecode": "HGH",
    "to_station": "上海虹桥",
    "to_station_telecode": "AOH",
    "prices": [
        {"seat_name": "二等座", "num": "有", "price": 73},
        {"seat_name": "无座", "num": "0", "price": None},
    ],
}


def result(payload, *, error=False, structured=False):
    return SimpleNamespace(
        isError=error,
        structuredContent=payload if structured else None,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


@pytest.fixture
def mcp(monkeypatch):
    schemas = {
        "get-station-code-by-names": {
            "type": "object",
            "properties": {"stationNames": {"type": "string"}},
            "required": ["stationNames"],
            "additionalProperties": False,
        },
        "get-tickets": {
            "type": "object",
            "properties": {
                "date": {"type": "string"},
                "fromStation": {"type": "string"},
                "toStation": {"type": "string"},
                "sortFlag": {"enum": ["startTime"]},
                "limitedNum": {"const": 0},
                "format": {"const": "json"},
            },
            "required": ["date", "fromStation", "toStation", "limitedNum", "format"],
            "additionalProperties": False,
        },
    }
    tools = [
        SimpleNamespace(name=name, inputSchema=schema)
        for name, schema in schemas.items()
    ]
    tools.append(SimpleNamespace(name="buy-ticket", inputSchema={}))
    session = SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(tools=tools, nextCursor=None)
        ),
        call_tool=AsyncMock(side_effect=[result(STATIONS), result([TICKET])]),
    )
    transport_calls = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        transport_calls.append((url, http_client))
        yield object(), object(), lambda: None

    @asynccontextmanager
    async def client_session(*args, **kwargs):
        yield session

    monkeypatch.setattr(rail, "streamable_http_client", transport)
    monkeypatch.setattr(rail, "ClientSession", client_session)
    return session, schemas, transport_calls


def search(client=None, **kwargs):
    return asyncio.run(
        rail.search_rail(
            "http://127.0.0.1:18061/mcp",
            client or object(),
            date=kwargs.pop("date", date(2026, 10, 15)),
            departure_name=kwargs.pop("departure_name", "杭州东站"),
            arrival_name=kwargs.pop("arrival_name", "上海虹桥"),
            **kwargs,
        )
    )


def test_exact_stations_one_query_and_cross_midnight_rebased_to_boarding_date(mcp):
    session, _, calls = mcp
    client = direct_client()
    try:
        response = search(client)
    finally:
        asyncio.run(client.aclose())
    assert calls == [("http://127.0.0.1:18061/mcp", client)]
    assert client._trust_env is False
    assert response["source"] == "12306_mcp"
    assert response["queried_at"].endswith("+08:00")
    assert response["departure_station"] == {"name": "杭州东", "code": "HGH"}
    assert response["arrival_station"] == {"name": "上海虹桥", "code": "AOH"}
    item = response["items"][0]
    assert item["departure_at"] == "2026-10-15T23:45:00+08:00"
    assert item["arrival_at"] == "2026-10-16T01:15:00+08:00"
    assert item["duration_minutes"] == 90
    assert item["seats"][1]["price_cny"] is None
    assert session.call_tool.await_args_list[0].args == (
        "get-station-code-by-names",
        {"stationNames": "杭州东|上海虹桥"},
    )
    assert session.call_tool.await_args_list[1].args == (
        "get-tickets",
        {
            "date": "2026-10-15",
            "fromStation": "HGH",
            "toStation": "AOH",
            "sortFlag": "startTime",
            "limitedNum": 0,
            "format": "json",
        },
    )
    assert session.call_tool.await_count == 2
    session.initialize.assert_awaited_once()
    session.list_tools.assert_awaited_once()


def test_empty_valid_result_is_not_a_failure(mcp):
    mcp[0].call_tool.side_effect = [result(STATIONS), result([], structured=True)]
    assert search()["items"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from_station", "杭州"),
        ("from_station_telecode", "HZH"),
        ("to_station", "上海"),
        ("to_station_telecode", "SHH"),
        ("start_time", "24:00"),
        ("arrive_time", "01:16"),
        ("arrive_date", "2026-10-16"),
        ("lishi", None),
        ("lishi", "00:00"),
        ("lishi", "01:99"),
        ("prices", None),
        ("prices", [{"seat_name": "二等座", "num": "有", "price": -1}]),
        ("prices", [{"seat_name": "二等座", "num": "有", "price": float("nan")}]),
        ("prices", [{"seat_name": "二等座", "num": "有", "price": True}]),
        ("start_train_code", "untrusted-train"),
    ],
)
def test_inconsistent_provider_facts_are_rejected(mcp, field, value):
    row = {**copy.deepcopy(TICKET), field: value}
    mcp[0].call_tool.side_effect = [result(STATIONS), result([row])]
    with pytest.raises(rail.RailError, match="不一致"):
        search()
    assert mcp[0].call_tool.await_count == 2


def test_zero_provider_price_remains_unknown(mcp):
    row = copy.deepcopy(TICKET)
    row["prices"][0]["price"] = 0
    mcp[0].call_tool.side_effect = [result(STATIONS), result([row])]
    assert search()["items"][0]["seats"][0]["price_cny"] is None


@pytest.mark.parametrize(
    "payload", [{}, {"杭州东": {"station_name": "杭州", "station_code": "HGH"}}]
)
def test_unmatched_station_aborts_before_ticket_query(mcp, payload):
    mcp[0].call_tool.side_effect = [result(payload)]
    with pytest.raises(rail.RailError, match="完全一致"):
        search()
    assert mcp[0].call_tool.await_count == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"departure_name": "杭州东|北京"},
        {"departure_name": "杭州东", "arrival_name": "杭州东站"},
        {"departure_name": "https://secret.example"},
        {"date": "2026-10-15"},
    ],
)
def test_invalid_query_never_contacts_service(mcp, kwargs):
    with pytest.raises(rail.RailError):
        search(**kwargs)
    assert not mcp[2]


def test_provider_schema_is_checked_before_call(mcp):
    mcp[1]["get-tickets"]["properties"]["format"] = {"const": "csv"}
    with pytest.raises(rail.RailError, match="连接或查询失败"):
        search()
    assert mcp[0].call_tool.await_count == 1


def test_schema_reference_does_not_trigger_extra_network_calls(mcp):
    mcp[1]["get-station-code-by-names"]["$ref"] = "https://secret.example/schema"
    with pytest.raises(rail.RailError, match="引用"):
        search()
    mcp[0].call_tool.assert_not_awaited()


def test_missing_read_only_tool_is_rejected(mcp):
    mcp[0].list_tools.return_value.tools = []
    with pytest.raises(rail.RailError, match="缺少"):
        search()
    mcp[0].call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("http://secret.example/token?key=secret"),
        RuntimeError("http://secret.example/token?key=secret"),
        ExceptionGroup(
            "http://secret.example/token?key=secret", [RuntimeError("secret")]
        ),
    ],
)
def test_connection_failures_are_safe_and_never_retried(mcp, failure):
    mcp[0].call_tool.side_effect = failure
    with pytest.raises(rail.RailError) as caught:
        search()
    assert "secret" not in str(caught.value)
    assert "http" not in str(caught.value)
    assert mcp[0].call_tool.await_count == 1


def test_tool_error_payload_is_not_exposed(mcp):
    mcp[0].call_tool.side_effect = [result("http://secret.example/token", error=True)]
    with pytest.raises(rail.RailError) as caught:
        search()
    assert "secret" not in str(caught.value)
    assert mcp[0].call_tool.await_count == 1


def test_total_timeout_is_bounded(mcp, monkeypatch):
    monkeypatch.setattr(rail, "QUERY_TIMEOUT_SECONDS", 0.01)

    async def stalled(*args, **kwargs):
        await asyncio.sleep(5)

    mcp[0].call_tool.side_effect = stalled
    with pytest.raises(rail.RailError, match="连接或查询失败"):
        search()
    assert mcp[0].call_tool.await_count == 1


def test_response_limit_is_enforced(mcp):
    mcp[0].call_tool.side_effect = [result(STATIONS), result([TICKET] * 2001)]
    with pytest.raises(rail.RailError, match="格式不正确"):
        search()


def test_other_stations_are_filtered_before_limit_not_mistaken_for_requested_stations(
    mcp,
):
    other = {
        **TICKET,
        "from_station": "杭州南",
        "from_station_telecode": "XHH",
        "to_station": "上海松江",
        "to_station_telecode": "IMH",
    }
    requested = [
        {**TICKET, "start_train_code": f"D{number}"} for number in range(100, 125)
    ]
    mcp[0].call_tool.side_effect = [result(STATIONS), result([other] * 25 + requested)]
    response = search()
    assert response["matched_count"] == 25
    assert response["excluded_other_station_count"] == 25
    assert len(response["items"]) == 20
    assert response["items"][0]["train_number"] == "D100"


def test_only_other_stations_reports_empty_exact_results(mcp):
    other = {**TICKET, "from_station": "杭州南", "from_station_telecode": "XHH"}
    mcp[0].call_tool.side_effect = [result(STATIONS), result([other])]
    response = search()
    assert response["items"] == []
    assert response["matched_count"] == 0
    assert response["excluded_other_station_count"] == 1


@pytest.mark.parametrize("origin", ["2026-10-16", "2026-10-01"])
def test_future_or_stale_provider_origin_dates_are_rejected(mcp, origin):
    row = {
        **TICKET,
        "start_date": origin,
        "start_time": "09:00",
        "arrive_time": "10:30",
        "arrive_date": origin,
    }
    mcp[0].call_tool.side_effect = [result(STATIONS), result([row])]
    with pytest.raises(rail.RailError, match="不一致"):
        search()


def test_session_exception_group_retains_our_safe_error_message(mcp):
    mcp[0].call_tool.side_effect = ExceptionGroup(
        "unsafe provider url", [rail.RailError("12306 MCP 未返回有效查询数据")]
    )
    with pytest.raises(rail.RailError, match="未返回有效查询数据") as caught:
        search()
    assert "unsafe" not in str(caught.value)
