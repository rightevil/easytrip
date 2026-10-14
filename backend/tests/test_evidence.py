import asyncio
import copy
import json

import httpx
import pytest

from easytrip.config import Settings
from easytrip.services.evidence import (
    NoteExtraction,
    build_candidates,
    build_directions,
    build_gaps,
    extract_note,
)


def settings():
    return Settings(
        model_base_url="https://model.example/v1",
        model_api_key="private-key",
        model_id="agnes-test",
    )


def note():
    return {
        "source_id": "note-a",
        "title": "杭州西湖",
        "text": "西湖适合散步，河坊街可以吃饭。",
        "comments": [
            {"source_id": "comment-a", "text": "我去了西湖，排队一小时但夜景值得"}
        ],
        "images": [],
    }


def extraction():
    return {
        "mentions": [
            {
                "id": "westlake",
                "source_id": "note-a",
                "name": "西湖",
                "city": "杭州",
                "category": "play",
                "quote": "西湖适合散步",
            }
        ],
        "activities": [
            {
                "mention_id": "westlake",
                "source_id": "note-a",
                "quote": "西湖适合散步",
                "description": "适合散步",
            }
        ],
        "evidence": [
            {
                "source_id": "comment-a",
                "mention_id": "westlake",
                "object_text": "西湖",
                "dimension": "排队/夜景",
                "stance": "mixed",
                "nature": "experience",
                "quote": "我去了西湖，排队一小时但夜景值得",
                "link_status": "linked",
            }
        ],
        "image_observations": [],
    }


def run(outputs, *, sample=None, before=None, after=None, **limits):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                outputs[len(calls) - 1], ensure_ascii=False
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 50,
                    "completion_tokens": 50,
                    "total_tokens": 100,
                },
            },
        )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ) as client:
            return await extract_note(
                client,
                settings(),
                sample or note(),
                before_request=before,
                after_request=after,
                **limits,
            )

    return asyncio.run(check()), calls


def sources():
    return {
        "note-a": {"kind": "note", "author_key": "a" * 64},
        "comment-a": {"kind": "comment", "author_key": "b" * 64},
    }


def line_ref(part="text", start=1, end=1):
    return {"part": part, "start_line": start, "end_line": end}


def test_line_refs_restore_exact_unicode_newlines_and_ledger_strings():
    sample, data = note(), extraction()
    sample["title"] = "杭州\r\n青山⛰️步道"
    sample["text"] = "青山⛰️步道\r\n\r\n沿路有树林\n适合散步"
    data["mentions"][0].update(name="青山", quote=line_ref("title", 2, 2))
    data["activities"][0]["quote"] = line_ref("text", 1, 4)
    data["evidence"][0].update(mention_id=None, link_status="unlinked")
    ledger = []

    async def after(identity, record):
        ledger.append(record)

    result, calls = run([data], sample=sample, after=after)
    assert result["status"] == "passed" and len(calls) == 1
    assert result["data"]["mentions"][0]["quote"] == "青山⛰️步道"
    assert result["data"]["activities"][0]["quote"] == sample["text"]
    assert ledger[0]["model_data"] == result["data"]
    assert all(
        isinstance(row["quote"], str)
        for field in ("mentions", "activities", "evidence")
        for row in result["data"][field]
    )
    payload = json.loads(calls[0].content)
    context = json.loads(payload["messages"][1]["content"][0]["text"])
    assert context["title"] == [
        {"line": 1, "text": "杭州\r\n"},
        {"line": 2, "text": "青山⛰️步道"},
    ]
    assert context["text"][1] == {"line": 2, "text": "\r\n"}
    assert "".join(row["text"] for row in context["text"]) == sample["text"]
    assert context["comments"][0]["source_id"] == "comment-a"
    assert context["comments"][0]["text"] == [
        {"line": 1, "text": sample["comments"][0]["text"]}
    ]
    schema = json.loads(
        payload["messages"][0]["content"].split("只输出schema JSON: ", 1)[1]
    )
    for name in ("PlaceMention", "Activity"):
        condition = schema["$defs"][name]["allOf"][0]
        assert condition["if"]["required"] == ["citation_kind"]
        assert condition["else"]["properties"]["quote"]["type"] == "object"
        assert condition["then"]["properties"]["quote"]["type"] == "string"
        assert (
            NoteExtraction.model_json_schema()["$defs"][name]["properties"]["quote"][
                "type"
            ]
            == "string"
        )
    assert schema["$defs"]["Evidence"]["properties"]["quote"]["type"] == "string"


@pytest.mark.parametrize(
    "reference",
    [
        {},
        {"part": "text", "start_line": 1},
        {**line_ref(), "source_id": "note-a"},
        line_ref("title+text"),
        line_ref(None),
        line_ref(start=True),
        line_ref(end=False),
        line_ref(start=1.0),
        line_ref(end="1"),
        line_ref(start=0),
        line_ref(start=-1),
        line_ref(start=2, end=1),
        line_ref(end=2),
    ],
)
def test_invalid_line_refs_are_rejected_with_only_one_repair(reference):
    data = extraction()
    data["mentions"][0]["quote"] = reference
    result, calls = run([data, data])
    assert result["status"] == "failed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "mentions[0].quote" in repair


@pytest.mark.parametrize("part,source_text", [("title", ""), ("text", "\n")])
def test_empty_or_whitespace_line_refs_cannot_become_quotes(part, source_text):
    sample, data = note(), extraction()
    sample[part] = source_text
    data["mentions"][0]["quote"] = line_ref(part)
    result, calls = run([data, data], sample=sample)
    assert result["status"] == "failed" and len(calls) == 2
    assert (
        "mentions[0].quote" in json.loads(calls[1].content)["messages"][-1]["content"]
    )


