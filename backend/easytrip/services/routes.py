"""Real route facts, bounded reuse, and explicit gaps; no invented road geometry."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from ..domain.routes import RouteLeg
from ..integrations.amap import AmapError, _location
from ..storage.routes import ROUTE_TTL, normalized_city

MODES = {"walking", "transit", "bicycling", "electrobike", "driving"}
CHINA_TIME = timezone(timedelta(hours=8))


def _quantity(value, *, maximum=100_000_000):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def _coordinates(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(
            isinstance(number, bool) or not isinstance(number, (int, float))
            for number in value
        )
    ):
        raise ValueError("路线端点必须是经度、纬度")
    return _location(f"{value[0]},{value[1]}")


def _polyline(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        points = [list(map(float, point.split(","))) for point in value.split(";")]
        if len(points) < 2 or any(
            len(point) != 2
            or not all(math.isfinite(number) for number in point)
            or not -180 <= point[0] <= 180
            or not -90 <= point[1] <= 90
            for point in points
        ):
            return None
        return points
    except (TypeError, ValueError):
        return None


def _steps_geometry(value):
    if not isinstance(value, dict):
        return [], False
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        line = _polyline(value.get("polyline"))
        return ([line], True) if line else ([], False)
    lines, complete = [], True
    for step in steps:
        line = _polyline(step.get("polyline")) if isinstance(step, dict) else None
        if line:
            lines.append(line)
        else:
            complete = False
    return lines, complete


def normalize_route(mode, response):
    """Select the first returned proposal, never merge alternative bus lines."""
    result = {
        "status": "unknown",
        "reason": "路线返回字段不完整或不合法",
        "duration_seconds": None,
        "distance_meters": None,
        "walking_distance_meters": None,
        "geometry": [],
        "geometry_complete": False,
        "cost_cny": None,
        "cost_source": "unknown",
        "conditions": [],
    }
    route = response.get("route") if isinstance(response, dict) else None
    paths = (
        route.get("transits" if mode == "transit" else "paths")
        if isinstance(route, dict)
        else None
    )
    if isinstance(paths, list) and not paths:
        result.update(
            status="unavailable", reason="高德返回空路线，不能确认该交通方式可达"
        )
        return result
    if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
        return result
    path = paths[0]
    cost = path.get("cost")
    duration = (
        _quantity(cost.get("duration"), maximum=31_536_000)
        if isinstance(cost, dict)
        else None
    )
    if duration is None:
        duration = _quantity(path.get("duration"), maximum=31_536_000)
    result["duration_seconds"] = math.ceil(duration) if duration is not None else None
    result["distance_meters"] = _quantity(path.get("distance"))
    if mode != "transit":
        result["geometry"], result["geometry_complete"] = _steps_geometry(path)
        if mode == "walking":
            result["walking_distance_meters"] = result["distance_meters"]
    else:
        segments = path.get("segments")
        segments = segments if isinstance(segments, list) else []
        complete, walking_complete, walking, fees_complete, fees = (
            bool(segments),
            bool(segments),
            0.0,
            bool(segments),
            [],
        )
        for segment in segments:
            if not isinstance(segment, dict):
                complete = walking_complete = fees_complete = False
                continue
            walk = segment.get("walking")
            if isinstance(walk, dict) and walk:
                lines, valid = _steps_geometry(walk)
                result["geometry"].extend(lines)
                complete = complete and valid
                distance = _quantity(walk.get("distance"))
                if distance is None:
                    walking_complete = False
                else:
                    walking += distance
            elif walk != []:
                walking_complete = False
            bus = segment.get("bus")
            if isinstance(bus, dict) and bus:
                # v5 refers to the v3 busline shape; steps is also returned by v5 providers.
                lines = bus.get("buslines", bus.get("steps"))
                line = (
                    lines[0]
                    if isinstance(lines, list) and lines and isinstance(lines[0], dict)
                    else None
                )
                points = _polyline(line.get("polyline")) if line else None
                if points:
                    result["geometry"].append(points)
                else:
                    complete = False
                segment_cost = segment.get("cost")
                fee = (
                    _quantity(segment_cost.get("transit_fee"))
                    if isinstance(segment_cost, dict)
                    else None
                )
                if fee is None:
                    fees_complete = False
                else:
                    fees.append(fee)
            if segment.get("railway") or segment.get("taxi"):
                # Stop coordinates are not a returned road/rail polyline.
                complete = fees_complete = False
                if not any(
                    condition["code"] == "transit_segment_unknown"
                    for condition in result["conditions"]
                ):
                    result["conditions"].append(
                        {
                            "code": "transit_segment_unknown",
                            "message": "方案含铁路或出租车分段，线路几何及费用未完整校验",
                        }
                    )
            if (
                not walk
                and not bus
                and not segment.get("railway")
                and not segment.get("taxi")
            ):
                complete = False
        result["geometry_complete"] = complete and bool(result["geometry"])
        total_walk = _quantity(path.get("walking_distance"))
        result["walking_distance_meters"] = (
            total_walk
            if total_walk is not None
            else _quantity(walking)
            if walking_complete
            else None
        )
        fare = _quantity(cost.get("transit_fee")) if isinstance(cost, dict) else None
        if fare is None and fees_complete and fees:
            fare = _quantity(sum(fees))
        result["cost_cny"] = fare
        result["cost_source"] = "source" if fare is not None else "unknown"
    if result["duration_seconds"] is not None and result["distance_meters"] is not None:
        result.update(
            status="available",
            reason="采用高德首个返回方案；耗时为服务取得时的路线估计",
        )
    if not result["geometry_complete"]:
        result["conditions"].append(
            {
                "code": "geometry_unknown",
                "message": "未返回完整可绘制线路，地图仅展示已有真实分段",
            }
        )
    return result


class RouteProvider:
    def __init__(self, amap, database):
        self.amap = amap
        self.database = database

    async def route(self, mode, origin, destination, city, departure_at):
        if mode not in MODES:
            raise ValueError("不支持的交通方式")
        if not isinstance(departure_at, datetime) or departure_at.utcoffset() is None:
            raise ValueError("路线出发时间必须带时区")
        start, finish = _coordinates(origin), _coordinates(destination)
        local = departure_at.astimezone(CHINA_TIME)
        strategy = 0 if mode == "transit" else 32 if mode == "driving" else None
        query = {
            "origin": start,
            "destination": finish,
            "city": normalized_city(city),
            "mode": mode,
            "departure_minute": local.strftime("%Y-%m-%dT%H:%M%z"),
            "strategy": strategy,
            "api_version": "v5",
        }
        key = hashlib.sha256(
            json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        cached = self.database.cached_route(key)
        if cached is not None:
            return cached
        code = None
        try:
            if mode == "transit":
                code = self.database.cached_city_code(city)
                if code is None:
                    code = await self.amap.city_code(city)
                    self.database.save_city_code(city, code)
            response = await self.amap.route(
                mode, start, finish, code, departure_at=local, strategy=strategy
            )
            facts = normalize_route(mode, response)
            infocode = None
        except AmapError as error:
            known_unavailable = error.infocode in {
                "10001",
                "10002",
                "10003",
                "10005",
                "10006",
                "10007",
                "10009",
                "10010",
                "10012",
                "10013",
                "10026",
                "10041",
                "10044",
                "10045",
                "20011",
                "20800",
                "20801",
                "20802",
                "20803",
                "40000",
                "40002",
                "40003",
            }
            facts = normalize_route(mode, {})
            facts.update(
                status="unavailable" if known_unavailable else "unknown",
                reason=str(error),
            )
            infocode = error.infocode if error.infocode != "UNKNOWN" else None
        timestamp = datetime.now(timezone.utc)
        conditions = facts["conditions"]
        if mode in ("bicycling", "electrobike"):
            conditions.append(
                {
                    "code": "shared_vehicle_unverified",
                    "message": "仅验证路线；共享车辆在位、电量、还车条件及租价未知",
                }
            )
        if mode == "driving" and departure_at > timestamp:
            conditions.append(
                {
                    "code": "future_traffic_unverified",
                    "message": "普通驾车接口未校验未来出发时的实时路况",
                }
            )
        leg = RouteLeg.model_validate(
            {
                "id": hashlib.sha256(
                    f"{key}:{timestamp.isoformat()}".encode()
                ).hexdigest(),
                "mode": mode,
                "origin": list(map(float, start.split(","))),
                "destination": list(map(float, finish.split(","))),
                "city_code": code,
                "departure_at": local,
                "read_at": timestamp,
                "valid_until": timestamp + ROUTE_TTL,
                "provider_infocode": infocode,
                "conditional": any(
                    condition["code"]
                    in (
                        "shared_vehicle_unverified",
                        "future_traffic_unverified",
                        "transit_segment_unknown",
                    )
                    for condition in conditions
                ),
                **facts,
            }
        ).model_dump(mode="json")
        return self.database.save_route(key, leg)
