"""Preserve immutable daily prefixes and validate protected activity facts."""

from copy import deepcopy
from datetime import timedelta
from zoneinfo import ZoneInfo

from ..domain.models import ConfirmedOrigin, TripRequest
from ..storage.planning import planning_hash
from . import planning as kernel


def day_signature(frozen, date):
    request = TripRequest.model_validate(frozen["request"])
    planning = request.planning
    windows = request.planning_windows()
    window = next((item for item in windows if item.date.isoformat() == date), None)
    constraints = [
        item
        for item in planning.activity_constraints
        if item.date
        and item.date.isoformat() == date
        or (not planning.day_windows and len(windows) == 1)
    ]
    ids = {item.candidate_id for item in constraints}
    index = next((i for i, item in enumerate(windows) if item == window), -1)
    nights = [
        stay
        for stay in planning.overnight_stays
        if window
        and stay.date in [window.date, windows[index - 1].date if index > 0 else None]
    ]
    hotel_ids = {stay.candidate_id for stay in nights}
    zone = ZoneInfo(planning.timezone)
    rails = [
        rail
        for rail in planning.rail_segments
        if rail.date.isoformat()
        <= date
        <= (
            rail.arrival_at.astimezone(zone).date().isoformat()
            if rail.arrival_at
            else rail.date.isoformat()
        )
    ]
    station_ids = {
        identifier
        for rail in rails
        for identifier in (rail.departure_station_id, rail.arrival_station_id)
    }
    return {
        "window": window.model_dump(mode="json") if window else None,
        "origin": frozen["origin"] if index == 0 else None,
        "transport": (
            request.transport,
            request.transport_source,
            request.participants,
            request.preferences,
        ),
        "rules": (
            planning.timezone,
            planning.origin_city,
            planning.disabled_modes,
            planning.transfer_buffer_minutes,
            planning.max_walk_meters,
            planning.dietary_restrictions,
        ),
        "end": (planning.return_to_origin, planning.end_candidate_id)
        if index == len(windows) - 1
        else None,
        "constraints": [item.model_dump(mode="json") for item in constraints],
        "selected": [
            item for item in frozen["selected"] if item["candidate_id"] in ids
        ],
        "nights": [stay.model_dump(mode="json") for stay in nights],
        "hotels": [
            item
            for item in frozen.get("lodgings", [])
            if item["candidate_id"] in hotel_ids
        ],
        "rails": [rail.model_dump(mode="json") for rail in rails],
        "stations": [
            item
            for item in frozen.get("stations", [])
            if item["candidate_id"] in station_ids
        ],
    }


