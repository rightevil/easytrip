import asyncio
import copy
import json

import httpx
import pytest

from easytrip.config import Settings
from easytrip.services.evidence import (
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
                "description": "来源建议散步",
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


def test_unknown_source_and_activity_link_report_without_key_error():
    bad = extraction()
    bad["activities"][0].update(
        source_id="foreign-source", mention_id="foreign-mention"
    )
    result, calls = run([bad, bad])
    assert result["status"] == "failed" and len(calls) == 2
    repair = json.loads(calls[1].content)["messages"][-1]["content"]
    assert "activities[0].source_id" in repair and "activities[0].mention_id" in repair
    assert "choices" not in repair


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


def test_unclear_pronoun_is_preserved_but_cannot_be_counted_as_linked_experience():
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = "这里排队久但很美"
    data["evidence"][0].update(
        quote="这里排队久但很美", object_text="这里", stance="mixed"
    )
    result, _ = run([data], sample=sample)
    assert result["data"]["evidence"][0]["link_status"] == "uncertain"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


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
            "stance": "question" if index < 20 else "positive",
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
    assert reply["link_status"] == "uncertain"
    assert build_candidates([result], sources())[0]["stats"]["positive_count"] == 0


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
    assert "泛称，具体店铺或住宿名称待确认" in generic["uncertainty"]


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
    assert build_directions(candidates) == []


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
    directions = build_directions(candidates)
    assert len(directions) == 3
    assert all(
        item["kind"] == "direction"
        and item["match_status"] == "unmatched"
        and len(item["mention_ids"]) == 1
        for item in directions
    )
    assert all("尚未由用户采用" in " ".join(item["uncertainty"]) for item in directions)
