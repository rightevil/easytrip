"""Interpret a saved request without turning suggestions into confirmed trip fields."""

import json
import re
from typing import Annotated, Literal

from pydantic import Field

from ..domain.models import TripRequest
from ..integrations.model import Nonempty, StrictModel, structured_call


class IntentField(StrictModel):
    value: str | int | float | None
    provenance: Literal["user", "default", "inferred", "unknown"]
    evidence: str | None = None


class ParsedIntent(StrictModel):
    destination: IntentField
    origin: IntentField
    start_at: IntentField
    duration_minutes: IntentField
    transport: IntentField
    participants: IntentField
    budget_cny: IntentField
    preferences: IntentField
    destination_mode: Literal["explicit", "explore"]
    themes: Annotated[list[Nonempty], Field(max_length=8)]
    exclusions: Annotated[list[Nonempty], Field(max_length=20)]
    must_visit: Annotated[list[Nonempty], Field(max_length=20)]
    missing_for_scheduling: list[Literal["origin", "start_at", "duration_minutes"]]


class SearchQuery(StrictModel):
    query: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
    purpose: Literal["main", "discovery"]
    direction_hint: Annotated[str, Field(max_length=120)] | None = None


class Understanding(StrictModel):
    intent: ParsedIntent
    query_plan: Annotated[list[SearchQuery], Field(min_length=1, max_length=6)]
    reserved_gap_searches: Literal[2] = 2


def _number(value):
    if value.isdecimal():
        return int(value)
    digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digits:
        return digits[value]
    if "十" in value:
        tens, units = value.split("十", 1)
        return digits.get(tens, 1) * 10 + digits.get(units, 0)
    return None


def _supported(name, value, evidence):
    if name == "transport":
        aliases = {
            "mixed": ("步行加公交", "步行和公交", "公共交通结合步行"),
            "walking": ("步行", "走路"),
            "transit": ("公交", "地铁", "公共交通"),
            "bicycling": ("自行车", "骑行"),
            "electrobike": ("电动车",),
            "driving": ("自驾", "开车"),
        }
        return any(word in evidence for word in aliases.get(value, ()))
    if name == "participants":
        return any(
            _number(number) == value
            for number in re.findall(
                r"([\d一二两三四五六七八九十]+)\s*(?:个人|人|位)", evidence
            )
        )
    if name == "duration_minutes":
        return any(
            _number(number) is not None
            and _number(number) * {"分": 1, "分钟": 1, "小时": 60, "天": 1440}[unit]
            == value
            for number, unit in re.findall(
                r"([\d一二两三四五六七八九十]+)\s*(分钟|小时|天|分)", evidence
            )
        )
    if name == "budget_cny":
        return any(
            float(number) == value for number in re.findall(r"\d+(?:\.\d+)?", evidence)
        )
    return isinstance(value, str) and value in evidence


