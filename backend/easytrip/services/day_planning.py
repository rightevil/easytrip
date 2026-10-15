"""Compose explicit local-day windows with the existing single-day scheduler."""

from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..domain.models import ConfirmedOrigin, TripRequest
from ..storage.planning import planning_hash
from . import planning as single_day
from . import planning_adjustments as adjustments
from .lodgings import check_stay


def _anchor(place):
    poi = place["poi"]
    return ConfirmedOrigin(
        name=poi["name"],
        location=poi["location"],
        poi_id=poi["id"],
        source="amap",
        city=poi["city"],
    ).model_dump(mode="json")


def _day(window, origin=None, endpoint=None, lodging_id=None):
    return {
        **window.model_dump(mode="json"),
        "origin": origin,
        "endpoint": endpoint,
        "lodging_candidate_id": lodging_id,
        "itinerary": [],
        "unknowns": [],
        "conflicts": [],
    }


def _issue(code, message, **details):
    return {"code": code, "message": message, **details}


def _empty(input, request, status, issues):
    days = [_day(window) for window in request.planning.day_windows]
    return [
        {
            "status": status,
            "payload": {
                "input": deepcopy(input),
                "days": days,
                "itinerary": [],
                "unknowns": [] if status == "conflict" else issues,
                "conflicts": issues if status == "conflict" else [],
                "usage": {"route_requests": 0},
                "diff": [],
                "supplements": [],
                "valid_until": None,
            },
        }
    ]


