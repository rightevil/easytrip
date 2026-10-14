"""Synthetic mocked outputs check validation/metrics, not model semantic quality."""

import json

import pytest
from test_evidence import extraction, line_ref, note, run, sources

from easytrip.domain.evidence_metrics import object_dimension_judged
from easytrip.services.evidence import build_candidates


@pytest.mark.parametrize("kind", ["walk", "distance", "arrow", "overlapping_name"])
def test_explicit_relations_cannot_inherit_one_endpoint_place_vote(kind):
    sample, data = note(), extraction()
    sample["text"] = "西湖、河坊街与杭州东。"
    data["activities"] = []
    data["mentions"][0]["quote"] = line_ref()
    data["mentions"].extend(
        {**data["mentions"][0], "id": identity, "name": name}
        for identity, name in [("hefang", "河坊街"), ("station", "杭州东")]
    )
    texts = {
        "walk": "我从西湖步行到河坊街，很舒服",
        "arrow": "我去了西湖，沿西湖到河坊街徒步很舒服",
        "overlapping_name": "我从西湖走到西湖宾馆，这条路线很舒服",
        "distance": "哪个区域离杭州东比较近？",
    }
    objects = {
        "walk": "西湖步行至河坊街",
        "arrow": "西湖→河坊街",
        "overlapping_name": "西湖到西湖宾馆的路线",
        "distance": "各区域与杭州东的距离比较",
    }
    if kind == "overlapping_name":
        sample["text"] += "西湖宾馆。"
        data["mentions"].append(
            {**data["mentions"][0], "id": "hotel", "name": "西湖宾馆"}
        )
    text = texts[kind]
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        object_text=objects[kind],
        dimension="交通距离" if kind == "distance" else "步行体验",
        quote=text,
        mention_id="station" if kind == "distance" else "westlake",
        link_status="linked",
        nature="question" if kind == "distance" else "experience",
        stance="question" if kind == "distance" else "positive",
    )
    result, calls = run([data], sample=sample)
    item = result["data"]["evidence"][0]
    assert len(calls) == 1 and object_dimension_judged(item)
    assert item["link_status"] == "unlinked" and item["mention_id"] is None
    assert item["quote"] == text
    assert all(
        candidate["stats"]["positive_count"]
        == candidate["stats"]["negative_count"]
        == 0
        for candidate in build_candidates([result], sources())
    )


def test_nested_place_names_do_not_turn_a_single_place_into_a_relation():
    sample, data = note(), extraction()
    sample["text"] = "杭州西湖适合散步。"
    sample["comments"][0]["text"] = "我去了杭州西湖，这次步行很好"
    data["activities"] = []
    data["mentions"][0].update(name="杭州西湖", quote=line_ref())
    data["mentions"].append({**data["mentions"][0], "id": "short-name", "name": "西湖"})
    data["evidence"][0].update(
        object_text="杭州西湖步行",
        dimension="步行体验",
        stance="positive",
        quote=sample["comments"][0]["text"],
    )
    result, _ = run([data], sample=sample)
    item = result["data"]["evidence"][0]
    assert item["link_status"] == "linked" and item["mention_id"] is not None


@pytest.mark.parametrize(
    "text,judgements",
    [
        (
            "省博建议留半天，你这条路线一天逛不完。",
            [
                (
                    "省博建议留半天",
                    "省博",
                    "建议停留时长",
                    "suggestion",
                    "unknown",
                    "museum",
                ),
                (
                    "你这条路线一天逛不完",
                    "整体路线",
                    "单日可完成性",
                    "suggestion",
                    "negative",
                    None,
                ),
            ],
        ),
        (
            "最终计划能分享吗？西湖要不要单独留两天？",
            [
                (
                    "最终计划能分享吗？",
                    "最终计划",
                    "计划参考请求",
                    "question",
                    "question",
                    None,
                ),
                (
                    "西湖要不要单独留两天？",
                    "西湖",
                    "游览天数",
                    "question",
                    "question",
                    "westlake",
                ),
            ],
        ),
    ],
    ids=["museum-and-whole-route", "final-plan-and-lake-days"],
)
def test_mocked_multi_object_judgements_survive_validation_without_place_votes(
    text, judgements
):
    sample, data = note(), extraction()
    sample["text"] = "杭州西湖和省博的一日行程。"
    sample["comments"][0]["text"] = text
    data["mentions"][0]["quote"] = line_ref()
    data["mentions"].append({**data["mentions"][0], "id": "museum", "name": "省博"})
    data["activities"] = []
    data["evidence"] = [
        {
            **extraction()["evidence"][0],
            "quote": quote,
            "object_text": object_text,
            "dimension": dimension,
            "nature": nature,
            "stance": stance,
            "mention_id": mention_id,
            "link_status": "linked" if mention_id else "unlinked",
        }
        for quote, object_text, dimension, nature, stance, mention_id in judgements
    ]

    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    rows = result["data"]["evidence"]
    assert [
        (
            row["quote"],
            row["object_text"],
            row["dimension"],
            row["nature"],
            row["stance"],
        )
        for row in rows
    ] == [judgement[:5] for judgement in judgements]
    assert all(object_dimension_judged(row) for row in rows)
    for row, judgement in zip(rows, judgements, strict=True):
        assert (row["mention_id"] is None) == (judgement[5] is None)
    assert result["coverage"]["comments_analysed"] == 1
    assert all(
        candidate["stats"]["positive_count"]
        == candidate["stats"]["negative_count"]
        == 0
        for candidate in build_candidates([result], sources())
    )