def test_line_refs_do_not_expand_ranges_or_borrow_another_source():
    sample, data = note(), extraction()
    sample["text"] = "河坊街可以吃饭。\n西湖适合散步。"
    sample["comments"][0]["text"] = "这里怎么走？"
    data["mentions"][0]["quote"] = line_ref("text", 1, 1)
    data["activities"][0].update(source_id="comment-a", quote=line_ref())
    data["evidence"][0].update(
        mention_id=None,
        object_text="",
        quote="这里怎么走？",
        link_status="uncertain",
    )
    result, calls = run([data, data], sample=sample)
    assert result["status"] == "failed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "mentions[0].name" in repair and "activities[0].quote" in repair


def test_line_ref_errors_share_the_single_repair_with_other_strict_failures():
    bad, good = extraction(), extraction()
    bad["mentions"][0].update(source_id="foreign-source", quote=line_ref())
    bad["activities"][0]["quote"] = line_ref(end=2)
    bad["evidence"][0]["quote"] = "没有这段原文"
    good["mentions"][0]["quote"] = line_ref()
    good["activities"][0]["quote"] = line_ref()
    result, calls = run([bad, good])
    assert result["status"] == "passed" and len(calls) == 2
    first, second = [json.loads(call.content)["messages"] for call in calls]
    assert first[1]["content"][0]["text"] == second[1]["content"]
    assert all(
        path in second[-1]["content"]
        for path in ("mentions[0].quote", "activities[0].quote", "evidence[0].quote")
    )
    assert result["coverage"]["comments_analysed"] == 1


def test_image_line_refs_fail_but_original_observation_strings_can_repair():
    sample, good = note(), extraction()
    sample["images"] = ["https://sns-img.xhscdn.com/photo"]
    for field in ("mentions", "activities"):
        good[field][0].update(
            citation_kind="image", image_index=1, quote="西湖适合散步"
        )
    good["image_observations"] = [
        {"source_id": "note-a", "image_index": 1, "text": "西湖适合散步"}
    ]
    bad = copy.deepcopy(good)
    for field in ("mentions", "activities"):
        bad[field][0]["quote"] = line_ref()
    result, calls = run([bad, good], sample=sample)
    assert result["status"] == "passed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"]
    assert "mentions[0].quote" in repair[-1]["content"]
    assert "activities[0].quote" in repair[-1]["content"]
    assert "image_url" not in str(repair)
    assert result["images_submitted"] == 1
    assert result["data"]["mentions"][0]["quote"] == "西湖适合散步"


def test_evidence_keeps_short_literal_quotes_with_mixed_question_context():
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = "我去了西湖，排队一小时；河坊街需要预约吗？"
    data["mentions"][0]["quote"] = line_ref()
    data["activities"][0]["quote"] = line_ref()
    data["evidence"][0].update(quote="我去了西湖，排队一小时", stance="negative")
    data["evidence"].append(
        {
            **data["evidence"][0],
            "mention_id": None,
            "object_text": "河坊街",
            "dimension": "预约",
            "quote": "河坊街需要预约吗？",
            "nature": "question",
            "stance": "question",
            "link_status": "unlinked",
        }
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    assert [row["nature"] for row in result["data"]["evidence"]] == [
        "experience",
        "question",
    ]
    bad = copy.deepcopy(data)
    bad["evidence"][0]["quote"] = line_ref()
    result, calls = run([bad, bad], sample=sample)
    assert result["status"] == "failed" and len(calls) == 2


def test_mixed_experience_counts_unique_comment_once_per_side_and_no_coordinates():
    output = extraction()
    output["evidence"] *= 2
    result, calls = run([output])
    assert result["status"] == "passed"
    assert len(result["data"]["evidence"]) == 1
    candidate = build_candidates([result], sources())[0]
    assert candidate["stats"] == {
        "author_count": 2,
        "positive_count": 1,
        "negative_count": 1,
        "uncertain_count": 0,
    }
    assert candidate["match_status"] == "unmatched" and "latitude" not in candidate
    assert result["coverage"]["comments_analysed"] == 1
    assert result["data"]["evidence"][0]["model_id"] == "agnes-test"
    assert json.loads(calls[0].content)["max_tokens"] == 32768


@pytest.mark.parametrize(
    "change", ["source", "quote", "name", "link", "omitted_comment", "coordinate"]
)
def test_untrusted_schema_and_references_repair_once_then_stop(change):
    bad = extraction()
    if change == "source":
        bad["mentions"][0]["source_id"] = "other-trip-note"
    elif change == "quote":
        bad["evidence"][0]["quote"] = "西湖无需排队"
    elif change == "name":
        bad["mentions"][0]["quote"] = "适合散步"
    elif change == "link":
        bad["evidence"][0]["mention_id"] = "foreign-mention"
    elif change == "omitted_comment":
        bad["evidence"] = []
    else:
        bad["mentions"][0]["latitude"] = 30
    result, calls = run([bad, bad])
    assert result["status"] == "failed" and len(calls) == 2
    assert result["data"] is None and result["coverage"]["comments_analysed"] == 0
    assert result["coverage"]["comment_ids_unanalysed"] == ["comment-a"]
    assert result["usage"]["total_tokens"] == 200


def test_unsupported_optional_branch_is_unknown_without_repair():
    output = extraction()
    output["mentions"][0]["branch_hint"] = "不存在的分店"
    result, calls = run([output])
    assert result["status"] == "passed" and len(calls) == 1
    assert result["data"]["mentions"][0]["branch_hint"] is None
    assert (
        "地点和分店尚待核对" in build_candidates([result], sources())[0]["uncertainty"]
    )


def test_name_repair_identifies_all_invalid_mentions():
    bad = extraction()
    bad["mentions"][0]["name"] = "无依据地点"
    bad["mentions"].append({**bad["mentions"][0], "id": "other", "name": "另一地点"})
    result, calls = run([bad, extraction()])
    assert result["status"] == "passed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "mentions[0].name" in repair and "mentions[1].name" in repair


def test_single_repair_sees_name_activity_and_evidence_failures_together():
    bad = extraction()
    bad["mentions"][0]["name"] = "无依据地点"
    bad["mentions"].append({**bad["mentions"][0], "id": "other", "name": "另一地点"})
    bad["activities"][0]["quote"] = "河坊街可以吃饭"
    bad["evidence"][0]["quote"] = "伪造评论"
    result, calls = run([bad, extraction()])
    assert result["status"] == "passed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert all(
        path in repair
        for path in (
            "mentions[0].name",
            "mentions[1].name",
            "activities[0].quote",
            "evidence[0].quote",
        )
    )


