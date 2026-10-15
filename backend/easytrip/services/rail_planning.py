"""User-confirmed railway timetables composed with independently checked city legs."""

from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..domain.models import TripRequest
from ..storage.planning import planning_hash
from ..storage.routes import normalized_city
from . import planning as city_planning
from . import planning_adjustments as adjustments
from .day_planning import _anchor, _day, _issue
from .lodgings import check_stay


def cross_city(frozen):
    cities = {
        place["poi"].get("city")
        for place in [*frozen["selected"], *frozen.get("lodgings", [])]
    }
    cities.add(
        (frozen.get("origin") or {}).get("city")
        or frozen["request"].get("planning", {}).get("origin_city")
    )
    return len({normalized_city(city) for city in cities if city}) > 1


def _rail_item(rail, stations):
    return {
        "kind": "rail",
        "rail_id": rail.id,
        "date": rail.date.isoformat(),
        "start_at": rail.departure_at.isoformat() if rail.departure_at else None,
        "end_at": rail.arrival_at.isoformat() if rail.arrival_at else None,
        "train_number": rail.train_number,
        "departure_station": deepcopy(stations.get(rail.departure_station_id)),
        "arrival_station": deepcopy(stations.get(rail.arrival_station_id)),
        "source": "user",
        "confirmed": rail.confirmed,
        "boarding_buffer_minutes": rail.boarding_buffer_minutes,
        "arrival_buffer_minutes": rail.arrival_buffer_minutes,
        "cost_cny": rail.cost_cny,
        "cost_source": "user" if rail.cost_cny is not None else "unknown",
    }


