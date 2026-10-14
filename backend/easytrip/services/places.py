"""Conservative AMap POI matching and source-explainable candidate ordering."""

import math
import re
import unicodedata
from datetime import datetime


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _name(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _text(value) or "")).lower()


def _city(value):
    return _name(value).removesuffix("市")


def _coordinates(value):
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        if any(isinstance(item, bool) for item in value):
            return None
        longitude, latitude = map(float, value)
    except (TypeError, ValueError):
        return None
    if (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    ):
        return [longitude, latitude]
    return None


def normalize_poi(raw, read_at, api_version="v5"):
    """Project provider fields only; an absent entrance never uses the centre point."""
    navi = raw.get("navi") if isinstance(raw.get("navi"), dict) else {}
    normalized = "api_version" in raw and "city" in raw
    poi = {
        "id": _text(raw.get("id")),
        "name": _text(raw.get("name")),
        "location": _coordinates(raw.get("location")),
        "address": _text(raw.get("address")),
        "city": _text(raw.get("city") if normalized else raw.get("cityname")),
        "adcode": _text(raw.get("adcode")),
        "type": _text(raw.get("type")),
        "entrance_location": _coordinates(
            raw.get("entrance_location") if normalized else navi.get("entr_location")
        ),
    }
    poi["missing_fields"] = [key for key, value in poi.items() if value is None]
    poi["read_at"] = raw.get("read_at", read_at) if normalized else read_at
    if isinstance(poi["read_at"], datetime):
        poi["read_at"] = poi["read_at"].isoformat()
    poi["api_version"] = (
        raw.get("api_version", api_version) if normalized else api_version
    )
    return poi


def _category(poi):
    types = set((_text(poi.get("type")) or "").split(";"))
    if "餐饮服务" in types:
        return "eat"
    if "住宿服务" in types:
        return "stay"
    if types & {"风景名胜", "体育休闲服务", "博物馆", "美术馆", "展览馆", "科技馆"}:
        return "play"
    return None


def _base_name(value):
    return re.sub(r"\([^()]*\)$", "", _name(value))


def match_pois(candidate, mentions, response, read_at):
    """Only a complete, same-city/category, exact original name can auto-match."""
    result = {
        "status": "unmatched",
        "poi": None,
        "options": [],
        "reason": "未取得可用高德地点",
    }
    if response.get("status") != "1" or not isinstance(response.get("pois"), list):
        return result
    incomplete_fields = False
    for row in response["pois"]:
        if not isinstance(row, dict):
            incomplete_fields = True
            continue
        poi = normalize_poi(row, read_at, response.get("api_version", "v5"))
        if any(poi[key] is None for key in ("id", "name", "location", "city", "type")):
            incomplete_fields = True
        if poi["id"] and poi["location"] is not None:
            if all(option["id"] != poi["id"] for option in result["options"]):
                result["options"].append(poi)
    if not result["options"]:
        return result
    result.update(status="ambiguous", reason="地点信息不足，需确认具体地点或分店")
    if candidate.get("kind", "place") != "place":
        result["reason"] = "区域、路线或泛称需先确认具体地点"
        return result
    city = _city(candidate.get("city"))
    category = candidate.get("category")
    if not city or category not in ("eat", "play", "stay"):
        result["reason"] = "城市或类别未明确，不能自动确认"
        return result
    member_ids = set(candidate.get("mention_ids", []))
    members = [row for row in mentions if not member_ids or row.get("id") in member_ids]
    if any(_city(row.get("city")) not in ("", city) for row in members):
        result["reason"] = "来源城市存在冲突，需确认"
        return result
    if any(row.get("kind", "place") != "place" for row in members):
        result["reason"] = "来源包含区域、路线或泛称，需确认具体地点"
        return result
    names = {
        _name(candidate.get("name")),
        *(_name(row.get("name")) for row in members),
    } - {""}
    branches = {_name(row.get("branch_hint")) for row in members} - {""}
    if _text(candidate.get("branch_hint")):
        branches.add(_name(candidate["branch_hint"]))
    if any(
        len(branch) < 2 or branch in {"分店", "附近", "旁边", "附近店"}
        for branch in branches
    ):
        result["reason"] = "分店线索不明确，需补充店名或地址"
        return result
    qualified_names = {name for name in names if _base_name(name) != name}
    eligible = [
        poi
        for poi in result["options"]
        if _city(poi["city"]) == city and _category(poi) == category
    ]
    matches = []
    for poi in eligible:
        name, address = _name(poi["name"]), _name(poi["address"])
        branch_matches = all(branch in name or branch in address for branch in branches)
        exact = name in names
        explicit_branch = (
            bool(branches)
            and branch_matches
            and (not qualified_names or name in qualified_names)
            and any(_base_name(name) == _base_name(original) for original in names)
        )
        if branch_matches and (exact or explicit_branch):
            matches.append(poi)
    if not matches:
        result["reason"] = "无同城同类别的明确原名或分店匹配，需人工核对"
        return result
    if response.get("search_complete") is not True:
        result["reason"] = "检索结果不完整，不能排除同名分店"
        return result
    if incomplete_fields:
        result["reason"] = "检索中有地点坐标、城市或类别缺失，需人工核对"
        return result
    if len(matches) != 1:
        result["reason"] = "存在多个相符地点或分店，需确认"
        return result
    matched = matches[0]
    if not branches and not any("(" in original for original in names):
        families = [
            poi
            for poi in eligible
            if any(_name(poi["name"]).startswith(original) for original in names)
        ]
        if len(families) > 1:
            result["reason"] = "原名存在多个分店，需补充分店或地址"
            return result
    if not matched["address"] or not matched["name"]:
        result["reason"] = "地点名称或地址缺失，需确认"
        return result
    result.update(
        status="unique", poi=matched, reason="完整检索中同城同类别的原名及分店明确匹配"
    )
    return result