def test_single_repair_combines_schema_errors_and_safe_raw_reference_checks():
    bad = extraction()
    bad["mentions"][0]["name"] = "无依据地点"
    bad["activities"][0].pop("description")
    bad["activities"][0]["quote"] = "河坊街可以吃饭"
    bad["activities"].append(
        {
            **bad["activities"][0],
            "description": 123,
            "source_id": "foreign-source",
            "mention_id": "foreign-mention",
        }
    )
    bad["evidence"][0]["source_id"] = "foreign-comment"
    result, calls = run([bad, extraction()])
    assert result["status"] == "passed" and len(calls) == 2
    assert result["usage"]["total_tokens"] == 200
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert all(
        path in repair
        for path in (
            "activities.0.description: missing",
            "activities.1.description: string_type",
            "mentions[0].name",
            "activities[0].quote",
            "activities[1].source_id",
            "activities[1].mention_id",
            "evidence[0].source_id",
            "每条提交评论须显式分类",
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mentions", None),
        ("activities", {}),
        ("evidence", 123),
        ("image_observations", None),
        ("mentions", ["private-schema-input"]),
        ("evidence", [{"source_id": {}, "quote": [], "mention_id": []}]),
        ("image_observations", [{"source_id": [], "image_index": {}, "text": None}]),
    ],
)
def test_malformed_raw_fields_still_require_strict_schema_without_crashing(
    field, value
):
    bad = extraction()
    bad[field] = value
    result, calls = run([bad, bad])
    assert result["status"] == "failed" and result["data"] is None
    assert len(calls) == 2 and result["usage"]["total_tokens"] == 200
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert field in repair
    assert "choices" not in repair and "private-schema-input" not in repair


def test_invalid_image_observation_and_missing_activity_field_are_both_rejected():
    bad = extraction()
    bad["activities"][0].pop("description")
    bad["image_observations"] = [
        {"source_id": "note-a", "image_index": 1, "text": "西湖适合散步"}
    ]
    result, calls = run([bad, bad])
    assert result["status"] == "failed" and result["data"] is None
    assert len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "image_observations[0]" in repair
    assert "activities.0.description: missing" in repair


def test_original_emoji_name_and_multiline_quote_cannot_be_normalized_or_joined():
    sample, valid = note(), extraction()
    sample["text"] = "青山⛰️步道\n沿路有树林\n适合散步"
    valid["mentions"][0].update(name="青山", quote="青山⛰️步道")
    valid["activities"][0]["quote"] = sample["text"]
    valid["evidence"][0].update(mention_id=None, link_status="unlinked")
    bad = copy.deepcopy(valid)
    bad["mentions"][0]["name"] = "青山步道"
    bad["activities"][0]["quote"] = "青山⛰️步道\n适合散步"
    result, calls = run([bad, valid], sample=sample)
    assert result["status"] == "passed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "mentions[0].name" in repair and '"青山步道"' in repair
    assert "activities[0].quote" in repair
    assert result["data"]["activities"][0]["quote"] == sample["text"]


def test_unknown_source_and_activity_link_are_excluded_without_losing_comments():
    bad = extraction()
    bad["activities"][0].update(
        source_id="foreign-source", mention_id="foreign-mention"
    )
    result, calls = run([bad])
    assert result["status"] == "passed" and len(calls) == 1
    assert result["data"]["activities"] == []
    assert result["coverage"]["comments_analysed"] == 1
    report = result["data"]["activity_validation"]
    assert (report["received"], report["accepted"], report["rejected"]) == (1, 0, 1)
    assert "activities[0].source_id" in report["errors"][0]
    assert "activities[0].mention_id" in report["errors"][0]
    assert "choices" not in report["errors"][0]


def test_valid_activity_and_comments_survive_a_malformed_activity_and_enter_ledger():
    data, ledger = extraction(), []
    data["activities"].append(
        {"source_id": [], "mention_id": {}, "quote": None, "description": False}
    )

    async def after(_action_id, metadata):
        ledger.append(metadata)

    result, calls = run([data], after=after)
    assert len(calls) == 1 and result["status"] == "passed"
    assert len(result["data"]["activities"]) == 1
    assert result["coverage"]["comments_analysed"] == 1
    report = result["data"]["activity_validation"]
    assert (report["received"], report["accepted"], report["rejected"]) == (2, 1, 1)
    assert "activities.1." in report["errors"][0]
    assert ledger[0]["model_data"]["activity_validation"] == report
    assert ledger[0]["status"] == "passed"


