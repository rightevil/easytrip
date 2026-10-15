"""Dated, area-bound hotel suggestions; choosing a night remains an explicit edit."""

from copy import deepcopy

from ..domain.models import TripRequest
from .meals import _gap, _nearby, _supplement_places


def lodging_queries(frozen, suggestions):
    request = TripRequest.model_validate(frozen["request"])
    nights = {day.date.isoformat() for day in request.planning.day_windows[:-1]}
    contexts, seen = [], set()
    for suggestion in suggestions:
        for day in suggestion["payload"].get("days", []):
            date = day["date"]
            if (
                date in seen
                or date not in nights
                or not any(gap["code"] == "lodging_gap" for gap in day["unknowns"])
            ):
                continue
            seen.add(date)
            anchor = next(
                (
                    item["poi"]
                    for item in reversed(day["itinerary"])
                    if item["kind"] == "activity"
                ),
                day.get("origin"),
            )
            if not anchor or not anchor.get("city") or not anchor.get("location"):
                continue
            contexts.append(
                {
                    "purpose": "lodging_gap",
                    "date": date,
                    "query": f"{anchor['city']} {anchor['name']} 附近 {date} 住宿 酒店"[
                        :120
                    ],
                    "anchor_candidate_id": next(
                        (
                            item["candidate_id"]
                            for item in reversed(day["itinerary"])
                            if item["kind"] == "activity"
                        ),
                        None,
                    ),
                    "anchor_name": anchor["name"],
                    "anchor_location": anchor["location"],
                    "city": anchor["city"],
                }
            )
    return contexts


async def supplement_lodgings(runner, run, client, frozen, suggestions, amap):
    contexts = lodging_queries(frozen, suggestions)
    result = {"queries": contexts[:2], "candidates": [], "gaps": []}
    known_dates = {context["date"] for context in contexts}
    missing_regions = {
        day["date"]
        for suggestion in suggestions
        for day in suggestion["payload"].get("days", [])
        if day["date"] not in known_dates
        and any(gap["code"] == "lodging_gap" for gap in day["unknowns"])
    }
    result["gaps"].extend(
        {
            "code": "lodging_region_unknown",
            "date": date,
            "message": "当晚地区未知，未进行泛城市搜索；请明确活动地点或当日起点",
        }
        for date in sorted(missing_regions)
    )
    if not contexts:
        return result
    current = await runner.db("planning_input", run["trip_id"])
    if current["input_hash"] != frozen["input_hash"]:
        return result
    pending = []
    for context in contexts[:2]:
        existing = next(
            (
                place
                for place in current.get("lodging_candidates", [])
                if place["poi"].get("city", "").removesuffix("市")
                == context["city"].removesuffix("市")
                and place["poi"].get("address")
                and _nearby(place["poi"]["location"], context["anchor_location"])
            ),
            None,
        )
        if existing:
            result["candidates"].append(
                {
                    **deepcopy(existing),
                    "lodging_window": {
                        key: context[key]
                        for key in (
                            "date",
                            "anchor_candidate_id",
                            "anchor_name",
                            "anchor_location",
                            "city",
                        )
                    },
                }
            )
        else:
            pending.append(context)
    looked_up = await _supplement_places(
        runner, run, client, frozen, amap, pending, "stay"
    )
    result["candidates"].extend(looked_up["candidates"])
    result["gaps"].extend(looked_up["gaps"])
    result["gaps"].extend(
        _gap(context, "lookup_limit", "本轮最多补查两晚，其他住宿缺口保留")
        for context in contexts[2:]
    )
    return result
