"""One bounded, read-only HTTP MCP session for exact-station railway queries."""

import asyncio
import hashlib
import json
import math
import re
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from time import monotonic

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ..config import validate_url
from .mcp_schema import validate_arguments

READ_ONLY = frozenset({"get-station-code-by-names", "get-tickets"})
CHINA = timezone(timedelta(hours=8))
QUERY_TIMEOUT_SECONDS = 60


class RailError(RuntimeError):
    """Safe to show to the user; never contains provider errors or credentials."""


def _station_name(value):
    if not isinstance(value, str):
        raise RailError("请填写准确的出发站和到达站名称")
    value = value.strip().removesuffix("站")
    if not re.fullmatch(r"[A-Za-z\u3400-\u9fff·]{1,40}", value):
        raise RailError("请填写准确的出发站和到达站名称")
    return value


def _result_json(result):
    if result.isError:
        raise RailError("12306 MCP 查询失败，请稍后重新查询")
    if result.structuredContent is not None:
        return result.structuredContent
    text = "\n".join(item.text for item in result.content if item.type == "text")
    if len(text) > 1_000_000:
        raise RailError("12306 MCP 返回结果过大")
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        raise RailError("12306 MCP 未返回有效查询数据，请检查服务状态和日期") from None


def _station(payload, name):
    row = payload.get(name) if isinstance(payload, dict) else None
    if (
        not isinstance(row, dict)
        or row.get("station_name") != name
        or not isinstance(row.get("station_code"), str)
        or not re.fullmatch(r"[A-Z]{3}", row["station_code"])
    ):
        raise RailError("12306 未找到名称完全一致的车站，请填写具体站名")
    return {"name": name, "code": row["station_code"]}


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-2]\d:[0-5]\d", value):
        raise ValueError
    return time.fromisoformat(value)


def _ticket(row, query_date, departure, arrival):
    if not isinstance(row, dict) or any(
        row.get(key) != expected
        for key, expected in (
            ("from_station", departure["name"]),
            ("from_station_telecode", departure["code"]),
            ("to_station", arrival["name"]),
            ("to_station_telecode", arrival["code"]),
        )
    ):
        raise ValueError
    train = row.get("start_train_code")
    if not isinstance(train, str) or not re.fullmatch(
        r"[A-Z]?\d{1,5}(?:/[A-Z]?\d{1,5})?", train
    ):
        raise ValueError
    duration = row.get("lishi")
    if not isinstance(duration, str) or not re.fullmatch(r"\d{1,3}:[0-5]\d", duration):
        raise ValueError
    hours, minutes = map(int, duration.split(":"))
    duration_minutes = hours * 60 + minutes
    if not 0 < duration_minutes <= 7 * 24 * 60:
        raise ValueError
    start, end = _clock(row.get("start_time")), _clock(row.get("arrive_time"))
    provider_start = datetime.combine(Date.fromisoformat(row["start_date"]), start)
    provider_end = datetime.combine(Date.fromisoformat(row["arrive_date"]), end)
    if not 0 <= (
        query_date - provider_start.date()
    ).days <= 7 or provider_end - provider_start != timedelta(minutes=duration_minutes):
        raise ValueError
    # Upstream start_date is the train's origin date, not the boarding-station date.
    departure_at = datetime.combine(query_date, start, CHINA)
    arrival_at = departure_at + timedelta(minutes=duration_minutes)
    if arrival_at.time() != end:
        raise ValueError
    prices = row.get("prices")
    if not isinstance(prices, list) or len(prices) > 30:
        raise ValueError
    seats = []
    for seat in prices:
        if not isinstance(seat, dict):
            raise ValueError
        name, remaining, price = (
            seat.get("seat_name"),
            seat.get("num"),
            seat.get("price"),
        )
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 40
            or not isinstance(remaining, str)
            or len(remaining) > 30
            or (
                price is not None
                and (
                    type(price) not in (int, float)
                    or not math.isfinite(price)
                    or price < 0
                )
            )
        ):
            raise ValueError
        seats.append(
            {
                "name": name,
                "remaining": remaining or "未知",
                "price_cny": price if price else None,
            }
        )
    return {
        "train_number": train,
        "departure_at": departure_at.isoformat(),
        "arrival_at": arrival_at.isoformat(),
        "duration_minutes": duration_minutes,
        "seats": seats,
    }