def _records(rows):
    return rows if isinstance(rows, dict) else {row["id"]: row for row in rows or []}


def _preference_hits(candidate, terms, related, activities, source_map):
    passages = []
    for item in [*related, *activities]:
        if item.get("mention_id") not in candidate.get("mention_ids", []):
            continue
        if item.get("citation_kind", "text") != "text":
            continue
        if "stance" in item and (
            item.get("link_status") != "linked" or item.get("nature") != "experience"
        ):
            continue
        quote = _text(item.get("quote"))
        source = source_map.get(item.get("source_id"), {})
        original = "\n".join(source.get(field, "") for field in ("title", "text"))
        if quote and quote in original:
            label = ("证据" if "stance" in item else "活动") + item["id"]
            passages.append((quote, label, item.get("stance") in ("negative", "mixed")))
    name = _name(candidate.get("name"))
    for identifier in candidate.get("source_ids", []):
        source = source_map.get(identifier, {})
        for field in ("title", "text"):
            for clause in re.split(r"[。\n；;!?！？]", source.get(field, "")):
                if name and name in _name(clause):
                    passages.append((clause, "来源" + identifier, False))
    hits, conflicts = {term: set() for term in terms}, {term: set() for term in terms}
    for quote, label, negative in passages:
        text = _name(quote)
        for term in terms:
            if term in text:
                negated = re.search(
                    r"(?:不|没|无|并非|不是|难以|not)[^，,、;；。.!?！？:：/|]{0,3}"
                    + re.escape(term),
                    text,
                )
                (conflicts if negative or negated else hits)[term].add(label)
    return hits, conflicts