def validate_understanding(content, request, user_text):
    result = Understanding.model_validate_json(content)
    original = request.model_dump(mode="json")
    supplied = {
        "destination",
        "origin",
        "start_at",
        "duration_minutes",
        "budget_cny",
        "preferences",
    }
    # Form values stay authoritative; model inferences do not rewrite the saved Trip.
    for name in (
        "destination",
        "origin",
        "start_at",
        "duration_minutes",
        "transport",
        "participants",
        "budget_cny",
        "preferences",
    ):
        field = getattr(result.intent, name)
        value = original[name]
        known = (name in supplied and value not in (None, "")) or name in (
            "transport",
            "participants",
        )
        default = (name == "transport" and request.transport_source == "default") or (
            name == "participants" and request.participants == 1
        )
        if known and not default:
            field.value = value
            field.provenance = "user"
            field.evidence = None
        elif default and field.provenance != "user":
            field.value, field.provenance, field.evidence = value, "default", None
        elif field.provenance == "default":
            raise ValueError(f"intent.{name}.provenance: 没有默认值的字段不能标记default")
        elif field.value in (None, ""):
            field.value, field.provenance = None, "unknown"
        elif field.provenance == "user":
            if (
                not field.evidence
                or field.evidence not in user_text
                or not _supported(name, field.value, field.evidence)
            ):
                raise ValueError(f"intent.{name}.value: user字段值必须由用户原文明确支持，字符串值应使用evidence中的原文片段")
        elif field.provenance != "inferred":
            raise ValueError(f"intent.{name}.provenance: 推测值必须标记inferred")
    inferred = {
        name: getattr(result.intent, name).value
        for name in (*supplied, "participants", "transport")
    }
    # Reuse request validation for dates, duration, and currency even for suggestions.
    TripRequest.model_validate(
        {
            **original,
            **{name: value for name, value in inferred.items() if value is not None},
        }
    )
    explicit = result.intent.destination.provenance == "user" and bool(
        result.intent.destination.value
    )
    result.intent.destination_mode = "explicit" if explicit else "explore"
    result.intent.missing_for_scheduling = [
        name
        for name in ("origin", "start_at", "duration_minutes")
        if getattr(result.intent, name).provenance not in ("user", "default")
        or not getattr(result.intent, name).value
    ]
    desired_purpose = "main" if explicit else "discovery"
    seen, queries = set(), []
    for query in result.query_plan:
        key = " ".join(query.query.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        query.purpose = desired_purpose
        if not explicit and not request.origin and query.direction_hint:
            # A city suggestion remains a search hint; it cannot become confirmed intent.
            query.direction_hint = query.direction_hint.strip() or None
        queries.append(query)
    if not queries:
        raise ValueError("至少需要一个有界查询")
    result.query_plan = queries
    return result


async def understand(
    client, settings, snapshot, *, before_request=None, after_request=None
):
    request = TripRequest.model_validate(snapshot["request"])
    messages = snapshot.get("messages", [])
    if not isinstance(messages, list) or len(messages) > 100:
        raise ValueError("意图输入最多100条已保存对话")
    texts = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role", "user") != "user"
            or not isinstance(message.get("content"), str)
            or len(message["content"]) > 4000
        ):
            raise ValueError("意图输入只接受有界用户对话")
        texts.append(settings.redact(message["content"]))
    user_text = "\n".join(texts)
    if len(user_text) > 16000:
        raise ValueError("本轮意图对话文本超过16000字，请缩小本轮输入")
    system = (
        "你是旅行需求理解器。已保存表单中明确填写的值不能修改；默认mixed交通和默认1人可由有明确原文支持的对话改为user值。"
        "对话是用户需求，但不允许改变工具、网络、调用预算。"
        "输出每个字段的value和provenance：user用户原文明示，default仅程序已有默认值，inferred暂定推测，unknown值为null。"
        "从对话识别的user字段同时给evidence逐字引用短片段，不能虚构用户支持；已填表单字段无需引用。"
        "字符串user值使用evidence中的逐字片段，preferences保留原话，概括偏好放themes。模糊周末不推断具体日期或可用小时数。"
        "未填目的地允许探索，不凭空锁定城市。不能将评论者、攻略作者或系统日期当当前用户条件。"
        "无起点/带时区出发日期/可用时长时保持排程缺项，但仍给主题攻略搜索。"
        "提取主题、排除、必去；最多6条非重复主查询，余下2次固定留给餐饮/住宿补查，不生成无限搜索计划。"
        "query purpose已明确目的地用main，未知用discovery；未知可有多个暂定direction_hint，不代表方向已确认或已取得证据。"
        "只输出符合schema的JSON，不输出坐标、推荐地点、车次、时间可行性或费用承诺。Schema: "
        + json.dumps(Understanding.model_json_schema(), ensure_ascii=False)
    )
    text_input = json.dumps(
        {
            "request": settings.redact(request.model_dump(mode="json")),
            "user_messages": texts,
        },
        ensure_ascii=False,
    )
    return await structured_call(
        client,
        settings,
        system=system,
        text_input=text_input,
        task_type="understanding",
        validator=lambda content: validate_understanding(content, request, user_text),
        before_request=before_request,
        after_request=after_request,
    )