@pytest.mark.parametrize(
    "text,object_text,dimension,nature,stance",
    [
        (
            "能把评论区的攻略归纳一下吗？",
            "评论区攻略",
            "内容整理请求",
            "question",
            "question",
        ),
        (
            "账号主页会继续发路线，欢迎看看。",
            "账号主页",
            "更新与推广",
            "suggestion",
            "unknown",
        ),
        (
            "这条无名老巷拍得像电影布景。",
            "笔记里的无名老巷",
            "画面风格",
            "content_reaction",
            "positive",
        ),
        (
            "城东和城西哪个离杭州东更近？",
            "城东/城西到杭州东",
            "距离比较",
            "question",
            "question",
        ),
    ],
    ids=[
        "comment-guide",
        "account-home",
        "unnamed-alley",
        "area-to-station-comparison",
    ],
)
def test_mocked_explicit_non_place_objects_count_as_judged_without_place_votes(
    text, object_text, dimension, nature, stance
):
    sample, data = note(), extraction()
    sample["text"] = (
        "西湖附近的一条无名老巷。评论区有攻略，账号主页更新路线。城东与城西待比较。"
    )
    sample["comments"][0]["text"] = text
    data["mentions"][0]["quote"] = line_ref()
    data["activities"] = []
    data["evidence"][0].update(
        quote=text,
        object_text=object_text,
        dimension=dimension,
        nature=nature,
        stance=stance,
        mention_id=None,
        link_status="unlinked",
    )

    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    row = result["data"]["evidence"][0]
    assert (row["object_text"], row["dimension"], row["nature"], row["stance"]) == (
        object_text,
        dimension,
        nature,
        stance,
    )
    assert row["mention_id"] is None and row["link_status"] == "unlinked"
    assert object_dimension_judged(row)
    stats = build_candidates([result], sources())[0]["stats"]
    assert stats["positive_count"] == stats["negative_count"] == 0


@pytest.mark.parametrize("text", ["我前天去了西湖！", "我三年前来过西湖[庆祝]"])
def test_mocked_neutral_visit_is_judged_but_never_counted_as_praise(text):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text, dimension="历史到访记录", nature="experience", stance="unknown"
    )

    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    row = result["data"]["evidence"][0]
    assert row["nature"] == "experience" and row["stance"] == "unknown"
    assert row["link_status"] == "linked" and object_dimension_judged(row)
    stats = build_candidates([result], sources())[0]["stats"]
    assert stats["positive_count"] == stats["negative_count"] == 0


@pytest.mark.parametrize(
    "text,dimension,nature,stance",
    [
        ("可以", "unknown", "unknown", "unknown"),
        ("明天还开放吗？", "开放状态", "question", "question"),
    ],
)
def test_mocked_insufficient_context_keeps_empty_object_and_abstention(
    text, dimension, nature, stance
):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = text
    data["evidence"][0].update(
        quote=text,
        object_text="",
        dimension=dimension,
        nature=nature,
        stance=stance,
        mention_id=None,
        link_status="uncertain",
    )

    result, calls = run([data], sample=sample)
    assert result["status"] == "passed" and len(calls) == 1
    row = result["data"]["evidence"][0]
    assert row["object_text"] == "" and row["mention_id"] is None
    assert (row["dimension"], row["nature"], row["stance"]) == (
        dimension,
        nature,
        stance,
    )
    assert row["link_status"] == "uncertain" and not object_dimension_judged(row)
    stats = build_candidates([result], sources())[0]["stats"]
    assert stats["positive_count"] == stats["negative_count"] == 0


@pytest.mark.parametrize("quote", ["西湖适合散步", "评论区攻略很详细"])
def test_non_place_objects_do_not_relax_source_literal_quote_protection(quote):
    sample, data = note(), extraction()
    sample["comments"][0]["text"] = "能归纳一下评论区攻略吗？"
    data["evidence"][0].update(
        quote=quote,
        object_text="评论区攻略",
        dimension="内容整理请求",
        nature="question",
        stance="question",
        mention_id=None,
        link_status="unlinked",
    )

    result, calls = run([data, data], sample=sample)
    assert result["status"] == "failed" and result["data"] is None
    assert len(calls) == 2
    assert (
        "evidence[0].quote" in json.loads(calls[1].content)["messages"][-1]["content"]
    )