def rank_candidates(candidates, request, sources=None, evidence=None, activities=None):
    """Keep text candidates visible; filter only verified city/category conflicts."""
    source_map, evidence_map = _records(sources), _records(evidence)
    activity_rows = list(_records(activities).values())
    terms = list(
        dict.fromkeys(
            _name(term)
            for term in re.split(
                r"[\s，,、;；。.!?！？:：/|]+", request.get("preferences") or ""
            )
            if len(_name(term)) >= 2
        )
    )
    verified_cities = {
        _city(row.get("poi", {}).get("city"))
        for row in candidates
        if row.get("poi") and row.get("match_status") == "unique"
    } - {""}
    destination = _city(request.get("destination"))
    requested_city = _city(request.get("city")) or (
        destination
        if destination in verified_cities
        or _name(request.get("destination")).endswith("市")
        else ""
    )
    ranked = []
    for row in candidates:
        candidate = {**row, "evidence_ids": list(row.get("evidence_ids", []))}
        poi = row.get("poi") or {}
        verified = (
            row.get("match_status") == "unique"
            and bool(poi.get("id"))
            and _coordinates(poi.get("location")) is not None
            and bool(_city(poi.get("city")))
            and _category(poi) == row.get("category")
        )
        if verified and requested_city and _city(poi["city"]) != requested_city:
            continue
        category = request.get("category")
        if (
            verified
            and category in ("eat", "play", "stay")
            and _category(poi) != category
        ):
            continue
        stats = row.get("stats") or {}
        authors = stats.get("author_count", 0)
        positive, negative = (
            stats.get("positive_count", 0),
            stats.get("negative_count", 0),
        )
        related = [
            evidence_map[key]
            for key in candidate["evidence_ids"]
            if key in evidence_map
        ]
        if evidence is not None:
            related = [
                item
                for item in related
                if item.get("mention_id") in row.get("mention_ids", [])
            ]
            eligible = [
                item
                for item in related
                if item.get("link_status") == "linked"
                and item.get("nature") == "experience"
                and item.get("citation_kind", "text") == "text"
                and source_map.get(item.get("source_id"), {}).get("kind") == "comment"
            ]
            positive = len(
                {
                    item["source_id"]
                    for item in eligible
                    if item.get("stance") in ("positive", "mixed")
                }
            )
            negative = len(
                {
                    item["source_id"]
                    for item in eligible
                    if item.get("stance") in ("negative", "mixed")
                }
            )
        source_ids = set(row.get("source_ids", [])) | {
            item.get("source_id") for item in related
        }
        if sources is not None:
            authors = len(
                {
                    source_map.get(identifier, {}).get("author_key")
                    for identifier in source_ids
                }
                - {None, ""}
            )
        hits, conflicts = _preference_hits(
            candidate, terms, related, activity_rows, source_map
        )
        preference_score = sum(
            bool(hits[term]) - bool(conflicts[term]) for term in terms
        )
        reasons = [
            "地点城市和类别已核实" if verified else "地点、城市或类别待核实",
            f"当前来源覆盖{authors}位不同作者",
            f"当前来源亲历评论支持{positive}条、反对{negative}条；不代表平台好评率",
        ]
        if negative:
            reasons.append("有负面或混合体验，请查看原证据")
        for term in terms:
            if hits[term]:
                reasons.append(
                    f"偏好短语原文命中“{term}”（{'、'.join(sorted(hits[term])[:3])}），仅字面关联，适用性待核实"
                )
            if conflicts[term]:
                reasons.append(
                    f"否定、负面或混合原文包含偏好短语“{term}”（{'、'.join(sorted(conflicts[term])[:3])}），请核对冲突"
                )
        unknown = [term for term in terms if not hits[term] and not conflicts[term]]
        if unknown:
            reasons.append("偏好短语未命中原文或未明确关联：" + "、".join(unknown))
        elif request.get("preferences") and not terms:
            reasons.append("偏好短语不足以进行字面匹配，适用性未知")
        if request.get("budget_cny") is not None:
            reasons.append("当前价格未核实，预算可行性待确认")
        latest = 0
        latest_published = None
        for identifier in source_ids:
            published = source_map.get(identifier, {}).get("published_at")
            try:
                date = (
                    datetime.fromisoformat(published)
                    if isinstance(published, str)
                    else published
                )
                if isinstance(date, datetime) and date.utcoffset() is not None:
                    if latest_published is None or date.timestamp() > latest:
                        latest, latest_published = date.timestamp(), date.isoformat()
            except ValueError:
                pass
        reasons.append(
            f"最近来源发布时间{latest_published}，不代表当前有效"
            if latest_published
            else "来源发布时间未知"
        )
        candidate["ranking_reasons"] = reasons
        ranked.append(
            (
                (
                    not verified,
                    -preference_score,
                    -authors,
                    -(positive - negative),
                    -latest,
                ),
                candidate,
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [candidate for _, candidate in ranked]
