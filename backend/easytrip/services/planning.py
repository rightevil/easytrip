"""Bounded single-day schedules calculated from provider routes and explicit facts."""

import math
import re
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..domain.models import ActivityConstraint, ConfirmedOrigin, TripRequest
from ..domain.places import Poi
from ..integrations.amap import AmapBudgetExceeded, _location
from ..storage.planning import planning_hash


def _current_time():
    return datetime.now(timezone.utc)


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


def _same_location(left, right):
    if not all(
        isinstance(value, (list, tuple)) and len(value) == 2 for value in (left, right)
    ):
        return False
    try:
        return _location(f"{left[0]},{left[1]}") == _location(f"{right[0]},{right[1]}")
    except ValueError:
        return False


def _available(leg, mode, origin, destination, departure):
    return (
        leg["status"] == "available"
        and leg["mode"] == mode
        and _same_location(leg["origin"], origin)
        and _same_location(leg["destination"], destination)
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


def _meal_gaps(itinerary, start, end, zone, origin):
    if start is None or end is None:
        return []
    date = start.astimezone(zone).date()
    activities = [
        item
        for item in itinerary
        if item["kind"] == "activity" and item.get("state") != "skipped"
    ]
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
            anchor = next(
                (
                    item
                    for item in reversed(activities)
                    if item.get("start_at") and _time(item["start_at"]) < window_end
                ),
                None,
            )
            gaps.append(
                _issue(
                    "meal_gap",
                    "行程经过用餐时段，尚无对应餐饮活动",
                    period=period,
                    window_start=max(start, window_start).isoformat(),
                    window_end=min(end, window_end).isoformat(),
                    anchor_candidate_id=anchor["candidate_id"] if anchor else None,
                    anchor=deepcopy(anchor["poi"])
                    if anchor
                    else origin.model_dump(mode="json"),
                )
            )
    return gaps


def _parent_groups(parent, frozen):
    if (
        parent["trip_id"] != frozen["trip_id"]
        or parent["payload"]["input"]["trip_id"] != frozen["trip_id"]
    ):
        raise ValueError("父方案不属于当前行程")
    if parent.get("input_hash") != planning_hash(parent["payload"]["input"]):
        raise ValueError("父方案冻结输入签名无效")
    groups, pending, last = [], [], None
    for step in parent["payload"]["itinerary"]:
        if step["kind"] not in {"route", "wait", "activity"}:
            raise ValueError("父方案包含未支持的时序项目")
        begin, end = _time(step.get("start_at")), _time(step.get("end_at"))
        if begin and end and (end < begin or (last is not None and begin < last)):
            raise ValueError("父方案时序不完整或不一致")
        if end is not None:
            last = end
        pending.append(step)
        if step["kind"] == "activity":
            groups.append(pending)
            pending = []
    return groups


def _insert_meals(order, supplements):
    result = list(order)
    for item in sorted(supplements, key=lambda value: value["meal_window"]["start_at"]):
        anchor = item["meal_window"].get("anchor_candidate_id")
        index = next(
            (
                index
                for index, value in enumerate(result)
                if value["candidate_id"] == anchor
            ),
            len(result),
        )
        result.insert(index, item)
    return result


def _compatible_group(
    group,
    item,
    constraint,
    cursor,
    position,
    request,
    old_request,
    checked_at,
    walking_so_far,
):
    previous = group[-1]
    if (
        previous["candidate_id"] != item["candidate_id"]
        or previous.get("end_at") is None
    ):
        return False
    if (
        previous["poi"]["id"] != item["poi"]["id"]
        or previous["poi"]["location"] != item["poi"]["location"]
    ):
        return False
    if any(
        getattr(constraint, key) != previous.get(key)
        for key in ("duration_minutes", "duration_source", "cost_cny", "cost_source")
    ):
        return False
    if (
        request.planning.transfer_buffer_minutes
        != old_request.planning.transfer_buffer_minutes
    ):
        return False
    begin, end = _time(previous["start_at"]), _time(previous["end_at"])
    if (
        (constraint.fixed_start_at and constraint.fixed_start_at != begin)
        or (constraint.not_before and constraint.not_before > begin)
        or (constraint.not_after and constraint.not_after < end)
    ):
        return False
    routes = [step for step in group if step["kind"] == "route"]
    if len(routes) != 1:
        return False
    leg = routes[0]["leg"]
    if request.planning.max_walk_meters is not None:
        walking = leg.get("walking_distance_meters")
        if (
            walking is None
            or walking_so_far + walking > request.planning.max_walk_meters
        ):
            return False
    mode = leg["mode"]
    if mode in request.planning.disabled_modes or (
        request.transport != "mixed" and request.transport != mode
    ):
        return False
    if mode in {"bicycling", "electrobike"} and (
        constraint.fixed_start_at or request.planning.end_at or request.duration_minutes
    ):
        return False
    return bool(
        leg.get("valid_until")
        and _time(leg["valid_until"]) > checked_at
        and _available(leg, mode, position, item["poi"]["location"], cursor)
    )


async def schedule(
    input: dict,
    route_provider,
    *,
    max_combinations=3,
    parent_plan=None,
    supplemental_candidates=None,
    end_place=None,
    return_origin=None,
):
    """Return suggestions; only the caller owns network budgets, persistence and adoption."""
    if type(max_combinations) is not int or not 1 <= max_combinations <= 3:
        raise ValueError("单轮最多生成 1 至 3 组方案")
    frozen = deepcopy(input)
    request = TripRequest.model_validate(frozen["request"])
    planning = request.planning
    zone = ZoneInfo(planning.timezone)
    trip_start = request.start_at
    start = planning.progress_at or trip_start
    deadline = planning.end_at
    if trip_start and request.duration_minutes is not None:
        duration_deadline = trip_start + timedelta(minutes=request.duration_minutes)
        deadline = min(deadline, duration_deadline) if deadline else duration_deadline
    base_unknowns, base_conflicts = deepcopy(frozen.get("missing", [])), []
    checked_at = _current_time()
    groups = _parent_groups(parent_plan, frozen) if parent_plan else []
    old_request = (
        TripRequest.model_validate(parent_plan["payload"]["input"]["request"])
        if parent_plan
        else None
    )
    supplements = deepcopy(supplemental_candidates or [])
    if parent_plan:
        known = {item["candidate_id"] for item in frozen["selected"] + supplements}
        for item in parent_plan["payload"].get("supplements", []):
            window = item.get("meal_window", {})
            if (
                item["candidate_id"] not in known
                and window.get("start_at")
                and start
                and _time(window["start_at"]).astimezone(zone).date()
                == start.astimezone(zone).date()
            ):
                supplements.append(deepcopy(item))
                known.add(item["candidate_id"])

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
                    "supplements": supplements,
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
    origin = planning.progress_origin or ConfirmedOrigin.model_validate(
        frozen["origin"]
    )
    if planning.progress_at is not None and parent_plan is None:
        return empty(
            "conflict",
            [_issue("progress_parent_missing", "当前位置与时间必须在可靠原方案中调整")],
            conflicts=True,
        )
    if parent_plan and planning.progress_at is None and trip_start < checked_at:
        return empty(
            "draft",
            [
                _issue(
                    "current_progress_required",
                    "原出发时间已过，请提供明确当前位置与当前时间，不能把旧起点当现在",
                )
            ],
        )
    if (
        trip_start
        and start.astimezone(zone).date() != trip_start.astimezone(zone).date()
    ):
        return empty(
            "partial",
            [
                _issue(
                    "single_day_unsupported",
                    "当前进度与原行程不在同一当地日期，P4 不跨日恢复",
                )
            ],
        )
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
    known = {item["candidate_id"] for item in items}
    for item in supplements:
        if item["candidate_id"] not in known:
            items.append(item)
            known.add(item["candidate_id"])
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
    if end_place:
        endpoint_poi = Poi.model_validate(end_place["poi"])
        if (
            not endpoint_poi.id
            or not endpoint_poi.name
            or endpoint_poi.location is None
        ):
            return empty(
                "draft", [_issue("endpoint_unavailable", "指定终点缺少确认地点")]
            )
        cities.add((endpoint_poi.city or "").strip().removesuffix("市"))
    if not cities and return_origin and origin.city:
        cities.add(origin.city.strip().removesuffix("市"))
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
    user_city = (
        (planning.origin_city or "").strip().removesuffix("市")
        if planning.progress_at is None
        else ""
    )
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
    for item in supplements:
        if item["candidate_id"] not in {
            value.candidate_id for value in planning.activity_constraints
        }:
            constraint = ActivityConstraint.model_validate(
                {"candidate_id": item["candidate_id"], **item["constraint"]}
            )
            if constraint.candidate_id != item["candidate_id"]:
                raise ValueError("补查活动与其约束标识不一致")
            constraints[item["candidate_id"]] = constraint
    if any(item["category"] == "eat" for item in items) and (
        planning.dietary_restrictions
        or re.search(r"过敏|忌口|素食|无麸质|不耐受|不吃|不能吃", request.preferences)
    ):
        base_unknowns.append(
            _issue(
                "dietary_unverified",
                "笔记、评论和模型不能证明过敏或忌口安全，需向餐厅核实",
            )
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

    historical, protected_errors = [], []
    previous_activities = {group[-1]["candidate_id"]: group[-1] for group in groups}
    by_id = {item["candidate_id"]: item for item in items}
    protected_ids = {
        item["candidate_id"]
        for item in items
        if constraints[item["candidate_id"]].locked
        or constraints[item["candidate_id"]].state == "completed"
    }
    protected_ids.update(
        identifier
        for identifier, previous in previous_activities.items()
        if previous.get("locked") or previous.get("state") == "completed"
    )
    for identifier in protected_ids:
        item = by_id.get(identifier)
        if item is None:
            protected_errors.append(
                _issue(
                    "protected_selection_missing",
                    "已完成或锁定项目不能从新方案中删除",
                    candidate_id=identifier,
                )
            )
            continue
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
            previous["poi"]["id"] != item["poi"]["id"]
            or previous["poi"]["location"] != item["poi"]["location"]
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
        if previous.get("state") == "completed" and constraint.state != "completed":
            protected_errors.append(
                _issue(
                    "completed_state_regressed",
                    "已完成项目不能恢复成待完成或跳过",
                    candidate_id=identifier,
                )
            )
        if constraint.state == "completed":
            if _time(previous["end_at"]) > start:
                protected_errors.append(
                    _issue(
                        "progress_time_missing",
                        "请提供完成项目之后的当前时间与明确起点",
                        candidate_id=identifier,
                    )
                )
        else:
            if constraint.excluded or constraint.state == "skipped":
                protected_errors.append(
                    _issue(
                        "locked_activity_removed",
                        "原方案锁定项目不能排除或跳过",
                        candidate_id=identifier,
                    )
                )
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
                    "locked": True,
                }
            )
    if protected_errors:
        return empty("conflict", protected_errors, conflicts=True)
    completed_ids = {
        identifier
        for identifier, constraint in constraints.items()
        if constraint.state == "completed"
    }
    completed_count = 0
    if completed_ids:
        if planning.progress_at is None:
            return empty(
                "draft",
                [
                    _issue(
                        "progress_input_missing",
                        "请成对提供明确当前位置和当前时间；不会沿用原起点猜进度",
                    )
                ],
            )
        if frozen.get("active_plan_id") != parent_plan["id"]:
            return empty(
                "conflict",
                [
                    _issue(
                        "historical_parent_not_active",
                        "有已完成项目时必须从当前采用方案重排，旧版不能覆盖进度",
                    )
                ],
                conflicts=True,
            )
        completed_count = max(
            index + 1
            for index, group in enumerate(groups)
            if group[-1]["candidate_id"] in completed_ids
        )
        if any(
            _time(step["start_at"]).astimezone(zone).date()
            != start.astimezone(zone).date()
            or _time(step["end_at"]).astimezone(zone).date()
            != start.astimezone(zone).date()
            for group in groups[:completed_count]
            for step in group
        ):
            return empty(
                "partial",
                [
                    _issue(
                        "single_day_unsupported",
                        "已完成历史不属于当前当地日期；P4 不把跨日恢复当成单日重排",
                    )
                ],
            )
        if any(
            constraints[group[-1]["candidate_id"]].state not in {"completed", "skipped"}
            for group in groups[:completed_count]
        ):
            return empty(
                "conflict",
                [
                    _issue(
                        "historical_state_unconfirmed",
                        "已完成项目之前还有待完成项目；请明确标记已完成或跳过，不能猜测历史行程",
                    )
                ],
                conflicts=True,
            )
        changed_path = False
        for group in groups[:completed_count]:
            state = constraints[group[-1]["candidate_id"]].state
            changed_path = changed_path or state == "skipped"
            for step in group:
                saved = {**deepcopy(step), "historical": True}
                if state == "skipped":
                    saved.update(execution_unknown=True, untravelled=True)
                elif changed_path and saved["kind"] != "activity":
                    saved["execution_unknown"] = True
                if saved["kind"] == "activity":
                    saved["state"] = state
                    saved["locked"] = (
                        saved.get("locked", False)
                        or constraints[saved["candidate_id"]].locked
                    )
                historical.append(saved)
        if historical and (historical[-1]["poi"]["location"] != origin.location):
            base_unknowns.append(
                _issue(
                    "progress_movement_unknown",
                    "完成项目后到当前确认位置的实际移动、步行和费用未知，未用旧路线代替",
                )
            )
    items = [
        item
        for item in items
        if not constraints[item["candidate_id"]].excluded
        and constraints[item["candidate_id"]].state == "pending"
    ]
    if not items and not historical:
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
    core = [
        item
        for item in items
        if item["candidate_id"] not in {value["candidate_id"] for value in supplements}
    ]
    meals = [
        item
        for item in items
        if item["candidate_id"] in {value["candidate_id"] for value in supplements}
    ]
    suggestions = (
        _orders(core, constraints, max_combinations)
        if core
        else [("remaining_suffix", [])]
    )
    for index, (label, order) in enumerate(suggestions):
        order = _insert_meals(order, meals)
        itinerary, unknowns, conflicts = (
            deepcopy(historical),
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
        reused_ids, reused_leg_ids = set(), set()
        reused_walk = sum(
            step["leg"].get("walking_distance_meters") or 0
            for step in historical
            if step["kind"] == "route" and not step.get("execution_unknown")
        )
        if parent_plan:
            for group, item in zip(groups[completed_count:], order):
                if not _compatible_group(
                    group,
                    item,
                    constraints[item["candidate_id"]],
                    cursor,
                    position,
                    request,
                    old_request,
                    checked_at,
                    reused_walk,
                ):
                    break
                saved = deepcopy(group)
                for step in saved:
                    step.pop("historical", None)
                    if step["kind"] == "route":
                        validity.append(_time(step["leg"]["valid_until"]))
                        reused_leg_ids.add(step["leg"]["id"])
                        reused_walk += step["leg"].get("walking_distance_meters") or 0
                saved[-1].update(
                    state="pending",
                    locked=constraints[item["candidate_id"]].locked,
                    required=constraints[item["candidate_id"]].required,
                )
                itinerary.extend(saved)
                reused_ids.add(item["candidate_id"])
                cursor, position, from_id = (
                    _time(saved[-1]["end_at"]),
                    item["poi"]["location"],
                    item["candidate_id"],
                )
            if reused_ids:
                diff.append(
                    _issue(
                        "unchanged_prefix_reused",
                        "不受影响的路线和活动前缀保持原样，未重复查询",
                        candidate_ids=list(reused_ids),
                    )
                )
            unknowns.extend(
                deepcopy(issue)
                for issue in parent_plan["payload"]["unknowns"]
                if (
                    issue.get("candidate_id") in reused_ids
                    or issue.get("leg_id") in reused_leg_ids
                )
                and issue["code"] != "route_fare_estimated"
            )
        usage.update(
            reused_route_count=len(reused_leg_ids),
            historical_route_count=0,
            historical_walking_distance_meters=0.0,
            historical_known_cost_cny=0.0,
        )
        for step in itinerary:
            if step.get("execution_unknown"):
                if step["kind"] != "wait":
                    usage["walking_known"] = False
                    usage["unknown_cost_items"] += 1
                    unknowns.append(
                        _issue(
                            "historical_execution_unknown",
                            "跳过项目改变了实际路线；原快照保留供比较，实际步行与支出未知",
                            candidate_id=step.get("candidate_id"),
                            leg_id=step.get("leg", {}).get("id"),
                        )
                    )
                continue
            old_cost = (
                step.get("cost_cny")
                if step["kind"] == "activity"
                else step.get("leg", {}).get("cost_cny")
            )
            historic = step.get("historical", False)
            before_cost, before_walk = (
                usage["known_cost_cny"],
                usage["walking_distance_meters"],
            )
            if step["kind"] == "route":
                leg = step["leg"]
                walk = leg.get("walking_distance_meters")
                if walk is None:
                    usage["walking_known"] = False
                    if planning.max_walk_meters is not None:
                        unknowns.append(
                            _issue(
                                "historical_walking_unknown"
                                if historic
                                else "walking_unknown",
                                "缺少路线步行量，累计上限无法保证",
                                leg_id=leg["id"],
                            )
                        )
                else:
                    usage["walking_distance_meters"] += walk
                if historic:
                    usage["historical_route_count"] += 1
                if old_cost is not None and leg["mode"] == "transit":
                    passengers = (
                        old_request.participants if historic else request.participants
                    )
                    amount = old_cost * passengers
                    usage["estimated_cost_cny"] += amount
                    unknowns.append(
                        _issue(
                            "route_fare_estimated",
                            "公交金额按原对应人数计算，实际付款未知",
                            leg_id=leg["id"],
                            participants=passengers,
                            estimated_total_cny=amount,
                        )
                    )
                elif old_cost is not None:
                    usage["known_cost_cny"] += old_cost
            elif step["kind"] == "activity":
                if old_cost is not None:
                    usage[
                        "known_cost_cny"
                        if step["cost_source"] == "user"
                        else "estimated_cost_cny"
                    ] += old_cost
            if step["kind"] != "wait" and old_cost is None:
                usage["unknown_cost_items"] += 1
                unknowns.append(
                    _issue(
                        "historical_cost_unknown" if historic else "cost_unknown",
                        "该项目费用未知，未清零历史支出",
                        candidate_id=step.get("candidate_id"),
                        leg_id=step.get("leg", {}).get("id"),
                    )
                )
            if historic:
                usage["historical_known_cost_cny"] += (
                    usage["known_cost_cny"] - before_cost
                )
                usage["historical_walking_distance_meters"] += (
                    usage["walking_distance_meters"] - before_walk
                )
        if any(issue["code"] == "progress_movement_unknown" for issue in unknowns):
            usage["walking_known"] = False
            usage["unknown_cost_items"] += 1
        order = [item for item in order if item["candidate_id"] not in reused_ids]

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
                    or deadline is not None,
                )
            if cursor is None or (budget_exhausted and not routed):
                if constraint.locked and identifier in previous_activities:
                    itinerary.append(
                        {**deepcopy(previous_activities[identifier]), "locked": True}
                    )
                else:
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
            locked_late = (
                constraint.locked
                and constraint.fixed_start_at
                and earliest > constraint.fixed_start_at
            )
            if constraint.locked and identifier in previous_activities:
                itinerary.append(
                    {**deepcopy(previous_activities[identifier]), "locked": True}
                )
            else:
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
            cursor = None if locked_late else finish

        endpoint = None
        if end_place:
            endpoint = (end_place["poi"]["location"], end_place["candidate_id"])
        elif planning.return_to_origin:
            endpoint = (
                ConfirmedOrigin.model_validate(
                    return_origin or frozen["origin"]
                ).location,
                None,
            )
        elif planning.end_candidate_id:
            end_item = next(
                (
                    item
                    for item in by_id.values()
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
        if endpoint and (
            endpoint[0] != position or (endpoint[1] != from_id and end_place is None)
        ):
            if cursor is not None and not budget_exhausted:
                await route_to(*endpoint, appointment=deadline is not None)
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
        if (
            planning.max_walk_meters is not None
            and usage["walking_distance_meters"] > planning.max_walk_meters
        ):
            conflicts.append(
                _issue(
                    "walking_limit",
                    "包含历史前缀的累计已知步行超过用户上限",
                    meters=usage["walking_distance_meters"],
                    maximum=planning.max_walk_meters,
                )
            )
        if cursor and cursor > deadline:
            conflicts.append(
                _issue("deadline_conflict", "保留前缀和新后缀后的结束超过原行程期限")
            )
        unknowns.extend(_meal_gaps(itinerary, trip_start, cursor, zone, origin))
        if not validity and historical and not order:
            incomplete = True
            unknowns.append(
                _issue(
                    "no_future_schedule", "项目已全部完成，没有可声明未来有效的交通方案"
                )
            )
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
                    "supplements": supplements,
                },
            }
        )
        if budget_exhausted:
            break
    return results