async def schedule(
    frozen,
    provider,
    *,
    max_combinations=3,
    parent_plan=None,
    supplemental_candidates=None,
):
    if type(max_combinations) is not int or not 1 <= max_combinations <= 3:
        raise ValueError("单轮最多生成 1 至 3 组方案")
    request = TripRequest.model_validate(frozen["request"])
    planning, zone = request.planning, ZoneInfo(request.planning.timezone)
    windows = request.planning_windows()
    stations = {place["candidate_id"]: place for place in frozen.get("stations", [])}
    constraints = {item.candidate_id: item for item in planning.activity_constraints}
    rails = sorted(
        planning.rail_segments,
        key=lambda rail: (
            rail.date,
            rail.departure_at.timestamp() if rail.departure_at else float("-inf"),
            rail.id,
        ),
    )
    problems = deepcopy(frozen.get("missing", []))
    conflicts = []
    if not (frozen.get("origin") or {}).get("city") and not planning.origin_city:
        problems.append(
            _issue("origin_city_unknown", "铁路前序接驳需要明确出发点所属城市")
        )
    if not windows or any(not day.start_at or not day.end_at for day in windows):
        problems.append(
            _issue("day_window_missing", "铁路编排需要覆盖全程的明确本地日期窗口")
        )
    deadline = planning.end_at
    if request.start_at and request.duration_minutes is not None:
        end = request.start_at + timedelta(minutes=request.duration_minutes)
        deadline = min(deadline, end) if deadline else end
    if (
        windows
        and request.start_at
        and deadline
        and (
            windows[0].date != request.start_at.astimezone(zone).date()
            or windows[-1].date != deadline.astimezone(zone).date()
        )
    ):
        problems.append(
            _issue("day_coverage_missing", "铁路日窗必须覆盖出发至结束的全部本地日期")
        )
    if not rails:
        problems.append(
            _issue(
                "rail_timetable_unknown",
                "跨城衔接缺少用户确认的车站、车次和时刻，保留待确定铁路的条件草案",
            )
        )
    for rail in rails:
        if not rail.confirmed:
            problems.append(
                _issue(
                    "rail_timetable_unknown",
                    "车次与时刻尚未由用户明确确认，未推断余票或准点保证",
                    rail_id=rail.id,
                    date=rail.date.isoformat(),
                )
            )
        for identifier in (rail.departure_station_id, rail.arrival_station_id):
            station = stations.get(identifier)
            if (
                not station
                or not station["poi"].get("city")
                or not any(
                    kind in (station["poi"].get("type") or "")
                    for kind in ("火车站", "铁路客运")
                )
            ):
                problems.append(
                    _issue(
                        "rail_station_unconfirmed",
                        "铁路两端必须选择已核对的具体火车站 POI，不能用地铁站或泛地区替代",
                        rail_id=rail.id,
                        candidate_id=identifier,
                    )
                )
    adjustment = adjustments.prepare(frozen, parent_plan, request)
    problems.extend(adjustment["issues"])
    conflicts.extend(adjustment["conflicts"])
    if (
        parent_plan
        and planning.progress_at
        and any(
            rail.confirmed
            and rail.departure_at < planning.progress_at < rail.arrival_at
            for rail in rails
        )
    ):
        problems.append(
            _issue(
                "rail_in_progress_unverified",
                "当前时间处于已确认班次的运行区间，实际乘车和到达状态未知；保留时刻与后续条件草案，请在到达后明确当前位置",
            )
        )
    if not parent_plan and any(
        item.locked or item.state == "completed" for item in constraints.values()
    ):
        conflicts.append(
            _issue("protected_snapshot_missing", "已完成或锁定项目需要可靠父方案")
        )
    if problems or conflicts:
        days = [_day(window) for window in windows]
        for index, day in enumerate(days):
            day["origin"] = deepcopy(frozen["origin"]) if index == 0 else None
            day["itinerary"] = [
                {
                    **city_planning._activity(
                        place,
                        constraints.get(place["candidate_id"])
                        or city_planning.ActivityConstraint(
                            candidate_id=place["candidate_id"]
                        ),
                    ),
                    "date": day["date"],
                }
                for place in frozen["selected"]
                if place["category"] != "stay"
                and (
                    len(days) == 1
                    or (
                        constraints.get(place["candidate_id"])
                        and constraints[place["candidate_id"]].date
                        and constraints[place["candidate_id"]].date.isoformat()
                        == day["date"]
                    )
                )
            ]
            day["itinerary"].extend(
                _rail_item(rail, stations)
                for rail in rails
                if rail.date.isoformat() == day["date"]
            )
            if index < len(days) - 1:
                overnight_rail = next(
                    (
                        rail
                        for rail in rails
                        if rail.confirmed
                        and rail.departure_at
                        and rail.arrival_at
                        and rail.departure_at.astimezone(zone).date().isoformat()
                        <= day["date"]
                        < rail.arrival_at.astimezone(zone).date().isoformat()
                    ),
                    None,
                )
                if overnight_rail:
                    day["overnight_rail_id"] = overnight_rail.id
                else:
                    stay = next(
                        (
                            stay
                            for stay in planning.overnight_stays
                            if stay.date.isoformat() == day["date"]
                        ),
                        None,
                    )
                    hotel = next(
                        (
                            place
                            for place in frozen.get("lodgings", [])
                            if stay and place["candidate_id"] == stay.candidate_id
                        ),
                        None,
                    )
                    if hotel:
                        day.update(
                            lodging_candidate_id=hotel["candidate_id"],
                            endpoint=_anchor(hotel),
                        )
                        day["unknowns"].append(
                            _issue(
                                "lodging_details_unverified",
                                "酒店房态、平台实时报价、办理和行李条件待核实，接驳尚未完成",
                                date=day["date"],
                            )
                        )
                    else:
                        day["unknowns"].append(
                            _issue(
                                "lodging_gap",
                                "本晚酒店尚未明确，铁路缺口不替代住宿缺口",
                                date=day["date"],
                                anchor=next(
                                    (
                                        step["poi"]
                                        for step in reversed(day["itinerary"])
                                        if step["kind"] == "activity"
                                    ),
                                    day.get("origin"),
                                ),
                                nights=1,
                                price_status="unknown",
                            )
                        )
            problems.extend(day["unknowns"])
        return [
            {
                "status": "conflict" if conflicts else "partial",
                "payload": {
                    "input": deepcopy(frozen),
                    "days": days,
                    "itinerary": [step for day in days for step in day["itinerary"]],
                    "unknowns": problems,
                    "conflicts": conflicts,
                    "usage": {"route_requests": 0},
                    "diff": [],
                    "valid_until": None,
                    "supplements": [],
                },
            }
        ]
    for left, right in zip(rails, rails[1:]):
        if right.departure_at < left.arrival_at + timedelta(
            minutes=left.arrival_buffer_minutes + right.boarding_buffer_minutes
        ):
            conflicts.append(
                _issue(
                    "train_connection_conflict",
                    "铁路段之间没有保留用户设置的进出站缓冲",
                    rail_id=right.id,
                )
            )
    pools, unassigned = [], []
    for window in windows:
        items = []
        for place in frozen["selected"]:
            item = constraints.get(
                place["candidate_id"]
            ) or city_planning.ActivityConstraint(candidate_id=place["candidate_id"])
            constraints.setdefault(place["candidate_id"], item)
            if place["category"] == "stay" or item.excluded or item.state == "skipped":
                continue
            if item.date == window.date or (
                not planning.day_windows and len(windows) == 1
            ):
                items.append(place)
            elif item.date is None and window == windows[0]:
                unassigned.append(
                    _issue(
                        "activity_date_missing",
                        "多日活动需要明确日期",
                        candidate_id=place["candidate_id"],
                    )
                )
        pools.append(city_planning._orders(items, constraints, max_combinations))
    hotels = {place["candidate_id"]: place for place in frozen.get("lodgings", [])}
    stays = {
        stay.date: hotels.get(stay.candidate_id) for stay in planning.overnight_stays
    }
    night_rules = {stay.date.isoformat(): stay for stay in planning.overnight_stays}
    results, seen = [], set()
    for combination in range(max_combinations):
        orders = [deepcopy(pool[min(combination, len(pool) - 1)][1]) for pool in pools]
        signature = tuple(
            tuple(place["candidate_id"] for place in order) for order in orders
        )
        if signature in seen:
            continue
        seen.add(signature)
        days, unknowns, current_conflicts, validity = (
            [],
            deepcopy(unassigned),
            deepcopy(conflicts),
            [],
        )
        usage = {
            "route_requests": 0,
            "walking_distance_meters": 0.0,
            "walking_known": True,
            "known_cost_cny": 0.0,
            "estimated_cost_cny": 0.0,
            "unknown_cost_items": 0,
            "combination_index": combination,
            "combination_kind": "rail_and_city",
        }
        incomplete = bool(unassigned)
        pending = None
        for index, (window, order) in enumerate(zip(windows, orders)):
            day = _day(window)
            date = window.date.isoformat()
            if date in adjustment["reused"]:
                day = deepcopy(adjustment["reused"][date])
                adjustments.add_usage(usage, day, request.participants)
                days.append(day)
                unknowns.extend(day["unknowns"])
                current_conflicts.extend(day["conflicts"])
                validity.extend(
                    city_planning._time(step["leg"]["valid_until"])
                    for step in day["itinerary"]
                    if step["kind"] == "route"
                    and not step.get("historical")
                    and step["leg"].get("valid_until")
                )
                pending = next(
                    (rail for rail in rails if rail.id == day.get("overnight_rail_id")),
                    None,
                )
                continue
            previous_hotel = stays.get(windows[index - 1].date) if index else None
            origin = (
                deepcopy(frozen["origin"])
                if index == 0
                else _anchor(previous_hotel)
                if previous_hotel
                else None
            )
            if index == 0 and origin and not origin.get("city"):
                origin["city"] = planning.origin_city
            cursor = window.start_at
            if pending:
                day["continuing_rail_id"] = pending.id
                if pending.arrival_at.astimezone(zone).date() > window.date:
                    day["overnight_rail_id"] = pending.id
                    if order:
                        day["conflicts"].append(
                            _issue(
                                "activity_during_rail",
                                "本日仍在跨日铁路上，不能同时安排市内活动",
                                date=date,
                            )
                        )
                    days.append(day)
                    current_conflicts.extend(day["conflicts"])
                    continue
                origin = _anchor(stations[pending.arrival_station_id])
                cursor = max(
                    cursor,
                    pending.arrival_at
                    + timedelta(minutes=pending.arrival_buffer_minutes),
                )
                pending = None
            day["origin"] = deepcopy(origin)
            if index and not day.get("continuing_rail_id"):
                check_stay(
                    day,
                    window,
                    night_rules.get(windows[index - 1].date.isoformat()),
                    checkout=True,
                    progress_at=planning.progress_at
                    if planning.progress_at
                    and planning.progress_at.astimezone(zone).date() == window.date
                    else None,
                )
            progress_today = bool(
                planning.progress_at
                and planning.progress_at.astimezone(zone).date() == window.date
            )
            progress_consumed = False
            passed_ids = set()
            if progress_today and parent_plan:
                old_day = adjustment["parents"].get(date)
                old_rails = {
                    rail.id: rail
                    for rail in TripRequest.model_validate(
                        parent_plan["payload"]["input"]["request"]
                    ).planning.rail_segments
                }
                passed_ids = {
                    rail.id
                    for rail in old_rails.values()
                    if rail.date == window.date
                    and rail.arrival_at
                    and rail.arrival_at <= planning.progress_at
                }
                current_rails = {rail.id: rail for rail in rails}
                if any(
                    identifier not in current_rails
                    or current_rails[identifier] != old_rails[identifier]
                    for identifier in passed_ids
                ):
                    day["conflicts"].append(
                        _issue(
                            "historical_rail_changed",
                            "已经过去的铁路确认快照不能改写",
                            date=date,
                        )
                    )
                if passed_ids and old_day:
                    cutoff = max(
                        i
                        for i, step in enumerate(old_day["itinerary"])
                        if step["kind"] == "rail" and step["rail_id"] in passed_ids
                    )
                    while (
                        cutoff + 1 < len(old_day["itinerary"])
                        and old_day["itinerary"][cutoff + 1]["kind"] == "wait"
                        and city_planning._time(
                            old_day["itinerary"][cutoff + 1]["end_at"]
                        )
                        <= planning.progress_at
                    ):
                        cutoff += 1
                    prefix = deepcopy(old_day["itinerary"][: cutoff + 1])
                    done_ids = set()
                    for step in prefix:
                        step["historical"] = True
                        if step["kind"] == "activity":
                            item = constraints.get(step["candidate_id"])
                            done_ids.add(step["candidate_id"])
                            if item is None or item.state not in {
                                "completed",
                                "skipped",
                            }:
                                day["conflicts"].append(
                                    _issue(
                                        "historical_state_unconfirmed",
                                        "过去铁路之前的活动仍未明确完成或跳过",
                                        date=date,
                                    )
                                )
                            else:
                                step.update(
                                    state=item.state,
                                    locked=step.get("locked", False) or item.locked,
                                    required=item.required,
                                )
                                if item.state == "skipped":
                                    step.update(
                                        execution_unknown=True, untravelled=True
                                    )
                        elif step["kind"] != "wait":
                            step["execution_unknown"] = True
                    day["itinerary"].extend(prefix)
                    adjustments.add_usage(
                        usage, {"itinerary": prefix}, request.participants
                    )
                    order = [
                        place
                        for place in order
                        if place["candidate_id"] not in done_ids
                    ]
                    day["unknowns"].append(
                        _issue(
                            "historical_execution_unknown",
                            "保留原铁路确认时刻与历史计划；实际交通、准点与支出未知",
                            date=date,
                        )
                    )
                origin = planning.progress_origin.model_dump(mode="json")
                cursor = max(window.start_at, planning.progress_at)
                if passed_ids:
                    ready = max(
                        old_rails[identifier].arrival_at
                        + timedelta(
                            minutes=old_rails[identifier].arrival_buffer_minutes
                        )
                        for identifier in passed_ids
                    )
                    if ready > cursor:
                        day["itinerary"].append(
                            {
                                "kind": "wait",
                                "date": date,
                                "start_at": cursor.isoformat(),
                                "end_at": ready.isoformat(),
                                "reason": "保留尚未结束的出站接驳缓冲",
                            }
                        )
                        cursor = ready

            async def city_segment(items, endpoint, end, before_rail_id=None):
                nonlocal cursor, origin, incomplete, progress_consumed
                if not origin or cursor is None:
                    incomplete = True
                    day["unknowns"].append(
                        _issue(
                            "day_origin_unknown",
                            "起点或前序衔接未知，未计算市内路线",
                            date=date,
                        )
                    )
                    return "partial"
                if cursor >= end:
                    day["conflicts"].append(
                        _issue(
                            "train_connection_conflict",
                            "前序时间已越过铁路缓冲或当日硬期限",
                            date=date,
                        )
                    )
                    return "conflict"
                if not items and endpoint is None:
                    return "validated"
                daily = deepcopy(frozen)
                daily.update(
                    origin=origin,
                    selected=items,
                    selected_ids=[place["candidate_id"] for place in items],
                    missing=[],
                )
                daily["request"].update(
                    start_at=cursor.isoformat(), duration_minutes=None, budget_cny=None
                )
                daily["request"]["planning"].update(
                    day_windows=[
                        {
                            "date": date,
                            "start_at": cursor.isoformat(),
                            "end_at": end.isoformat(),
                        }
                    ],
                    overnight_stays=[],
                    rail_segments=[],
                    progress_at=planning.progress_at.isoformat()
                    if progress_today and not progress_consumed
                    else None,
                    progress_origin=planning.progress_origin.model_dump(mode="json")
                    if progress_today and not progress_consumed
                    else None,
                    origin_city=origin.get("city"),
                    end_at=end.isoformat(),
                    return_to_origin=False,
                    end_candidate_id=None,
                    activity_constraints=[
                        constraints[place["candidate_id"]].model_dump(mode="json")
                        for place in items
                    ],
                )
                daily["planning"] = daily["request"]["planning"]
                daily["input_hash"] = planning_hash(daily)
                supplements = [
                    item
                    for item in (supplemental_candidates or [])
                    if item.get("meal_window")
                    and city_planning._time(item["meal_window"]["start_at"])
                    .astimezone(zone)
                    .date()
                    == window.date
                    and normalized_city(item["poi"]["city"])
                    == normalized_city(origin["city"])
                ]
                result = (
                    await city_planning.schedule(
                        daily,
                        provider,
                        max_combinations=1,
                        parent_plan=adjustments.rail_city_parent(
                            parent_plan, adjustment["parents"].get(date), before_rail_id
                        ),
                        end_place=endpoint,
                        supplemental_candidates=supplements,
                    )
                )[0]
                progress_consumed = True
                payload = result["payload"]
                day["itinerary"].extend(
                    {**step, "date": date} for step in payload["itinerary"]
                )
                day["unknowns"].extend(
                    {**issue, "date": date} for issue in payload["unknowns"]
                )
                day["conflicts"].extend(
                    {**issue, "date": date} for issue in payload["conflicts"]
                )
                for key in (
                    "route_requests",
                    "walking_distance_meters",
                    "known_cost_cny",
                    "estimated_cost_cny",
                    "unknown_cost_items",
                ):
                    usage[key] += payload["usage"].get(key, 0)
                usage["walking_known"] &= payload["usage"].get("walking_known", True)
                if payload["valid_until"]:
                    validity.append(city_planning._time(payload["valid_until"]))
                timed = [step for step in payload["itinerary"] if step.get("end_at")]
                if timed:
                    cursor = city_planning._time(timed[-1]["end_at"])
                if result["status"] in {"partial", "draft"}:
                    incomplete = True
                ok = result["status"] in {"validated", "conditional"}
                if ok:
                    if endpoint:
                        origin = _anchor(endpoint)
                    elif items:
                        origin = _anchor(items[-1])
                return result["status"]

            for rail in (
                value
                for value in rails
                if value.date == window.date and value.id not in passed_ids
            ):
                departure, arrival = (
                    stations[rail.departure_station_id],
                    stations[rail.arrival_station_id],
                )
                items = []
                while (
                    order
                    and origin
                    and normalized_city(order[0]["poi"]["city"])
                    == normalized_city(origin["city"])
                ):
                    items.append(order.pop(0))
                if not origin or normalized_city(origin["city"]) != normalized_city(
                    departure["poi"]["city"]
                ):
                    day["conflicts"].append(
                        _issue(
                            "rail_gap",
                            "当前地点与出发站不在同城，缺少可靠的前序跨城段",
                            rail_id=rail.id,
                            date=date,
                        )
                    )
                else:
                    connected = await city_segment(
                        items,
                        departure,
                        rail.departure_at
                        - timedelta(minutes=rail.boarding_buffer_minutes),
                        before_rail_id=rail.id,
                    )
                    if connected == "conflict":
                        day["conflicts"].append(
                            _issue(
                                "train_connection_conflict",
                                "活动、两端接驳或缓冲未通过赶车校验",
                                rail_id=rail.id,
                                date=date,
                            )
                        )
                    elif connected in {"draft", "partial"}:
                        incomplete = True
                        day["unknowns"].append(
                            _issue(
                                "rail_connection_unverified",
                                "接驳资料或路线未取得，赶车可行性未知",
                                rail_id=rail.id,
                                date=date,
                            )
                        )
                    elif cursor < rail.departure_at:
                        day["itinerary"].append(
                            {
                                "kind": "wait",
                                "date": date,
                                "start_at": cursor.isoformat(),
                                "end_at": rail.departure_at.isoformat(),
                                "reason": "用户设置的进站缓冲及候车；不代表已购票或保证准点",
                            }
                        )
                day["itinerary"].append(_rail_item(rail, stations))
                if rail.cost_cny is None:
                    usage["unknown_cost_items"] += 1
                else:
                    usage["known_cost_cny"] += rail.cost_cny
                day["unknowns"].append(
                    _issue(
                        "rail_service_unverified",
                        "用户确认的时刻不证明已购票、余票、实时票价、准点或一定赶上换乘；缓冲需按实际车站核实",
                        rail_id=rail.id,
                        date=date,
                    )
                )
                origin = _anchor(arrival)
                cursor = rail.arrival_at + timedelta(
                    minutes=rail.arrival_buffer_minutes
                )
                if rail.arrival_at.astimezone(zone).date() > window.date:
                    pending = rail
                    day["overnight_rail_id"] = rail.id
                    break
                if rail.arrival_buffer_minutes:
                    day["itinerary"].append(
                        {
                            "kind": "wait",
                            "date": date,
                            "start_at": rail.arrival_at.isoformat(),
                            "end_at": cursor.isoformat(),
                            "reason": "用户设置的出站接驳缓冲，需核实车站实际条件",
                        }
                    )
            hotel = stays.get(window.date) if index < len(windows) - 1 else None
            if pending:
                if hotel:
                    day["conflicts"].append(
                        _issue(
                            "lodging_rail_conflict",
                            "本晚同时绑定酒店和跨日铁路，请明确真实过夜安排",
                            date=date,
                        )
                    )
                if order:
                    day["conflicts"].append(
                        _issue(
                            "activity_during_rail",
                            "跨日出发后仍有本日活动未完成",
                            date=date,
                        )
                    )
            else:
                endpoint = hotel
                if index == len(windows) - 1:
                    if planning.return_to_origin:
                        endpoint = {
                            "candidate_id": "trip-origin",
                            "poi": {
                                "id": frozen["origin"].get("poi_id") or "user-origin",
                                "name": frozen["origin"]["name"],
                                "city": frozen["origin"].get("city")
                                or planning.origin_city,
                                "location": frozen["origin"]["location"],
                                "read_at": request.start_at.isoformat(),
                                "api_version": "user",
                            },
                        }
                    elif planning.end_candidate_id:
                        endpoint = next(
                            (
                                place
                                for place in frozen["selected"]
                                if place["candidate_id"] == planning.end_candidate_id
                            ),
                            None,
                        )
                if origin and any(
                    normalized_city(place["poi"]["city"])
                    != normalized_city(origin["city"])
                    for place in [*order, *([endpoint] if endpoint else [])]
                ):
                    incomplete = True
                    day["unknowns"].append(
                        _issue(
                            "rail_gap",
                            "尚缺连接活动或酒店城市的可靠铁路段，未调用跨城市内路线",
                            date=date,
                        )
                    )
                    day["itinerary"].extend(
                        {
                            **city_planning._activity(
                                place, constraints[place["candidate_id"]]
                            ),
                            "date": date,
                        }
                        for place in order
                    )
                else:
                    await city_segment(order, endpoint, window.end_at)
                    day["endpoint"] = deepcopy(origin)
                    if index == len(windows) - 1 and planning.return_to_origin:
                        day["endpoint"] = deepcopy(frozen["origin"])
                if index < len(windows) - 1:
                    if hotel:
                        day["lodging_candidate_id"] = hotel["candidate_id"]
                        usage["unknown_cost_items"] += 1
                        day["unknowns"].append(
                            _issue(
                                "lodging_details_unverified",
                                "房态、平台实时可订价格、入住/退房办理时长、行李寄存分别未知，未声称预订完成",
                                date=date,
                                candidate_id=hotel["candidate_id"],
                            )
                        )
                    else:
                        incomplete = True
                        day["unknowns"].append(
                            _issue(
                                "lodging_gap",
                                "本晚酒店与区域仍需明确绑定；次日起点未知",
                                date=date,
                                anchor=deepcopy(origin),
                                nights=1,
                                price_status="unknown",
                            )
                        )
            if hotel and not pending:
                check_stay(day, window, night_rules.get(window.date.isoformat()))
            days.append(day)
            unknowns.extend(day["unknowns"])
            current_conflicts.extend(day["conflicts"])
        for field in (
            "walking_distance_meters",
            "known_cost_cny",
            "estimated_cost_cny",
            "unknown_cost_items",
        ):
            usage[field] = 0
        usage["walking_known"] = True
        for day in days:
            for step in day["itinerary"]:
                if step.get("historical") and step["kind"] in {"route", "rail"}:
                    step["execution_unknown"] = True
            rule = night_rules.get(day["date"])
            adjustments.add_usage(
                usage,
                day,
                request.participants,
                rule.nightly_cost_cny if rule else None,
            )
        if pending:
            incomplete = True
            unknowns.append(
                _issue("day_coverage_missing", "跨日铁路到达时间未被后续日窗覆盖")
            )
        if (
            planning.max_walk_meters is not None
            and usage["walking_distance_meters"] > planning.max_walk_meters
        ):
            current_conflicts.append(
                _issue("walking_limit", "包含全部铁路两端接驳的累计步行超过上限")
            )
        if request.budget_cny is not None:
            if usage["known_cost_cny"] > request.budget_cny:
                current_conflicts.append(
                    _issue(
                        "budget_conflict", "活动、铁路与接驳的全程已知费用超过总预算"
                    )
                )
            if usage["unknown_cost_items"] or usage["estimated_cost_cny"]:
                unknowns.append(
                    _issue(
                        "budget_unverified",
                        "铁路、酒店或接驳费用未知/估计，无法保证总预算",
                    )
                )
        results.append(
            {
                "status": "conflict"
                if current_conflicts
                else "partial"
                if incomplete
                else "conditional",
                "payload": {
                    "input": deepcopy(frozen),
                    "days": days,
                    "itinerary": [step for day in days for step in day["itinerary"]],
                    "unknowns": unknowns,
                    "conflicts": current_conflicts,
                    "usage": usage,
                    "diff": [
                        _issue(
                            "rail_recomputed",
                            "保留未受影响的日期与原历史，按确认铁路时刻核对相关日和下游接驳",
                            reused_dates=list(adjustment["reused"]),
                        )
                    ]
                    if parent_plan
                    else [],
                    "valid_until": min(validity).isoformat()
                    if validity
                    else (
                        city_planning._current_time() + timedelta(minutes=30)
                    ).isoformat(),
                    "supplements": deepcopy(supplemental_candidates or []),
                },
            }
        )
    return results