@pytest.mark.parametrize("change", ["missing", "too_many", "forged_diagnostic"])
def test_activity_envelope_and_local_diagnostics_cannot_be_forged(change):
    data = extraction()
    if change == "missing":
        data.pop("activities")
    elif change == "too_many":
        data["activities"] *= 121
    else:
        data["activity_validation"] = {"received": 0, "accepted": 0, "rejected": 0}
    result, calls = run([data, data])
    assert result["status"] == "failed" and result["data"] is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mention_id,omitted_status",
    [("westlake", False), (None, False), ("westlake", True)],
)
def test_inconsistent_unlinked_evidence_keeps_no_place_vote(mention_id, omitted_status):
    output = extraction()
    output["evidence"][0].update(
        mention_id=mention_id, link_status="unlinked" if mention_id else "linked"
    )
    if omitted_status:
        output["evidence"][0].pop("link_status")
    result, calls = run([output])
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    if omitted_status:
        assert item["mention_id"] is not None and item["link_status"] == "uncertain"
    else:
        assert item["mention_id"] is None and item["link_status"] == "unlinked"
    candidate = build_candidates([result], sources())[0]
    assert (
        candidate["stats"]["positive_count"]
        == candidate["stats"]["negative_count"]
        == 0
    )


def test_candidate_reports_source_time_without_claiming_current_validity():
    result, _ = run([extraction()])
    originals = sources()
    originals["note-a"]["published_at"] = "2025-10-01T09:00:00+08:00"
    candidate = build_candidates([result], originals)[0]
    assert "可见来源最近发布时间2025-10-01" in candidate["summary"]
    assert "发布时间不代表当前有效" in candidate["summary"]
    assert "部分或全部来源发布时间未知" in candidate["uncertainty"]


@pytest.mark.parametrize(
    "text,nature",
    [
        ("好想请假去西湖", "wish"),
        ("这地方存了，西湖真好", "wish"),
        ("听说西湖好像关闭了", "hearsay"),
        ("西湖照片拍得真好", "content_reaction"),
        ("请问西湖需要预约吗？", "question"),
    ],
)
def test_wish_content_hearsay_and_question_are_not_experience_support(text, nature):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(quote=text, nature="experience", stance="positive")
    result, _ = run([data], sample=sample)
    item = result["data"]["evidence"][0]
    assert item["nature"] == nature
    candidate = build_candidates([result], sources())[0]
    assert candidate["stats"]["positive_count"] == 0
    assert candidate["stats"]["negative_count"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "我去过西湖，怎么看哪里都很漂亮",
        "我昨天去了西湖，机位和拍摄角度很适合摄影",
        "我去了西湖，回来收藏了我拍的照片",
        "我去了西湖，感觉好像更清静了",
        "西湖哪里都很漂亮",
        "西湖适合摄影，机位很多",
    ],
)
def test_statement_and_direct_visit_are_not_retyped_by_isolated_keywords(text):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(quote=text, nature="experience", stance="positive")
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert item["nature"] == "experience" and item["stance"] == "positive"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 1


@pytest.mark.parametrize(
    "text",
    ["西湖现在开放吗[微笑R]", "西湖需要预约么。", "请问西湖怎么走", "西湖需要预约？"],
)
def test_explicit_questions_still_cannot_become_experience_votes(text):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(quote=text, nature="experience", stance="positive")
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert item["nature"] == item["stance"] == "question"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