async def schedule(
    input,
    route_provider,
    *,
    max_combinations=3,
    parent_plan=None,
    supplemental_candidates=None,
):
    request = TripRequest.model_validate(input["request"])
    from . import rail_planning

    if request.planning.rail_segments or rail_planning.cross_city(input):
        return await rail_planning.schedule(
            input,
            route_provider,
            max_combinations=max_combinations,
            parent_plan=parent_plan,
            supplemental_candidates=supplemental_candidates,
        )
    if not request.planning.day_windows:
        return await single_day.schedule(
            input,
            route_provider,
            max_combinations=max_combinations,
            parent_plan=parent_plan,
            supplemental_candidates=supplemental_candidates,
        )
    if type(max_combinations) is not int or not 1 <= max_combinations <= 3:
        raise ValueError("单轮最多生成 1 至 3 组方案")
    frozen = deepcopy(input)
    frozen.pop("lodging_candidates", None)
    planning = request.planning
    windows = planning.day_windows
    zone = ZoneInfo(planning.timezone)
    deadline = planning.end_at
    if request.start_at and request.duration_minutes is not None:
        duration_end = request.start_at + timedelta(minutes=request.duration_minutes)
        deadline = min(deadline, duration_end) if deadline else duration_end
    if frozen.get("missing"):
        cross_city = any(issue["code"] == "cross_city" for issue in frozen["missing"])
        return _empty(
            frozen, request, "conflict" if cross_city else "draft", frozen["missing"]
        )
    if request.start_at is None or deadline is None or not frozen.get("origin"):
        return _empty(
            frozen,
            request,
            "draft",
            [_issue("planning_input_missing", "请补齐起点、出发时间和结束期限")],
        )
    if (
        windows[0].date != request.start_at.astimezone(zone).date()
        or windows[-1].date != deadline.astimezone(zone).date()
    ):
        return _empty(
            frozen,
            request,
            "partial",
            [
                _issue(
                    "day_coverage_missing",
                    "每日窗口尚未覆盖出发至结束的全部本地日期；过夜部分未省略",
                )
            ],
        )
    adjustment = adjustments.prepare(frozen, parent_plan, request)
    if adjustment["conflicts"] or adjustment["issues"]:
        return _empty(
            frozen,
            request,
            "conflict" if adjustment["conflicts"] else "draft",
            adjustment["conflicts"] or adjustment["issues"],
        )
    if not parent_plan and any(
        item.locked or item.state == "completed"
        for item in planning.activity_constraints
    ):
        return _empty(
            frozen,
            request,
            "conflict",
            [_issue("protected_snapshot_missing", "已完成或锁定项目需要可靠原方案")],
        )

    constraints = {item.candidate_id: item for item in planning.activity_constraints}
    end_constraint = constraints.get(planning.end_candidate_id)
    if end_constraint and (
        end_constraint.excluded or end_constraint.state == "skipped"
    ):
        return _empty(
            frozen,
            request,
            "conflict",
            [
                _issue(
                    "endpoint_excluded", "指定终点同时被排除或跳过，请明确其中一个条件"
                )
            ],
        )
    pools = {}
    unassigned = []
    for window in windows:
        items = []
        for item in frozen["selected"]:
            constraint = constraints.get(item["candidate_id"])
            if item["category"] == "stay":
                continue
            if constraint and (constraint.excluded or constraint.state == "skipped"):
                continue
            if not constraint or constraint.date is None:
                if window == windows[0]:
                    unassigned.append(
                        _issue(
                            "activity_date_missing",
                            "请明确活动所属日期",
                            candidate_id=item["candidate_id"],
                        )
                    )
            elif constraint.date == window.date:
                items.append(item)
        pools[window.date] = single_day._orders(
            items,
            {item["candidate_id"]: constraints[item["candidate_id"]] for item in items},
            max_combinations,
        )
    lodgings = {item["candidate_id"]: item for item in frozen.get("lodgings", [])}
    stays = {
        stay.date: lodgings.get(stay.candidate_id) for stay in planning.overnight_stays
    }
    night_rules = {stay.date.isoformat(): stay for stay in planning.overnight_stays}
    supplemental_candidates = supplemental_candidates or []
    signatures, results = set(), []
    for combination in range(max_combinations):
        orders = [pool[min(combination, len(pool) - 1)][1] for pool in pools.values()]
        signature = tuple(
            tuple(item["candidate_id"] for item in order) for order in orders
        )
        if signature in signatures:
            continue
        signatures.add(signature)
        days, unknowns, conflicts, validity = [], deepcopy(unassigned), [], []
        usage = {
            "route_requests": 0,
            "walking_distance_meters": 0.0,
            "walking_known": True,
            "known_cost_cny": 0.0,
            "estimated_cost_cny": 0.0,
            "unknown_cost_items": 0,
            "combination_index": combination,
            "combination_kind": "daily_windows",
        }
        incomplete = bool(unassigned)
        for index, (window, order) in enumerate(zip(windows, orders)):
            date = window.date.isoformat()
            if date in adjustment["reused"]:
                day = deepcopy(adjustment["reused"][date])
                adjustments.add_usage(usage, day, request.participants)
                days.append(day)
                unknowns.extend(day["unknowns"])
                conflicts.extend(day["conflicts"])
                validity.extend(
                    single_day._time(step["leg"]["valid_until"])
                    for step in day["itinerary"]
                    if step["kind"] == "route"
                    and not step.get("historical")
                    and step["leg"].get("valid_until")
                )
                continue
            hotel = stays.get(window.date) if index < len(windows) - 1 else None
            previous_hotel = stays.get(windows[index - 1].date) if index else None
            origin = (
                frozen["origin"]
                if index == 0
                else _anchor(previous_hotel)
                if previous_hotel
                else None
            )
            end_place = hotel
            if index == len(windows) - 1 and planning.end_candidate_id:
                end_place = next(
                    (
                        item
                        for item in frozen["selected"]
                        if item["candidate_id"] == planning.end_candidate_id
                    ),
                    None,
                )
            day = _day(
                window,
                origin,
                _anchor(end_place) if end_place else None,
                hotel["candidate_id"] if hotel else None,
            )
            if index:
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
            if index < len(windows) - 1:
                if not hotel:
                    incomplete = True
                    day["unknowns"].append(
                        _issue(
                            "lodging_gap",
                            "这一晚尚无本行程已确认且明确绑定的酒店",
                            date=date,
                            anchor=deepcopy(order[-1]["poi"] if order else origin),
                            nights=1,
                            price_status="unknown",
                        )
                    )
                else:
                    day["unknowns"].append(
                        _issue(
                            "lodging_details_unverified",
                            "酒店房态、平台实时可订价格、入住/退房办理时长与行李寄存条件未知，未假定固定时间或免费",
                            candidate_id=hotel["candidate_id"],
                            date=date,
                        )
                    )
                    usage["unknown_cost_items"] += 1
            if origin is None:
                day["unknowns"].append(
                    _issue(
                        "day_origin_unknown",
                        "前一晚酒店未明确，次日起点与接驳时间未知",
                        date=date,
                    )
                )
            if window.start_at is None or window.end_at is None:
                day["unknowns"].append(
                    _issue("day_window_missing", "请明确当日开始和结束时间", date=date)
                )
            if origin is None or window.start_at is None or window.end_at is None:
                incomplete = True
                day["itinerary"] = [
                    {
                        **single_day._activity(item, constraints[item["candidate_id"]]),
                        "date": date,
                    }
                    for item in order
                ]
                if hotel:
                    day["unknowns"].append(
                        _issue(
                            "lodging_transfer_unknown",
                            "缺少起点或时间，前往酒店的接驳尚未核实",
                            date=date,
                        )
                    )
            elif (
                not order
                and end_place is None
                and not (index == len(windows) - 1 and planning.return_to_origin)
            ):
                day["endpoint"] = deepcopy(origin)
            else:
                daily = deepcopy(frozen)
                daily["selected"] = deepcopy(order)
                daily["selected_ids"] = [item["candidate_id"] for item in order]
                daily["origin"] = origin
                daily["missing"] = []
                day_request = deepcopy(frozen["request"])
                day_request.update(
                    start_at=window.start_at.isoformat(),
                    duration_minutes=None,
                    budget_cny=None,
                )
                day_request["planning"].update(
                    end_at=window.end_at.isoformat(),
                    day_windows=[window.model_dump(mode="json")],
                    overnight_stays=[],
                    progress_at=planning.progress_at.isoformat()
                    if planning.progress_at
                    and planning.progress_at.astimezone(zone).date() == window.date
                    else None,
                    progress_origin=planning.progress_origin.model_dump(mode="json")
                    if planning.progress_at
                    and planning.progress_at.astimezone(zone).date() == window.date
                    else None,
                    origin_city=origin.get("city") or planning.origin_city,
                    end_candidate_id=None,
                    return_to_origin=index == len(windows) - 1
                    and planning.return_to_origin,
                    activity_constraints=[
                        constraints[item["candidate_id"]].model_dump(mode="json")
                        for item in order
                    ],
                )
                if planning.max_walk_meters is not None:
                    day_request["planning"]["max_walk_meters"] = max(
                        0,
                        planning.max_walk_meters
                        - int(usage["walking_distance_meters"]),
                    )
                daily["request"] = day_request
                daily["planning"] = day_request["planning"]
                daily["input_hash"] = planning_hash(daily)
                supplements = [
                    item
                    for item in supplemental_candidates
                    if single_day._time(item["meal_window"]["start_at"])
                    .astimezone(zone)
                    .date()
                    == window.date
                ]
                result = (
                    await single_day.schedule(
                        daily,
                        route_provider,
                        max_combinations=1,
                        parent_plan=adjustments.city_parent(
                            parent_plan, adjustment["parents"].get(date)
                        ),
                        end_place=end_place,
                        return_origin=frozen["origin"],
                        supplemental_candidates=supplements,
                    )
                )[0]
                payload = result["payload"]
                day["itinerary"] = [
                    {**step, "date": date} for step in payload["itinerary"]
                ]
                day["unknowns"].extend(
                    {**issue, "date": date} for issue in payload["unknowns"]
                )
                day["conflicts"].extend(
                    {**issue, "date": date} for issue in payload["conflicts"]
                )
                incomplete |= result["status"] in {"partial", "draft"}
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
                    validity.append(single_day._time(payload["valid_until"]))
                if day["endpoint"] is None and day["itinerary"]:
                    if index == len(windows) - 1 and planning.return_to_origin:
                        day["endpoint"] = deepcopy(frozen["origin"])
                    else:
                        activity = next(
                            (
                                step
                                for step in reversed(day["itinerary"])
                                if step["kind"] == "activity"
                            ),
                            None,
                        )
                        if activity:
                            day["endpoint"] = _anchor({"poi": activity["poi"]})
            if hotel:
                check_stay(day, window, night_rules.get(window.date.isoformat()))
            days.append(day)
            unknowns.extend(day["unknowns"])
            conflicts.extend(day["conflicts"])
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
                if step.get("historical") and step["kind"] == "route":
                    step["execution_unknown"] = True
            rule = night_rules.get(day["date"])
            adjustments.add_usage(
                usage,
                day,
                request.participants,
                rule.nightly_cost_cny if rule else None,
            )
        if not validity:
            incomplete = True
            unknowns.append(
                _issue(
                    "no_future_schedule",
                    "没有仍有效的未来交通段，历史保留但不声明可执行",
                )
            )
        if (
            planning.max_walk_meters is not None
            and usage["walking_distance_meters"] > planning.max_walk_meters
        ):
            conflicts.append(
                _issue(
                    "walking_limit",
                    "全行程累计步行超过用户上限",
                    meters=usage["walking_distance_meters"],
                    maximum=planning.max_walk_meters,
                )
            )
        if request.budget_cny is not None:
            if usage["known_cost_cny"] > request.budget_cny:
                conflicts.append(
                    _issue(
                        "budget_conflict",
                        "全行程已知费用超过总预算",
                        known_cost_cny=usage["known_cost_cny"],
                        maximum=request.budget_cny,
                    )
                )
            if usage["unknown_cost_items"] or usage["estimated_cost_cny"]:
                unknowns.append(
                    _issue(
                        "budget_unverified",
                        "酒店或其他项目费用未知/估计，不能保证总预算",
                    )
                )
        results.append(
            {
                "status": "conflict"
                if conflicts
                else "partial"
                if incomplete
                else "conditional"
                if unknowns
                else "validated",
                "payload": {
                    "input": frozen,
                    "days": days,
                    "itinerary": [step for day in days for step in day["itinerary"]],
                    "unknowns": unknowns,
                    "conflicts": conflicts,
                    "usage": usage,
                    "diff": [
                        _issue(
                            "multiday_recomputed",
                            "保留未受影响的日期与完成历史，重新核对相关日及下游接驳",
                            reused_dates=list(adjustment["reused"]),
                        )
                    ]
                    if parent_plan
                    else [],
                    "valid_until": min(validity).isoformat() if validity else None,
                    "supplements": deepcopy(supplemental_candidates),
                },
            }
        )
    return results