def prepare(frozen, parent, request):
    result = {"reused": {}, "parents": {}, "issues": [], "conflicts": []}
    if not parent:
        if request.planning.progress_at is not None:
            result["conflicts"].append(
                kernel._issue("progress_parent_missing", "执行中调整必须沿用可靠原方案")
            )
        return result
    if (
        parent["trip_id"] != frozen["trip_id"]
        or parent["payload"]["input"]["trip_id"] != frozen["trip_id"]
        or parent["input_hash"] != planning_hash(parent["payload"]["input"])
    ):
        raise ValueError("父方案归属或冻结签名无效")
    old = parent["payload"]["input"]
    planning = request.planning
    zone = ZoneInfo(planning.timezone)
    progress = planning.progress_at
    if (
        request.start_at
        and request.start_at < kernel._current_time()
        and progress is None
    ):
        result["issues"].append(
            kernel._issue(
                "current_progress_required", "原出发时间已过，请明确当前位置与当前时间"
            )
        )
    if progress and parent["id"] != frozen.get("active_plan_id"):
        result["conflicts"].append(
            kernel._issue(
                "historical_parent_not_active", "执行中调整必须基于当前采用方案"
            )
        )
    dates = {window.date.isoformat() for window in request.planning_windows()}
    if progress and progress.astimezone(zone).date().isoformat() not in dates:
        result["conflicts"].append(
            kernel._issue("progress_date_outside", "当前进度不在明确的行程日窗中")
        )
    constraints = {item.candidate_id: item for item in planning.activity_constraints}
    places = {item["candidate_id"]: item for item in frozen["selected"]}
    previous = {
        item["candidate_id"]: item
        for item in parent["payload"]["itinerary"]
        if item["kind"] == "activity"
    }
    for identifier in set(previous) | set(constraints):
        prior, item = previous.get(identifier), constraints.get(identifier)
        if not (
            (prior and (prior.get("locked") or prior.get("state") == "completed"))
            or (item and (item.locked or item.state == "completed"))
        ):
            continue
        place = places.get(identifier)
        if (
            not prior
            or not prior.get("start_at")
            or not prior.get("end_at")
            or not item
            or not place
        ):
            result["conflicts"].append(
                kernel._issue(
                    "protected_snapshot_missing",
                    "已完成或锁定活动必须保留原地点、日期与完整时刻",
                    candidate_id=identifier,
                )
            )
            continue
        date = prior.get("date")
        changed = (
            (item.date and item.date.isoformat() != date)
            or date not in dates
            or prior["poi"]["id"] != place["poi"]["id"]
            or prior["poi"]["location"] != place["poi"]["location"]
            or item.excluded
            or item.state == "skipped"
        )
        changed |= any(
            getattr(item, field) is not None
            and getattr(item, field) != prior.get(field)
            for field in ("duration_minutes", "cost_cny")
        )
        changed |= bool(
            item.fixed_start_at
            and item.fixed_start_at != kernel._time(prior["start_at"])
        )
        changed |= bool(request.planning.day_windows and item.date is None)
        changed |= bool(
            item.not_before and item.not_before > kernel._time(prior["start_at"])
        )
        changed |= bool(
            item.not_after and item.not_after < kernel._time(prior["end_at"])
        )
        if prior.get("state") == "completed" and item.state != "completed":
            changed = True
        if changed:
            result["conflicts"].append(
                kernel._issue(
                    "protected_activity_changed",
                    "已完成或锁定活动的日期、地点、时长、费用和原时刻不能改写",
                    candidate_id=identifier,
                )
            )
        if item.state == "completed" and (
            progress is None or kernel._time(prior["end_at"]) > progress
        ):
            result["conflicts"].append(
                kernel._issue(
                    "progress_time_missing",
                    "请提供完成活动之后的当前时间和位置",
                    candidate_id=identifier,
                )
            )
    changed = False
    for day in parent["payload"].get("days", []):
        date = day["date"]
        result["parents"][date] = day
        if date not in dates:
            continue
        past = bool(progress and date < progress.astimezone(zone).date().isoformat())
        if past:
            activities = [
                step for step in day["itinerary"] if step["kind"] == "activity"
            ]
            if any(
                not constraints.get(step["candidate_id"])
                or constraints[step["candidate_id"]].state
                not in {"completed", "skipped"}
                for step in activities
            ):
                result["conflicts"].append(
                    kernel._issue(
                        "historical_state_unconfirmed",
                        "过去日期仍有未明确完成或跳过的活动，不能推定其已发生",
                        date=date,
                    )
                )
            before, after = day_signature(old, date), day_signature(frozen, date)
            for signature in (before, after):
                signature.pop("constraints")
                signature.pop("transport")
                signature.pop("rules")
            if before != after:
                result["conflicts"].append(
                    kernel._issue(
                        "historical_day_changed",
                        "过去日期的窗口、地点、酒店或铁路快照不能改写",
                        date=date,
                    )
                )
            saved = deepcopy(day)
            for step in saved["itinerary"]:
                step["historical"] = True
                if step["kind"] == "activity":
                    item = constraints.get(step["candidate_id"])
                    if item:
                        step.update(
                            state=item.state,
                            locked=step.get("locked", False) or item.locked,
                            required=item.required,
                        )
                        if item.state == "skipped":
                            step.update(execution_unknown=True, untravelled=True)
                elif step["kind"] != "wait":
                    step["execution_unknown"] = True
            saved["unknowns"].append(
                kernel._issue(
                    "historical_execution_unknown",
                    "保留过去的计划快照；实际交通、住宿入住与支出未被自动确认为已发生",
                    date=date,
                )
            )
            result["reused"][date] = saved
            continue
        changed |= day_signature(old, date) != day_signature(frozen, date)
        changed |= bool(
            progress and date == progress.astimezone(zone).date().isoformat()
        )
        changed |= bool(
            day["conflicts"]
            or not (day.get("origin") or day.get("continuing_rail_id"))
            or not (day.get("endpoint") or day.get("overnight_rail_id"))
        )
        changed |= any(
            not step.get("start_at") or not step.get("end_at")
            for step in day["itinerary"]
        )
        if date != max(dates):
            changed |= not (
                day.get("lodging_candidate_id") or day.get("overnight_rail_id")
            )
        changed |= any(
            step["kind"] == "route"
            and (
                not step["leg"].get("valid_until")
                or kernel._time(step["leg"]["valid_until"]) <= kernel._current_time()
            )
            for step in day["itinerary"]
        )
        if not changed:
            result["reused"][date] = deepcopy(day)
    return result


