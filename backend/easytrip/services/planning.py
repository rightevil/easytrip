"""Bounded single-day schedules calculated from provider routes and explicit facts."""

import math
from copy import deepcopy
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..domain.models import ActivityConstraint, ConfirmedOrigin, TripRequest
from ..domain.places import Poi
from ..integrations.amap import AmapBudgetExceeded


def _time(value):
    if value is None:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("排程时间必须包含时区")
    return result


def _issue(code, message, **details):
    return {"code": code, "message": message, **details}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _available(leg, mode, origin, destination, departure):
    return (
        leg["status"] == "available"
        and leg["mode"] == mode
        and leg["origin"] == origin
        and leg["destination"] == destination
        and _time(leg["departure_at"]) == departure
        and leg["duration_source"] == "amap"
        and _number(leg.get("duration_seconds"))
        and _number(leg.get("distance_meters"))
        and (leg["duration_seconds"] > 0 or leg["distance_meters"] == 0)
        and (
            leg.get("walking_distance_meters") is None
            or _number(leg["walking_distance_meters"])
        )
        and (leg.get("cost_cny") is None or _number(leg["cost_cny"]))
    )


def _activity(item, constraint, start=None, end=None):
    return {
        "kind": "activity",
        "candidate_id": item["candidate_id"],
        "name": item["name"],
        "poi": deepcopy(item["poi"]),
        "category": item["category"],
        "start_at": start.isoformat() if start else None,
        "end_at": end.isoformat() if end else None,
        **{
            key: getattr(constraint, key)
            for key in (
                "duration_minutes",
                "duration_source",
                "cost_cny",
                "cost_source",
                "state",
                "locked",
                "required",
            )
        },
    }


def _orders(items, constraints, limit):
    # A fixed appointment is mandatory even if the user did not also tick 必去.
    mandatory = {
        item["candidate_id"]
        for item in items
        if constraints[item["candidate_id"]].required
        or constraints[item["candidate_id"]].locked
        or constraints[item["candidate_id"]].fixed_start_at is not None
    }
    suggestions = [("selected_order", items), ("reverse_order", list(reversed(items)))]
    gentle = [
        item
        for index, item in enumerate(items)
        if item["candidate_id"] in mandatory or item["category"] == "eat" or index == 0
    ]
    suggestions.append(("gentle", gentle))
    seen, result = set(), []
    for label, order in suggestions:
        signature = tuple(item["candidate_id"] for item in order)
        if signature not in seen:
            seen.add(signature)
            result.append((label, order))
        if len(result) == limit:
            break
    return result


def _meal_gaps(itinerary, start, end, zone):
    if start is None or end is None:
        return []
    date = start.astimezone(zone).date()
    activities = [item for item in itinerary if item["kind"] == "activity"]
    gaps = []
    for period, opening, closing in (("lunch", 11, 14), ("dinner", 17, 20)):
        window_start = datetime.combine(date, time(opening), zone)
        window_end = datetime.combine(date, time(closing), zone)
        if end <= window_start or start >= window_end:
            continue
        meal = any(
            item["category"] == "eat"
            and item.get("start_at")
            and item.get("end_at")
            and _time(item["start_at"]) < window_end
            and _time(item["end_at"]) > window_start
            for item in activities
        )
        if not meal:
            gaps.append(
                _issue("meal_gap", "行程经过用餐时段，尚无对应餐饮活动", period=period)
            )
    return gaps