def test_real_mixed_crowd_comment_retains_experience_and_question_as_separate_judgements():
    text = (
        "他怎么做的去的时候都没有啥人？ 我们去的每次都人挤人？ 时间不对还是地点不对？"
    )
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text,
        mention_id=None,
        object_text="整体到访",
        dimension="拥挤程度",
        nature="experience",
        stance="negative",
        link_status="unlinked",
    )
    data["evidence"].append(
        {
            **data["evidence"][0],
            "dimension": "客流变化原因",
            "nature": "question",
            "stance": "question",
        }
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    assert [(row["nature"], row["stance"]) for row in result["data"]["evidence"]] == [
        ("experience", "negative"),
        ("question", "question"),
    ]
    assert all(
        row["link_status"] == "unlinked" and row["mention_id"] is None
        for row in result["data"]["evidence"]
    )
    assert build_candidates([result], sources())[0]["stats"]["negative_count"] == 0


def test_historical_self_photography_is_not_relabelled_as_praise_for_author_images():
    text = "我去年拍的时候没有收费"
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text,
        mention_id=None,
        object_text="整体历史拍摄活动",
        dimension="历史拍摄费用",
        nature="experience",
        stance="unknown",
        link_status="unlinked",
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    assert result["data"]["evidence"][0]["nature"] == "experience"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


@pytest.mark.parametrize(
    "text,nature,stance,dimension",
    [
        ("昨天去拍的，这里景少人美", "experience", "negative", "景观丰富度"),
        (
            "这个不冷门吧?石梁飞瀑啊，属于台州标志性景点了",
            "question",
            "negative",
            "是否冷门与知名度",
        ),
        ("被美景种草了，住宿有推荐的吗？", "wish", "positive", "兴趣与出行意愿"),
    ],
)
def test_real_prereview_nature_and_stance_are_not_rewritten_by_neighbouring_words(
    text, nature, stance, dimension
):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text,
        mention_id=None,
        object_text="整体来源对象",
        dimension=dimension,
        nature=nature,
        stance=stance,
        link_status="unlinked",
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert (item["nature"], item["stance"]) == (nature, stance)
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


def test_named_rhetorical_question_keeps_negative_stance_without_becoming_experience_vote():
    text = "西湖不冷门吧？属于标志性景点。"
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text, nature="question", stance="negative", dimension="是否冷门"
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert (
        item["nature"] == "question"
        and item["stance"] == "negative"
        and item["link_status"] == "linked"
    )
    candidate = build_candidates([result], sources())[0]
    assert (
        candidate["stats"]["positive_count"]
        == candidate["stats"]["negative_count"]
        == 0
    )


def test_unclear_pronoun_is_preserved_but_cannot_be_counted_as_linked_experience():
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = "这里排队久但很美"
    data["evidence"][0].update(
        quote="这里排队久但很美", object_text="这里", stance="mixed"
    )
    result, _ = run([data], sample=sample)
    assert result["data"]["evidence"][0]["link_status"] == "uncertain"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


@pytest.mark.parametrize("object_text", [None, "", " \t\n"])
def test_linked_empty_object_uses_single_repair_without_guessing(object_text):
    bad, good = extraction(), extraction()
    if object_text is None:
        bad["evidence"][0].pop("object_text")
    else:
        bad["evidence"][0]["object_text"] = object_text
    result, calls = run([bad, good])
    assert result["status"] == "passed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "evidence[0].object_text" in repair and "uncertain" in repair
    assert result["data"]["evidence"][0]["object_text"] == "西湖"
    schema = json.loads(
        json.loads(calls[0].content)["messages"][0]["content"].split(
            "只输出schema JSON: ", 1
        )[1]
    )
    assert {"object_text", "link_status"} <= set(
        schema["$defs"]["Evidence"]["required"]
    )
    assert (
        "object_text"
        not in NoteExtraction.model_json_schema()["$defs"]["Evidence"]["required"]
    )


def test_blank_linked_object_error_aggregates_and_stops_after_one_repair():
    bad = extraction()
    bad["mentions"][0]["name"] = "不存在的地点"
    bad["evidence"][0]["object_text"] = ""
    result, calls = run([bad, bad])
    assert result["status"] == "failed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert all(
        path in repair for path in ("evidence[0].object_text", "mentions[0].name")
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_unknown_empty_object_and_legacy_link_remain_conservative(legacy):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = "这里排队久"
    data["evidence"][0].update(
        quote="这里排队久", object_text="", link_status="uncertain"
    )
    if legacy:
        data["evidence"][0].pop("link_status")
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert item["object_text"] == "" and item["link_status"] == "uncertain"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


def test_empty_image_object_stays_unverified_without_text_object_repair():
    sample, data = note(), extraction()
    sample["images"] = ["https://sns-img.xhscdn.com/photo"]
    data["evidence"][0].update(
        source_id="note-a", object_text="", citation_kind="image", image_index=1
    )
    data["image_observations"] = [
        {"source_id": "note-a", "image_index": 1, "text": "西湖"}
    ]
    data["evidence"][0]["quote"] = "西湖"
    data["evidence"].append(
        {**extraction()["evidence"][0], "mention_id": None, "link_status": "unlinked"}
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    item = result["data"]["evidence"][0]
    assert item["object_text"] == "" and item["link_status"] == "uncertain"
    assert item["nature"] == "unknown" and item["stance"] == "unknown"


def test_two_objects_keep_elderly_and_child_conditions_and_question_nature():
    sample = note()
    sample["text"] = "断桥到花港观鱼的路线"
    sample["comments"][0]["text"] = "老人小孩从断桥到花港观鱼要多久？"
    data = {
        "mentions": [
            {
                "id": name,
                "source_id": "note-a",
                "name": name,
                "category": "play",
                "quote": name,
            }
            for name in ("断桥", "花港观鱼")
        ],
        "activities": [],
        "evidence": [
            {
                "source_id": "comment-a",
                "mention_id": name,
                "object_text": name,
                "dimension": "老人小孩用时",
                "nature": "question",
                "stance": "question",
                "quote": sample["comments"][0]["text"],
                "link_status": "linked",
            }
            for name in ("断桥", "花港观鱼")
        ],
        "image_observations": [],
    }
    result, _ = run([data], sample=sample)
    assert len(result["data"]["evidence"]) == 2
    assert all("老人小孩" in item["quote"] for item in result["data"]["evidence"])
    assert len(build_candidates([result], sources())) == 2


def test_picture_only_place_is_unverified_and_repair_does_not_resubmit_image():
    sample = {
        "source_id": "note-a",
        "text": "看图片攻略",
        "comments": [],
        "images": [
            "https://invalid.example/private",
            "https://sns-webpic-qc.xhscdn.com/photo",
        ],
    }
    valid = {
        "mentions": [
            {
                "id": "pic",
                "source_id": "note-a",
                "name": "飞来峰",
                "category": "play",
                "citation_kind": "image",
                "image_index": 2,
                "quote": "飞来峰45元",
            }
        ],
        "activities": [],
        "evidence": [
            {
                "source_id": "note-a",
                "mention_id": "pic",
                "object_text": "飞来峰",
                "dimension": "价格",
                "stance": "positive",
                "nature": "experience",
                "link_status": "linked",
                "quote": "飞来峰45元",
                "citation_kind": "image",
                "image_index": 2,
            }
        ],
        "image_observations": [
            {
                "source_id": "note-a",
                "image_index": 2,
                "text": "飞来峰45元",
                "uncertain": False,
            }
        ],
    }
    invalid = copy.deepcopy(valid)
    invalid["mentions"][0]["image_index"] = 1
    ledger, guards = [], []

    async def before(info):
        guards.append(info)
        return len(guards)

    async def after(action_id, metadata):
        ledger.append(metadata)

    result, calls = run([invalid, valid], sample=sample, before=before, after=after)
    assert result["status"] == "passed" and len(calls) == 2
    assert result["submitted_images"] == [{"source_id": "note-a", "image_index": 2}]
    assert guards[0]["submitted_images"] == result["submitted_images"]
    assert guards[1]["submitted_images"] == [] and guards[1]["images_submitted"] == 0
    assert ledger[1]["image_context"] == result["submitted_images"]
    assert ledger[1]["model_data"]["evidence"][0]["model_id"] == "agnes-test"
    assert result["data"]["image_observations"][0]["uncertain"] is True
    assert result["data"]["evidence"][0]["nature"] == "unknown"
    assert result["data"]["evidence"][0]["link_status"] == "uncertain"
    assert "image_url" not in str(json.loads(calls[1].content)["messages"])
    candidate = build_candidates([result], sources())[0]
    assert "图片待核验" in candidate["uncertainty"]
    assert candidate["stats"]["positive_count"] == 0
    assert result["coverage"]["images_submitted"] == 1
    assert result["coverage"]["images_analysed"] == 1
    assert result["coverage"]["images_unanalysed"] == 1


def test_budget_refusal_has_no_submitted_comments_or_images_and_no_model_call():
    sample = note()
    sample["images"] = ["https://sns-webpic-qc.xhscdn.com/photo"]

    async def deny(info):
        assert info["images_submitted"] == 1
        return False

    result, calls = run([], sample=sample, before=deny)
    assert not calls and result["attempts"] == 0
    assert result["submitted_images"] == [] and result["images_submitted"] == 0
    assert result["coverage"]["comments_submitted"] == 0
    assert result["coverage"]["images_analysed"] == 0
    assert result["coverage"]["images_unanalysed"] == 1


def test_submitted_image_without_valid_observation_is_unanalysed():
    sample = note()
    sample["images"] = ["https://sns-webpic-qc.xhscdn.com/photo"]
    invalid = extraction()
    invalid["evidence"] = []
    result, _ = run([invalid, invalid], sample=sample)
    assert result["status"] == "failed" and result["images_submitted"] == 1
    assert result["coverage"]["images_analysed"] == 0
    assert result["coverage"]["images_unanalysed"] == 1


def test_replies_keep_parent_context_after_twenty_first_level_comments():
    sample = note()
    sample["comments"] = [
        {"source_id": f"comment-{index}", "text": "西湖需要预约吗？"}
        for index in range(20)
    ] + [
        {
            "source_id": "reply-a",
            "reply_to_source_id": "comment-0",
            "text": "我上周去了，不需要",
        }
    ]
    sample["video_unanalysed"] = True
    data = extraction()
    data["evidence"] = [
        {
            "source_id": item["source_id"],
            "mention_id": "westlake",
            "object_text": "西湖",
            "dimension": "预约",
            "nature": "question" if index < 20 else "experience",
            "stance": "question" if index < 20 else "unknown",
            "quote": item["text"],
            "link_status": "linked",
        }
        for index, item in enumerate(sample["comments"])
    ]
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed"
    assert result["coverage"]["comments_analysed"] == 21
    assert result["coverage"]["first_level_analysed"] == 20
    assert result["coverage"]["replies_analysed"] == 1
    assert result["coverage"]["video_unanalysed"] is True
    payload = json.loads(calls[0].content)
    context = json.loads(payload["messages"][1]["content"][0]["text"])
    assert context["comments"][-1]["reply_to_source_id"] == "comment-0"
    reply = next(
        row for row in result["data"]["evidence"] if row["source_id"] == "reply-a"
    )
    assert reply["quote"] == "我上周去了，不需要"
    assert reply["link_status"] == "linked"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


@pytest.mark.parametrize("case", ["ambiguous_parent", "other_place", "unnamed_object"])
def test_reply_link_requires_unique_parent_and_explicit_object(case):
    sample, data = note(), extraction()
    data["mentions"].append(
        {
            "id": "hefang",
            "source_id": "note-a",
            "name": "河坊街",
            "category": "eat",
            "quote": "河坊街可以吃饭",
        }
    )
    parent = (
        "西湖和河坊街需要预约吗？" if case == "ambiguous_parent" else "西湖需要预约吗？"
    )
    reply = "河坊街不需要" if case == "other_place" else "我上周去了，不需要"
    sample["comments"] = [
        {"source_id": "comment-a", "text": parent},
        {"source_id": "reply-a", "reply_to_source_id": "comment-a", "text": reply},
    ]
    data["evidence"] = [
        {
            "source_id": item["source_id"],
            "mention_id": "westlake",
            "object_text": "这里" if case == "unnamed_object" and index else "西湖",
            "dimension": "预约",
            "nature": "question" if index == 0 else "experience",
            "stance": "question" if index == 0 else "unknown",
            "quote": item["text"],
            "link_status": "linked",
        }
        for index, item in enumerate(sample["comments"])
    ]
    result, calls = run([data], sample=sample)
    assert len(calls) == 1 and result["status"] == "passed"
    assert result["data"]["evidence"][1]["link_status"] == "uncertain"
    assert result["data"]["evidence"][1]["quote"] == reply


def test_reply_cannot_borrow_parent_quote_or_reference_a_foreign_parent():
    sample = note()
    sample["comments"].append(
        {
            "source_id": "reply-a",
            "reply_to_source_id": "comment-a",
            "text": "真的吗？",
        }
    )
    invalid = extraction()
    invalid["evidence"].append({**invalid["evidence"][0], "source_id": "reply-a"})
    result, calls = run([invalid, invalid], sample=sample)
    assert result["status"] == "failed" and len(calls) == 2
    sample["comments"][-1]["reply_to_source_id"] = "foreign-comment"
    with pytest.raises(ValueError, match="一级评论"):
        run([], sample=sample)


def test_configured_comment_and_reply_limits_are_not_hard_coded_to_twenty():
    sample, data = note(), extraction()
    sample["comments"] = [
        {"source_id": f"comment-{index}", "text": "我去了西湖，夜景值得"}
        for index in range(50)
    ] + [
        {
            "source_id": f"reply-{index}",
            "text": "我去了西湖，夜景值得",
            "reply_to_source_id": "comment-0",
        }
        for index in range(100)
    ]
    data["evidence"] = [
        {
            **data["evidence"][0],
            "source_id": item["source_id"],
            "quote": item["text"],
            "stance": "positive",
        }
        for item in sample["comments"]
    ]
    result, _ = run([data], sample=sample, comment_limit=50, reply_limit=100)
    assert result["status"] == "passed"
    assert result["coverage"]["first_level_analysed"] == 50
    assert result["coverage"]["replies_analysed"] == 100
    assert result["coverage"]["comments_analysed"] == 150
    none = extraction()
    none["evidence"] = []
    result, _ = run([none], sample=sample, comment_limit=0, reply_limit=100)
    assert result["coverage"]["comments_submitted"] == 0
    assert len(result["coverage"]["comment_ids_unanalysed"]) == 150


@pytest.mark.parametrize(
    "limits",
    [
        {"comment_limit": 51},
        {"comment_limit": True},
        {"reply_limit": 101},
        {"reply_limit": -1},
    ],
)
def test_invalid_configured_limits_are_rejected_before_network(limits):
    with pytest.raises(ValueError, match="提交预算"):
        run([], **limits)


def test_comment_text_limit_is_honest_partial_coverage_and_keeps_short_comments():
    sample, data = note(), extraction()
    sample["comments"] = [
        {"source_id": "long-one", "text": "好" * 20000},
        {"source_id": "long-two", "text": "赞" * 20000},
        {"source_id": "short", "text": "西湖需要预约吗？"},
    ]
    data["evidence"] = [
        {
            **data["evidence"][0],
            "source_id": "long-one",
            "quote": "好" * 20,
            "mention_id": None,
            "nature": "unknown",
            "stance": "unknown",
            "link_status": "unlinked",
        },
        {
            **data["evidence"][0],
            "source_id": "short",
            "quote": "西湖需要预约吗？",
            "nature": "question",
            "stance": "question",
        },
    ]
    result, _ = run([data], sample=sample)
    assert result["status"] == "passed"
    assert result["coverage"]["comments_received"] == 3
    assert result["coverage"]["comments_submitted"] == 2
    assert result["coverage"]["comments_analysed"] == 2
    assert result["coverage"]["comment_ids_unanalysed"] == ["long-two"]
    assert result["coverage"]["comment_ids_text_limited"] == ["long-two"]


def test_comment_and_image_coverage_reports_unsubmitted_data_without_fake_analysis():
    sample = note()
    sample["comments"] = [
        {"source_id": f"comment-{index}", "text": "这是什么地方？"}
        for index in range(21)
    ]
    sample["images"] = ["https://invalid.example/pic"]
    sample["contains_video"] = True
    data = {
        "mentions": [],
        "activities": [],
        "evidence": [
            {
                "source_id": f"comment-{index}",
                "mention_id": None,
                "object_text": "",
                "dimension": "询问",
                "stance": "question",
                "nature": "question",
                "quote": "这是什么地方？",
                "link_status": "unlinked",
            }
            for index in range(20)
        ],
    }
    result, _ = run([data], sample=sample)
    coverage = result["coverage"]
    assert (
        coverage["comments_received"] == 21
        and coverage["comments_submitted"] == 20
        and coverage["comments_analysed"] == 20
    )
    assert coverage["comment_ids_unanalysed"] == ["comment-20"]
    assert coverage["images_unanalysed"] == 1 and coverage["video_unanalysed"] is True


def test_generic_lodging_and_same_name_branches_stay_separate():
    data = extraction()
    data["mentions"] += [
        {
            "id": "store-one",
            "source_id": "note-a",
            "name": "豆浆店",
            "city": "杭州",
            "category": "eat",
            "branch_hint": "鼓楼",
            "quote": "鼓楼豆浆店",
        },
        {
            "id": "store-two",
            "source_id": "note-a",
            "name": "豆浆店",
            "city": "杭州",
            "category": "eat",
            "branch_hint": "运河",
            "quote": "运河豆浆店",
        },
        {
            "id": "generic",
            "source_id": "note-a",
            "name": "山脚民宿",
            "category": "stay",
            "kind": "generic",
            "quote": "山脚民宿",
        },
    ]
    sample = note()
    sample["text"] += "鼓楼豆浆店，运河豆浆店，山脚民宿。"
    result, _ = run([data], sample=sample)
    candidates = build_candidates([result], sources())
    assert len([item for item in candidates if item["name"] == "豆浆店"]) == 2
    generic = next(item for item in candidates if item["name"] == "山脚民宿")
    assert generic["kind"] == "generic"
    assert "泛称，具体店铺或住宿名称待确认" in generic["uncertainty"]


def test_generic_mentions_do_not_inflate_direction_ranking_or_member_counts():
    candidates = [
        {
            "city": city,
            "category": "play",
            "kind": "place",
            "mention_ids": [f"place-{city}-{index}"],
            "evidence_ids": [],
        }
        for city, count in [("A", 1), ("B", 2), ("C", 3), ("D", 4)]
        for index in range(count)
    ]
    generic = [
        {
            "city": "A",
            "category": "eat",
            "kind": "generic",
            "mention_ids": [f"dish-{index}"],
            "evidence_ids": [],
        }
        for index in range(10)
    ]
    directions = build_directions(candidates + generic, [])
    assert [row["city"] for row in directions] == ["D", "C", "B"]
    assert [len(row["mention_ids"]) for row in directions] == [4, 3, 2]
    assert build_directions(generic, []) == []


def test_gaps_depend_on_confirmed_trip_times_and_do_not_erase_food_mentions():
    unknown = build_gaps({}, [])
    assert {item["status"] for item in unknown} == {"undetermined"}
    request = {
        "destination": "杭州",
        "start_at": "2026-10-13T10:00:00+08:00",
        "duration_minutes": 600,
    }
    open_gaps = build_gaps(request, [])
    assert [item["period"] for item in open_gaps] == ["lunch", "dinner"]
    assert all(item["status"] == "open" for item in open_gaps)
    food = [{"id": "bean", "category": "eat", "city": "杭州市"}]
    with_food = build_gaps(request, food)
    assert all(
        item["status"] == "undetermined" and item["candidate_ids"] == ["bean"]
        for item in with_food
    )
    assert (
        build_gaps(
            {"start_at": "2026-10-13T23:00:00+08:00", "duration_minutes": 120}, []
        )[0]["status"]
        == "undetermined"
    )
    assert any(
        item["kind"] == "lodging" and item["status"] == "open"
        for item in build_gaps({**request, "duration_minutes": 2880}, [])
    )


def test_city_without_source_support_is_not_promoted_to_exploration_direction():
    data = extraction()
    data["mentions"][0]["city"] = "成都"
    result, _ = run([data])
    candidates = build_candidates([result], sources())
    assert candidates[0]["city"] is None
    assert build_directions(candidates, [result]) == []


def test_exploration_directions_are_bounded_and_backed_by_candidate_mentions():
    candidates = [
        {
            "id": f"candidate-{city}",
            "city": city,
            "category": "play",
            "mention_ids": [f"mention-{city}"],
            "evidence_ids": [],
        }
        for city in ("杭州", "安吉", "桐庐", "南京")
    ]
    directions = build_directions(candidates, [])
    assert len(directions) == 3
    assert all(
        item["kind"] == "direction"
        and item["match_status"] == "unmatched"
        and len(item["mention_ids"]) == 1
        for item in directions
    )
    assert all("尚未由用户采用" in " ".join(item["uncertainty"]) for item in directions)


@pytest.mark.parametrize(
    "description", ["票价15元", "建议预约", "排队一小时", "来源建议散步", "   "]
)
def test_text_activity_description_cannot_borrow_details_outside_its_own_quote(
    description,
):
    sample = note()
    sample["text"] += "\n西湖票价15元，建议预约。"
    bad = extraction()
    bad["activities"][0].update(quote="杭州西湖", description=description)
    result, calls = run([bad], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    assert result["data"]["activities"] == []
    assert result["coverage"]["comments_analysed"] == 1
    assert result["data"]["activity_validation"]["rejected"] == 1
    assert (
        "activities[0].description"
        in result["data"]["activity_validation"]["errors"][0]
    )


def test_activity_description_guard_joins_name_and_structural_errors_in_single_repair():
    bad = extraction()
    bad["activities"][0].update(quote="杭州西湖", description="票价15元")
    bad["mentions"][0]["name"] = "不存在的地点"
    bad["evidence"][0].pop("dimension")
    result, calls = run([bad, extraction()])
    repair = calls[1].content.decode()
    assert result["status"] == "passed" and len(calls) == 2
    assert "activities[0].description" in repair
    assert "mentions[0].name" in repair and "evidence.0.dimension" in repair


def test_supported_activity_description_uses_resolved_text_lines_and_retains_raw_wording():
    sample = note()
    sample["text"] = "西湖游玩。\n历史门票15元/人，需提前预约。\n河坊街可以吃饭。"
    data = extraction()
    data["mentions"][0]["quote"] = {"part": "text", "start_line": 1, "end_line": 1}
    data["activities"][0].update(
        quote={"part": "text", "start_line": 1, "end_line": 2},
        description="历史门票15元/人，需提前预约。",
    )
    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    activity = result["data"]["activities"][0]
    assert activity["description"] == "历史门票15元/人，需提前预约。"
    assert activity["description"] in activity["quote"]


def test_visual_activity_remains_an_unverified_observation_without_text_literal_requirement():
    data = extraction()
    data["mentions"][0].update(
        quote="西湖岸边的步道", citation_kind="image", image_index=1
    )
    data["activities"][0].update(
        quote="西湖岸边的步道",
        description="沿湖步道散步的视觉线索",
        citation_kind="image",
        image_index=1,
    )
    data["image_observations"] = [
        {"source_id": "note-a", "image_index": 1, "text": "西湖岸边的步道"}
    ]
    result, calls = run(
        [data], sample={**note(), "images": ["https://sns-img.xhscdn.com/photo"]}
    )
    assert result["status"] == "passed" and len(calls) == 1
    candidate = build_candidates([result], sources())[0]
    assert "图片待核验" in candidate["uncertainty"]