def city_parent(parent, day, *, itinerary=None):
    if not parent or not day:
        return None
    sliced = deepcopy(parent)
    frozen = sliced["payload"]["input"]
    request = frozen["request"]
    request.update(start_at=day["start_at"], duration_minutes=None)
    request["planning"].update(
        day_windows=[{key: day[key] for key in ("date", "start_at", "end_at")}],
        overnight_stays=[],
        rail_segments=[],
        progress_at=None,
        progress_origin=None,
        end_at=day["end_at"],
        activity_constraints=[
            item
            for item in request["planning"]["activity_constraints"]
            if item.get("date") == day["date"] or not item.get("date")
        ],
    )
    frozen["planning"] = deepcopy(request["planning"])
    frozen["origin"] = deepcopy(day["origin"])
    sliced["payload"]["itinerary"] = deepcopy(
        day["itinerary"] if itinerary is None else itinerary
    )
    sliced["payload"]["unknowns"] = deepcopy(day["unknowns"])
    sliced["input_hash"] = planning_hash(frozen)
    return sliced


def add_usage(usage, day, participants, nightly_cost=None):
    for step in day["itinerary"]:
        if step["kind"] == "wait":
            continue
        if step.get("execution_unknown"):
            usage["walking_known"] = False
            usage["unknown_cost_items"] += 1
            continue
        cost = step.get("cost_cny")
        if step["kind"] == "route":
            leg = step["leg"]
            if leg.get("walking_distance_meters") is None:
                usage["walking_known"] = False
            else:
                usage["walking_distance_meters"] += leg["walking_distance_meters"]
            cost = leg.get("cost_cny")
            if cost is not None:
                usage[
                    "estimated_cost_cny"
                    if leg["mode"] == "transit"
                    else "known_cost_cny"
                ] += cost * (participants if leg["mode"] == "transit" else 1)
        elif cost is not None:
            usage[
                "known_cost_cny"
                if step.get("cost_source") == "user"
                else "estimated_cost_cny"
            ] += cost
        if cost is None:
            usage["unknown_cost_items"] += 1
    if day.get("lodging_candidate_id"):
        if nightly_cost is None:
            usage["unknown_cost_items"] += 1
        else:
            usage["known_cost_cny"] += nightly_cost


def rail_city_parent(parent, day, before_rail_id):
    if not parent or not day:
        return None
    steps = day["itinerary"]
    boundaries = [index for index, step in enumerate(steps) if step["kind"] == "rail"]
    finish = next(
        (index for index in boundaries if steps[index]["rail_id"] == before_rail_id),
        len(steps),
    )
    previous = next((index for index in reversed(boundaries) if index < finish), -1)
    block = steps[previous + 1 : finish]
    span = deepcopy(day)
    if previous >= 0:
        rail = steps[previous]
        poi = rail["arrival_station"]["poi"]
        span["origin"] = ConfirmedOrigin(
            name=poi["name"],
            location=poi["location"],
            poi_id=poi["id"],
            city=poi["city"],
            source="amap",
        ).model_dump(mode="json")
        span["start_at"] = (
            kernel._time(rail["end_at"])
            + timedelta(minutes=rail["arrival_buffer_minutes"])
        ).isoformat()
        while (
            block
            and block[0]["kind"] == "wait"
            and kernel._time(block[0]["end_at"]) <= kernel._time(span["start_at"])
        ):
            block = block[1:]
    if finish < len(steps):
        rail = steps[finish]
        span["end_at"] = (
            kernel._time(rail["start_at"])
            - timedelta(minutes=rail["boarding_buffer_minutes"])
        ).isoformat()
    return city_parent(parent, span, itinerary=block)