async def search_rail(
    url,
    client,
    *,
    date: Date,
    departure_name: str,
    arrival_name: str,
    before_call=None,
    after_call=None,
):
    """Use a direct_client; no service startup, retries, booking or automatic adoption."""
    departure_name, arrival_name = (
        _station_name(departure_name),
        _station_name(arrival_name),
    )
    if departure_name == arrival_name:
        raise RailError("出发站和到达站不能相同")
    if type(date) is not Date:
        raise RailError("请选择有效的乘车日期")
    hook_error = None
    try:
        validate_url(url)
        async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
            async with streamable_http_client(url, http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(
                    read, write, read_timeout_seconds=timedelta(seconds=45)
                ) as session:
                    initialized = await session.initialize()
                    page = await session.list_tools()
                    tools = {
                        tool.name: tool for tool in page.tools if tool.name in READ_ONLY
                    }
                    if not READ_ONLY.issubset(tools):
                        raise RailError("12306 MCP 缺少站名或余票查询工具")

                    async def call(name, arguments, parse):
                        nonlocal hook_error
                        if name not in READ_ONLY:
                            raise RailError("铁路工具不在只读白名单")
                        try:
                            validate_arguments(arguments, tools[name].inputSchema)
                        except ValueError:
                            raise RailError(
                                "12306 MCP 工具参数定义包含不可解析的引用"
                            ) from None
                        metadata = {
                            "schema_fingerprint": hashlib.sha256(
                                json.dumps(
                                    tools[name].inputSchema,
                                    sort_keys=True,
                                    ensure_ascii=False,
                                ).encode("utf-8")
                            ).hexdigest()
                        }
                        version = getattr(initialized, "protocolVersion", None)
                        if isinstance(version, str):
                            metadata["api_version"] = version
                        server_version = getattr(
                            getattr(initialized, "serverInfo", None), "version", None
                        )
                        if isinstance(server_version, str):
                            metadata["server_version"] = server_version
                        action_id = None
                        if before_call is not None:
                            try:
                                action_id = await before_call(name)
                            except Exception as error:
                                hook_error = error
                                raise
                        started = monotonic()
                        status = "unknown"
                        cancelled = False
                        try:
                            result = await session.call_tool(name, arguments)
                            try:
                                data = parse(_result_json(result))
                            except (
                                RailError,
                                ValueError,
                                TypeError,
                                KeyError,
                                OverflowError,
                            ):
                                status = "failed"
                                raise
                            status = "completed"
                            return data
                        except asyncio.CancelledError:
                            cancelled = True
                            raise
                        finally:
                            if after_call is not None and action_id is not None:
                                try:
                                    await after_call(
                                        action_id,
                                        status=status,
                                        elapsed_seconds=monotonic() - started,
                                        metadata=metadata,
                                    )
                                except Exception as error:
                                    if not cancelled:
                                        hook_error = error
                                        raise

                    def parse_stations(stations):
                        departure = _station(stations, departure_name)
                        arrival = _station(stations, arrival_name)
                        if departure["code"] == arrival["code"]:
                            raise RailError("出发站和到达站不能相同")
                        return departure, arrival

                    departure, arrival = await call(
                        "get-station-code-by-names",
                        {"stationNames": f"{departure_name}|{arrival_name}"},
                        parse_stations,
                    )

                    def parse_tickets(rows):
                        if not isinstance(rows, list) or len(rows) > 2000:
                            raise RailError("12306 MCP 余票结果格式不正确")
                        try:
                            items = []
                            for row in rows:
                                if not isinstance(row, dict) or any(
                                    not isinstance(row.get(key), str)
                                    or not row[key].strip()
                                    for key in (
                                        "from_station",
                                        "to_station",
                                        "from_station_telecode",
                                        "to_station_telecode",
                                    )
                                ):
                                    raise ValueError
                                for prefix, station in (
                                    ("from", departure),
                                    ("to", arrival),
                                ):
                                    if (
                                        row[f"{prefix}_station"] == station["name"]
                                    ) != (
                                        row[f"{prefix}_station_telecode"]
                                        == station["code"]
                                    ):
                                        raise ValueError
                                # 12306 may include other stations in the same cities.
                                if (
                                    row["from_station_telecode"] != departure["code"]
                                    or row["to_station_telecode"] != arrival["code"]
                                ):
                                    continue
                                items.append(_ticket(row, date, departure, arrival))
                        except (KeyError, TypeError, ValueError, OverflowError):
                            raise RailError(
                                "12306 MCP 车次的站点、时间或票价信息不一致"
                            ) from None
                        return {
                            "matched_count": len(items),
                            "excluded_other_station_count": len(rows) - len(items),
                            "items": sorted(
                                items, key=lambda item: item["departure_at"]
                            )[:20],
                        }

                    tickets = await call(
                        "get-tickets",
                        {
                            "date": date.isoformat(),
                            "fromStation": departure["code"],
                            "toStation": arrival["code"],
                            "sortFlag": "startTime",
                            "limitedNum": 0,
                            "format": "json",
                        },
                        parse_tickets,
                    )
                    return {
                        "source": "12306_mcp",
                        "queried_at": datetime.now(CHINA).isoformat(),
                        "departure_station": departure,
                        "arrival_station": arrival,
                        **tickets,
                    }
    except RailError:
        raise
    except Exception as error:
        pending = [error]
        while pending:
            failure = pending.pop()
            if failure is hook_error:
                raise hook_error
            if isinstance(failure, RailError):
                raise RailError(str(failure)) from None
            if isinstance(failure, BaseExceptionGroup):
                pending.extend(failure.exceptions)
        raise RailError("12306 MCP 连接或查询失败，请检查服务状态后重试") from None