async def schedule(
    input: dict, route_provider, *, max_combinations=3, parent_plan=None
):
    """Return suggestions; only the caller owns network budgets, persistence and adoption."""
    if type(max_combinations) is not int or not 1 <= max_combinations <= 3:
        raise ValueError("单轮最多生成 1 至 3 组方案")
    frozen = deepcopy(input)
    request = TripRequest.model_validate(frozen["request"])
    planning = request.planning
    zone = ZoneInfo(planning.timezone)
    start = request.start_at
    deadline = planning.end_at
    if start and request.duration_minutes is not None:
        duration_deadline = start + timedelta(minutes=request.duration_minutes)
        deadline = min(deadline, duration_deadline) if deadline else duration_deadline
    base_unknowns, base_conflicts = deepcopy(frozen.get("missing", [])), []

    def empty(status, issues, *, conflicts=False):
        return [
            {
                "status": status,
                "payload": {
                    "input": frozen,
                    "itinerary": [],
                    "unknowns": [] if conflicts else issues,
                    "conflicts": issues if conflicts else [],
                    "usage": {"route_requests": 0},
                    "diff": [],
                    "valid_until": None,
                },
            }
        ]

    if base_unknowns:
        known_conflicts = [
            item for item in base_unknowns if item["code"] == "cross_city"
        ]
        if known_conflicts:
            return empty("conflict", known_conflicts, conflicts=True)
        return empty("draft", base_unknowns)
    if start is None or deadline is None or frozen.get("origin") is None:
        return empty(
            "draft",
            [_issue("planning_input_missing", "请补齐起点、出发时间和结束期限")],
        )
    origin = ConfirmedOrigin.model_validate(frozen["origin"])
    if deadline <= start:
        return empty(
            "conflict",
            [_issue("deadline_conflict", "结束期限不晚于出发时间")],
            conflicts=True,
        )
    if deadline.astimezone(zone).date() != start.astimezone(zone).date():
        return empty(
            "partial",
            [
                _issue(
                    "single_day_unsupported", "P4 只支持同一当地日期，跨日部分尚未排程"
                )
            ],
        )

    items = deepcopy(frozen["selected"])
    if len({item["candidate_id"] for item in items}) != len(items):
        raise ValueError("规划活动标识不能重复")
    for item in items:
        poi = Poi.model_validate(item["poi"])
        if (
            item.get("kind", "place") != "place"
            or not poi.id
            or not poi.name
            or poi.location is None
        ):
            return empty(
                "draft",
                [
                    _issue(
                        "poi_unconfirmed",
                        "活动缺少已确认的真实地点",
                        candidate_id=item["candidate_id"],
                    )
                ],
            )
    cities = {
        (item["poi"].get("city") or "").strip().removesuffix("市") for item in items
    }
    if "" in cities or not cities:
        return empty("draft", [_issue("city_unknown", "无法核对活动所属城市")])
    if len(cities) != 1:
        return empty(
            "conflict",
            [_issue("cross_city", "P4 不将跨城路线当作单城行程")],
            conflicts=True,
        )
    city = next(iter(cities))
    provider_city = (origin.city or "").strip().removesuffix("市")
    user_city = (planning.origin_city or "").strip().removesuffix("市")
    if provider_city and user_city and provider_city != user_city:
        return empty(
            "conflict",
            [_issue("origin_city_conflict", "用户确认的起点城市与高德地点信息不一致")],
            conflicts=True,
        )
    origin_city = provider_city or user_city
    if origin_city and origin_city != city:
        return empty(
            "conflict",
            [_issue("cross_city", "已确认起点与活动位于不同城市；P4 不校验跨城衔接")],
            conflicts=True,
        )
    if not origin_city:
        base_unknowns.append(
            _issue(
                "origin_city_unknown", "起点所属城市未知，坐标未被推断为当前活动城市"
            )
        )
    constraints = {
        item["candidate_id"]: ActivityConstraint(candidate_id=item["candidate_id"])
        for item in items
    }
    constraints.update(
        {item.candidate_id: item for item in planning.activity_constraints}
    )
    if planning.end_candidate_id in constraints and (
        constraints[planning.end_candidate_id].excluded
        or constraints[planning.end_candidate_id].state == "skipped"
    ):
        return empty(
            "conflict",
            [
                _issue(
                    "endpoint_excluded", "指定终点同时被排除或跳过，请明确其中一个条件"
                )
            ],
            conflicts=True,
        )
    unavailable = [
        item.candidate_id
        for item in planning.activity_constraints
        if item.candidate_id not in {value["candidate_id"] for value in items}
        and (
            item.required
            or item.locked
            or item.state == "completed"
            or item.fixed_start_at
        )
    ]
    if unavailable:
        return empty(
            "conflict",
            [
                _issue(
                    "constraint_unavailable",
                    "硬约束活动没有可核对的地点",
                    candidate_ids=unavailable,
                )
            ],
            conflicts=True,
        )

    completed, protected_errors = [], []
    protected = [
        item
        for item in items
        if constraints[item["candidate_id"]].locked
        or constraints[item["candidate_id"]].state == "completed"
    ]
    previous_activities = {}
    if parent_plan:
        if parent_plan.get("trip_id") != frozen["trip_id"]:
            raise ValueError("父方案不属于当前行程")
        previous_activities = {
            item["candidate_id"]: item
            for item in parent_plan["payload"]["itinerary"]
            if item["kind"] == "activity"
        }
    for item in protected:
        identifier = item["candidate_id"]
        constraint = constraints[identifier]
        previous = previous_activities.get(identifier)
        if (
            previous is None
            or not previous.get("start_at")
            or not previous.get("end_at")
        ):
            protected_errors.append(
                _issue(
                    "protected_snapshot_missing",
                    "已完成或锁定项目需要可靠的原方案快照",
                    candidate_id=identifier,
                )
            )
            continue
        if (
            previous.get("poi") != item["poi"]
            or (
                constraint.duration_minutes is not None
                and constraint.duration_minutes != previous.get("duration_minutes")
            )
            or (
                constraint.cost_cny is not None
                and constraint.cost_cny != previous.get("cost_cny")
            )
        ):
            protected_errors.append(
                _issue(
                    "protected_activity_changed",
                    "不能修改已完成或锁定活动的地点、时长或费用",
                    candidate_id=identifier,
                )
            )
            continue
        if constraint.state == "completed":
            if _time(previous["end_at"]) > start:
                protected_errors.append(
                    _issue(
                        "progress_time_missing",
                        "请提供完成项目之后的当前时间与明确起点",
                        candidate_id=identifier,
                    )
                )
            completed.append({**deepcopy(previous), "state": "completed"})
        else:
            if constraint.fixed_start_at and constraint.fixed_start_at != _time(
                previous["start_at"]
            ):
                protected_errors.append(
                    _issue(
                        "locked_time_changed",
                        "锁定项目的预约时间不能改变",
                        candidate_id=identifier,
                    )
                )
            constraints[identifier] = constraint.model_copy(
                update={
                    "fixed_start_at": _time(previous["start_at"]),
                    "duration_minutes": previous["duration_minutes"],
                    "duration_source": previous["duration_source"],
                    "cost_cny": previous.get("cost_cny"),
                    "cost_source": previous.get("cost_source", "unknown"),
                }
            )
    if protected_errors:
        return empty("conflict", protected_errors, conflicts=True)
    completed.sort(key=lambda item: _time(item["start_at"]))
    items = [
        item
        for item in items
        if not constraints[item["candidate_id"]].excluded
        and constraints[item["candidate_id"]].state == "pending"
    ]
    if not items:
        return empty(
            "draft", [_issue("pending_selection_missing", "没有需要排程的活动")]
        )
    mandatory = [
        item
        for item in items
        if constraints[item["candidate_id"]].required
        or constraints[item["candidate_id"]].locked
        or constraints[item["candidate_id"]].fixed_start_at
    ]
    if len(mandatory) > 20:
        return empty(
            "partial",
            [_issue("activity_limit", "超过单轮 20 个活动上限；硬约束活动没有被删除")],
        )
    if len(items) > 20:
        kept = {item["candidate_id"] for item in mandatory}
        for item in items:
            if len(kept) == 20:
                break
            kept.add(item["candidate_id"])
        omitted = [
            item["candidate_id"] for item in items if item["candidate_id"] not in kept
        ]
        base_unknowns.append(
            _issue(
                "activity_limit",
                "超过 20 个活动的部分仍待排程",
                omitted_candidate_ids=omitted,
            )
        )
        items = [item for item in items if item["candidate_id"] in kept]

    disabled = set(planning.disabled_modes)
    modes = (
        [request.transport] if request.transport != "mixed" else ["walking", "transit"]
    )
    modes = [mode for mode in modes if mode not in disabled]
    if not modes and (
        request.transport != "mixed" or {"bicycling", "electrobike"}.issubset(disabled)
    ):
        return empty(
            "conflict",
            [_issue("transport_disabled", "指定交通方式已被用户禁用")],
            conflicts=True,
        )
    results = []
    for index, (label, order) in enumerate(
        _orders(items, constraints, max_combinations)
    ):
        itinerary, unknowns, conflicts = (
            deepcopy(completed),
            deepcopy(base_unknowns),
            deepcopy(base_conflicts),
        )
        diff = []
        omitted = [item["candidate_id"] for item in items if item not in order]
        if omitted:
            diff.append(
                _issue(
                    "gentle_selection",
                    "较轻松方案保留全部必去、预约与锁定活动，其余项目可比较后决定",
                    omitted_candidate_ids=omitted,
                )
            )
        usage = {
            "route_requests": 0,
            "walking_distance_meters": 0.0,
            "walking_known": True,
            "known_cost_cny": 0.0,
            "estimated_cost_cny": 0.0,
            "unknown_cost_items": 0,
            "buffer_minutes": planning.transfer_buffer_minutes,
            "combination_index": index,
            "combination_kind": label,
        }
        cursor, position, from_id = start, origin.location, None
        validity, incomplete, budget_exhausted = (
            [],
            any(item["code"] == "activity_limit" for item in base_unknowns),
            False,
        )

        async def route_to(destination, to_id, *, appointment=False):
            nonlocal cursor, position, from_id, incomplete, budget_exhausted
            if cursor is None:
                return False
            candidates = []
            failures = []
            attempted_modes = list(modes)
            if request.transport == "mixed":
                attempted_modes.extend(
                    mode
                    for mode in ("bicycling", "electrobike")
                    if mode not in disabled
                )
            for mode_index, mode in enumerate(attempted_modes):
                if mode_index == len(modes) and candidates:
                    break
                try:
                    usage["route_requests"] += 1
                    leg = await route_provider.route(
                        mode=mode,
                        origin=position,
                        destination=destination,
                        city=city,
                        departure_at=cursor,
                    )
                except AmapBudgetExceeded:
                    incomplete, budget_exhausted = True, True
                    unknowns.append(
                        _issue(
                            "route_budget_exhausted",
                            "本轮路线查询预算已耗尽，后续路段尚未计算",
                        )
                    )
                    break
                if _available(leg, mode, position, destination, cursor):
                    candidates.append(leg)
                else:
                    failures.append(leg)
            if budget_exhausted and not candidates:
                return False
            if not candidates:
                incomplete = True
                unknowns.append(
                    _issue(
                        "route_unavailable",
                        "允许的交通方式没有可计算的路线，未假定抵达时间",
                        to_candidate_id=to_id,
                        modes=[leg["mode"] for leg in failures],
                        provider_reasons=[
                            {
                                "mode": leg["mode"],
                                "reason": leg.get("reason"),
                                "provider_infocode": leg.get("provider_infocode"),
                            }
                            for leg in failures
                        ],
                    )
                )
                cursor = None
                return False
            if planning.max_walk_meters is not None:
                safe = [
                    leg
                    for leg in candidates
                    if leg.get("walking_distance_meters") is not None
                    and usage["walking_distance_meters"]
                    + leg["walking_distance_meters"]
                    <= planning.max_walk_meters
                ]
                if safe:
                    candidates = safe
            leg = min(
                candidates,
                key=lambda value: (
                    value.get("conditional", False),
                    value["duration_seconds"],
                    value["mode"],
                ),
            )
            if leg["mode"] in {"bicycling", "electrobike"}:
                unknowns.append(
                    _issue(
                        "shared_vehicle_unverified",
                        "共享车辆在位、电量、还车与租价尚未确认",
                        leg_id=leg["id"],
                    )
                )
                if appointment:
                    conflicts.append(
                        _issue(
                            "shared_only_appointment",
                            "预约或硬返程不能只依赖未确认可用的共享车辆",
                            leg_id=leg["id"],
                            relaxations=["确认可用车辆", "改用可核对交通方式"],
                        )
                    )
            if leg.get("conditional"):
                unknowns.append(
                    _issue(
                        "route_conditional",
                        "路线仅在列出的条件成立时可用",
                        leg_id=leg["id"],
                        conditions=leg.get("conditions", []),
                    )
                )
            walk = leg.get("walking_distance_meters")
            if walk is None:
                usage["walking_known"] = False
                if planning.max_walk_meters is not None:
                    unknowns.append(
                        _issue(
                            "walking_unknown",
                            "缺少接驳步行量，无法保证累计步行上限",
                            leg_id=leg["id"],
                        )
                    )
            else:
                usage["walking_distance_meters"] += walk
            if (
                planning.max_walk_meters is not None
                and usage["walking_distance_meters"] > planning.max_walk_meters
            ):
                conflicts.append(
                    _issue(
                        "walking_limit",
                        "累计已知步行距离超过用户上限",
                        meters=usage["walking_distance_meters"],
                        maximum=planning.max_walk_meters,
                        relaxations=["改用其他交通", "减少活动", "明确调整步行上限"],
                    )
                )
            cost = leg.get("cost_cny")
            if cost is None:
                usage["unknown_cost_items"] += 1
                unknowns.append(
                    _issue("route_cost_unknown", "该交通段费用未知", leg_id=leg["id"])
                )
            elif leg["mode"] == "transit":
                amount = cost * request.participants
                usage["estimated_cost_cny"] += amount
                unknowns.append(
                    _issue(
                        "route_fare_estimated",
                        "按高德公交票价和同行人数计算；优惠、票种及实际付款尚未确认",
                        leg_id=leg["id"],
                        per_person_cny=cost,
                        participants=request.participants,
                        estimated_total_cny=amount,
                    )
                )
            else:
                usage["known_cost_cny"] += cost
            arrival = cursor + timedelta(seconds=leg["duration_seconds"])
            itinerary.append(
                {
                    "kind": "route",
                    "start_at": cursor.isoformat(),
                    "end_at": arrival.isoformat(),
                    "from_candidate_id": from_id,
                    "to_candidate_id": to_id,
                    "leg": deepcopy(leg),
                }
            )
            if leg.get("valid_until"):
                validity.append(_time(leg["valid_until"]))
            else:
                unknowns.append(
                    _issue(
                        "route_validity_unknown",
                        "路线数据没有可核对的有效期限",
                        leg_id=leg["id"],
                    )
                )
            cursor, position, from_id = arrival, destination, to_id
            return True

        for item in order:
            identifier = item["candidate_id"]
            constraint = constraints[identifier]
            routed = False
            if cursor is not None and not budget_exhausted:
                routed = await route_to(
                    item["poi"]["location"],
                    identifier,
                    appointment=constraint.fixed_start_at is not None
                    or planning.end_at is not None,
                )
            if cursor is None or (budget_exhausted and not routed):
                itinerary.append(_activity(item, constraint))
                incomplete = True
                continue
            if planning.transfer_buffer_minutes:
                buffered = cursor + timedelta(minutes=planning.transfer_buffer_minutes)
                itinerary.append(
                    {
                        "kind": "wait",
                        "start_at": cursor.isoformat(),
                        "end_at": buffered.isoformat(),
                        "reason": "程序设置的活动前缓冲；未重复加入公交接口已包含的接驳步行",
                    }
                )
                cursor = buffered
            earliest = max(
                [cursor]
                + [
                    value
                    for value in (constraint.not_before, constraint.fixed_start_at)
                    if value
                ]
            )
            if earliest > cursor:
                itinerary.append(
                    {
                        "kind": "wait",
                        "start_at": cursor.isoformat(),
                        "end_at": earliest.isoformat(),
                        "reason": "等待用户约定时间窗或预约时间",
                    }
                )
            if constraint.fixed_start_at and earliest > constraint.fixed_start_at:
                conflicts.append(
                    _issue(
                        "appointment_late",
                        "真实路线和缓冲无法按时到达固定预约",
                        candidate_id=identifier,
                        arrival_at=cursor.isoformat(),
                        fixed_start_at=constraint.fixed_start_at.isoformat(),
                        relaxations=["减少前序活动", "更换交通", "明确修改预约"],
                    )
                )
            cursor = earliest
            duration = constraint.duration_minutes
            finish = (
                cursor + timedelta(minutes=duration) if duration is not None else None
            )
            itinerary.append(_activity(item, constraint, cursor, finish))
            unknowns.append(
                _issue(
                    "opening_unknown",
                    "当前开放、入场及预约可用性尚未核实",
                    candidate_id=identifier,
                )
            )
            if duration is None:
                unknowns.append(
                    _issue(
                        "duration_unknown",
                        "活动停留时长未知，后续时间未补造",
                        candidate_id=identifier,
                    )
                )
                incomplete = True
            elif constraint.duration_source != "user":
                unknowns.append(
                    _issue(
                        "duration_estimated"
                        if constraint.duration_source == "estimated"
                        else "duration_source_unverified",
                        "活动时长来自估计或历史来源，需确认",
                        candidate_id=identifier,
                    )
                )
            if constraint.cost_cny is None:
                usage["unknown_cost_items"] += 1
                unknowns.append(
                    _issue(
                        "activity_cost_unknown",
                        "当前活动费用未知，未填为免费",
                        candidate_id=identifier,
                    )
                )
            elif constraint.cost_source == "user":
                usage["known_cost_cny"] += constraint.cost_cny
            else:
                usage["estimated_cost_cny"] += constraint.cost_cny
                unknowns.append(
                    _issue(
                        "activity_cost_unverified",
                        "活动费用为来源报告或估计，不能证明当前价格",
                        candidate_id=identifier,
                    )
                )
            if finish and constraint.not_after and finish > constraint.not_after:
                conflicts.append(
                    _issue(
                        "activity_window_conflict",
                        "活动无法在用户时间窗内完成",
                        candidate_id=identifier,
                        relaxations=["缩短停留", "调整顺序", "明确修改时间窗"],
                    )
                )
            if finish and finish > deadline:
                conflicts.append(
                    _issue(
                        "deadline_conflict",
                        "活动结束超过可用时间或硬性结束期限",
                        candidate_id=identifier,
                        relaxations=["减少活动", "缩短停留", "明确修改结束期限"],
                    )
                )
            if (
                finish
                and finish.astimezone(zone).date() != start.astimezone(zone).date()
            ):
                incomplete = True
                unknowns.append(
                    _issue(
                        "single_day_unsupported",
                        "活动跨越当地午夜，跨日部分尚未校验",
                        candidate_id=identifier,
                    )
                )
            cursor = finish

        endpoint = None
        if planning.return_to_origin:
            endpoint = (origin.location, None)
        elif planning.end_candidate_id:
            end_item = next(
                (
                    item
                    for item in frozen["selected"]
                    if item["candidate_id"] == planning.end_candidate_id
                ),
                None,
            )
            if end_item is None:
                conflicts.append(
                    _issue("endpoint_unavailable", "指定终点缺少本行程确认地点")
                )
            else:
                endpoint = (end_item["poi"]["location"], end_item["candidate_id"])
        if endpoint and (endpoint[0] != position or endpoint[1] != from_id):
            if cursor is not None and not budget_exhausted:
                await route_to(*endpoint, appointment=planning.end_at is not None)
            else:
                incomplete = True
                unknowns.append(
                    _issue(
                        "return_route_uncomputed", "前序缺口使终点或返程路线尚未计算"
                    )
                )
        if cursor and cursor > deadline:
            conflicts.append(
                _issue(
                    "deadline_conflict",
                    "包含终点或返程后的时间超过结束期限",
                    relaxations=["减少活动", "更换交通", "明确修改结束期限"],
                )
            )
        if request.budget_cny is not None:
            if usage["known_cost_cny"] > request.budget_cny:
                conflicts.append(
                    _issue(
                        "budget_conflict",
                        "已知费用已超过用户总预算",
                        known_cost_cny=usage["known_cost_cny"],
                        maximum=request.budget_cny,
                        relaxations=["减少付费活动", "改用其他交通", "明确调整预算"],
                    )
                )
            if usage["unknown_cost_items"] or usage["estimated_cost_cny"]:
                unknowns.append(
                    _issue("budget_unverified", "存在未知或估计费用，无法保证不超支")
                )
        unknowns.extend(_meal_gaps(itinerary, start, cursor, zone))
        if parent_plan:
            before = [
                item["candidate_id"]
                for item in parent_plan["payload"]["itinerary"]
                if item["kind"] == "activity"
            ]
            after = [
                item["candidate_id"] for item in itinerary if item["kind"] == "activity"
            ]
            if before != after:
                diff.append(
                    _issue(
                        "activity_order_changed",
                        "活动组合或顺序已变化",
                        before=before,
                        after=after,
                    )
                )
        status = (
            "conflict"
            if conflicts
            else "partial"
            if incomplete
            else "conditional"
            if unknowns
            else "validated"
        )
        results.append(
            {
                "status": status,
                "payload": {
                    "input": frozen,
                    "itinerary": itinerary,
                    "unknowns": unknowns,
                    "conflicts": conflicts,
                    "usage": usage,
                    "diff": diff,
                    "valid_until": min(validity).isoformat() if validity else None,
                },
            }
        )
        if budget_exhausted:
            break
    return results
