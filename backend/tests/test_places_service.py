"""Synthetic provider responses only; these checks are not account acceptance."""

import pytest

from easytrip.services.places import match_pois, normalize_poi, rank_candidates

READ_AT = "2026-10-14T10:00:00+08:00"


def raw_poi(identifier="B001", name="西湖", **changes):
    return {
        "id": identifier,
        "name": name,
        "location": "120.1,30.2",
        "address": "龙井路1号",
        "cityname": "杭州市",
        "adcode": "330106",
        "type": "风景名胜;风景名胜;风景名胜",
        **changes,
    }


def candidate(**changes):
    return {
        "id": "candidate-1",
        "name": "西湖",
        "city": "杭州",
        "kind": "place",
        "category": "play",
        "mention_ids": ["mention-1"],
        "evidence_ids": [],
        **changes,
    }


def match(rows, **changes):
    return match_pois(
        candidate(**changes),
        [],
        {"status": "1", "pois": rows, "search_complete": True},
        READ_AT,
    )


def test_normalize_provider_fields_and_keep_missing_entrance():
    poi = normalize_poi(raw_poi(business={"cost": "100"}), READ_AT)
    assert poi["location"] == [120.1, 30.2]
    assert poi["city"] == "杭州市"
    assert poi["entrance_location"] is None
    assert poi["missing_fields"] == ["entrance_location"]
    assert "business" not in poi and "cost" not in poi
    assert poi["read_at"] == READ_AT and poi["api_version"] == "v5"
    assert normalize_poi(poi, "2026-10-15T10:00:00+08:00") == poi
    with_entrance = normalize_poi(
        raw_poi(navi={"entr_location": "120.15,30.25"}), READ_AT
    )
    assert with_entrance["entrance_location"] == [120.15, 30.25]


@pytest.mark.parametrize(
    "location",
    [None, "", [], [120], "120", "nan,30", "120,inf", "181,30", "120,91", [True, 30]],
)
def test_invalid_location_remains_unknown_and_cannot_match(location):
    raw = raw_poi(location=location)
    assert normalize_poi(raw, READ_AT)["location"] is None
    assert "location" in normalize_poi(raw, READ_AT)["missing_fields"]
    result = match([raw])
    assert result["status"] == "unmatched" and result["options"] == []


def test_complete_exact_same_city_category_is_unique():
    result = match([raw_poi()])
    assert result["status"] == "unique"
    assert result["poi"]["id"] == "B001"
    assert result["options"] == [result["poi"]]


@pytest.mark.parametrize(
    "changes",
    [
        {"city": None},
        {"city": "上海"},
        {"category": "eat"},
        {"kind": "generic"},
        {"name": "西"},
    ],
)
def test_missing_city_wrong_city_category_and_abbreviation_do_not_auto_match(changes):
    result = match([raw_poi()], **changes)
    assert result["status"] == "ambiguous" and result["poi"] is None
    assert len(result["options"]) == 1


@pytest.mark.parametrize("field", ["cityname", "type", "name", "address"])
def test_missing_provider_fields_need_confirmation(field):
    assert match([raw_poi(**{field: []})])["status"] == "ambiguous"


def test_one_valid_result_does_not_hide_an_option_without_coordinates_or_city():
    for unknown in [raw_poi("B002", location=[]), raw_poi("B002", cityname=[])]:
        assert match([raw_poi(), unknown])["status"] == "ambiguous"


def test_count_equal_page_size_does_not_prove_complete_search():
    result = match_pois(
        candidate(), [], {"status": "1", "pois": [raw_poi()], "count": "1"}, READ_AT
    )
    assert result["status"] == "ambiguous" and "不完整" in result["reason"]


def test_branches_do_not_silently_select_exact_first_hit():
    rows = [
        raw_poi("B001", "星巴克", type="餐饮服务;咖啡厅"),
        raw_poi("B002", "星巴克(湖滨店)", type="餐饮服务;咖啡厅"),
    ]
    assert match(rows, name="星巴克", category="eat")["status"] == "ambiguous"
    assert match(rows, name="星巴克（湖滨店）", category="eat")["poi"]["id"] == "B002"


def test_explicit_source_branch_and_address_disambiguate():
    rows = [
        raw_poi("B001", "游埠豆浆(鼓楼店)", type="餐饮服务;中餐厅"),
        raw_poi(
            "B002",
            "游埠豆浆(十五奎巷店)",
            type="餐饮服务;中餐厅",
            address="十五奎巷3号",
        ),
    ]
    mentioned = {
        "id": "mention-1",
        "name": "游埠豆浆",
        "city": "杭州",
        "branch_hint": "十五奎巷",
        "kind": "place",
    }
    result = match_pois(
        candidate(name="游埠豆浆", category="eat"),
        [mentioned],
        {"status": "1", "pois": rows, "search_complete": True},
        READ_AT,
    )
    assert result["status"] == "unique" and result["poi"]["id"] == "B002"
    mentioned["branch_hint"] = "河坊街"
    assert (
        match_pois(
            candidate(name="游埠豆浆", category="eat"),
            [mentioned],
            {"status": "1", "pois": rows, "search_complete": True},
            READ_AT,
        )["status"]
        == "ambiguous"
    )


def test_conflicting_source_city_or_area_needs_confirmation():
    for changes in [{"city": "上海"}, {"kind": "area"}]:
        mentioned = {"id": "mention-1", "name": "西湖", **changes}
        assert (
            match_pois(
                candidate(),
                [mentioned],
                {"status": "1", "pois": [raw_poi()], "search_complete": True},
                READ_AT,
            )["status"]
            == "ambiguous"
        )


def test_branch_hint_cannot_override_explicit_original_branch():
    rows = [raw_poi(name="游埠豆浆(十五奎巷店)", type="餐饮服务;中餐厅")]
    assert (
        match(rows, name="游埠豆浆(鼓楼店)", category="eat", branch_hint="十五奎巷")[
            "status"
        ]
        == "ambiguous"
    )
    assert (
        match(rows, name="游埠豆浆", category="eat", branch_hint="店")["status"]
        == "ambiguous"
    )


def test_aliases_are_not_guessed_and_distinct_activities_keep_same_provider_id():
    assert match([raw_poi()], name="西子湖")["status"] == "ambiguous"
    first = match_pois(
        candidate(id="a"),
        [],
        {"status": "1", "pois": [raw_poi()], "search_complete": True},
        READ_AT,
    )
    second = match_pois(
        candidate(id="b"),
        [],
        {"status": "1", "pois": [raw_poi()], "search_complete": True},
        READ_AT,
    )
    assert first["poi"]["id"] == second["poi"]["id"]


def test_rank_filters_only_verified_city_category_and_preserves_evidence_ids():
    verified = candidate(
        id="hangzhou",
        match_status="unique",
        poi=normalize_poi(raw_poi(), READ_AT),
        stats={"author_count": 2, "positive_count": 2, "negative_count": 1},
        evidence_ids=["proof"],
    )
    wrong_city = {
        **verified,
        "id": "shanghai",
        "poi": normalize_poi(raw_poi(cityname="上海市"), READ_AT),
    }
    unknown = candidate(id="unknown", stats={"author_count": 999}, cost=99999)
    ranked = rank_candidates(
        [unknown, wrong_city, verified], {"destination": "杭州", "budget_cny": 10}
    )
    assert [row["id"] for row in ranked] == ["hangzhou", "unknown"]
    assert ranked[0]["evidence_ids"] == ["proof"]
    assert "预算可行性待确认" in " ".join(ranked[0]["ranking_reasons"])
    assert verified.get("ranking_reasons") is None
    assert len(rank_candidates([wrong_city], {"destination": "西湖景区"})) == 1


def test_rank_only_counts_linked_text_experience_comments_once_per_source():
    sources = {
        "comment-1": {
            "kind": "comment",
            "author_key": "author-1",
            "published_at": "2026-10-01T10:00:00+08:00",
        },
        "comment-2": {"kind": "comment", "author_key": "author-1"},
    }
    evidence = [
        {
            "id": "positive",
            "mention_id": "mention-1",
            "source_id": "comment-1",
            "nature": "experience",
            "link_status": "linked",
            "stance": "positive",
        },
        {
            "id": "duplicate",
            "mention_id": "mention-1",
            "source_id": "comment-1",
            "nature": "experience",
            "link_status": "linked",
            "stance": "positive",
        },
        {
            "id": "wish",
            "mention_id": "mention-1",
            "source_id": "comment-2",
            "nature": "wish",
            "link_status": "linked",
            "stance": "positive",
        },
        {
            "id": "unlinked",
            "source_id": "comment-2",
            "nature": "experience",
            "link_status": "unlinked",
            "stance": "positive",
        },
        {
            "id": "negative",
            "mention_id": "mention-1",
            "source_id": "comment-2",
            "nature": "experience",
            "link_status": "linked",
            "stance": "mixed",
        },
    ]
    ranked = rank_candidates(
        [
            candidate(
                evidence_ids=[row["id"] for row in evidence],
                stats={"author_count": 99, "positive_count": 99},
                likes=999999,
            )
        ],
        {},
        sources,
        evidence,
    )
    reasons = " ".join(ranked[0]["ranking_reasons"])
    assert "覆盖1位不同作者" in reasons
    assert "支持2条、反对1条" in reasons
    assert "2026-10-01" in reasons
    assert "平台好评率" in reasons and "999999" not in reasons


def test_rank_preserves_original_source_timezone_and_orders_by_publication():
    rows = [
        candidate(id="old", evidence_ids=["old"]),
        candidate(id="new", evidence_ids=["new"]),
    ]
    sources = {
        "old": {
            "kind": "note",
            "author_key": "author",
            "published_at": "2026-10-01T23:30:00-04:00",
        },
        "new": {
            "kind": "note",
            "author_key": "author",
            "published_at": "2026-10-02T12:30:00+08:00",
        },
    }
    evidence = [
        {"id": "old", "source_id": "old", "mention_id": "mention-1"},
        {"id": "new", "source_id": "new", "mention_id": "mention-1"},
    ]
    ranked = rank_candidates(rows, {}, sources, evidence)
    assert [row["id"] for row in ranked] == ["new", "old"]
    assert "2026-10-01T23:30:00-04:00" in " ".join(ranked[1]["ranking_reasons"])


def test_rank_includes_mention_source_authors_and_deduplicates_comment_authors():
    sources = {
        "note": {"kind": "note", "author_key": "author-1"},
        "comment": {"kind": "comment", "author_key": "author-2"},
        "duplicate": {"kind": "note", "author_key": "author-1"},
    }
    evidence = [
        {
            "id": "e1",
            "source_id": "comment",
            "mention_id": "mention-1",
            "stance": "positive",
            "nature": "experience",
            "link_status": "linked",
        }
    ]
    ranked = rank_candidates(
        [candidate(source_ids=["note", "duplicate"], evidence_ids=["e1"])],
        {},
        sources,
        evidence,
    )
    assert "覆盖2位不同作者" in " ".join(ranked[0]["ranking_reasons"])


def test_literal_preferences_rank_only_grounded_candidate_passages_and_keep_evidence_ids():
    sources = {
        "positive": {
            "kind": "comment",
            "author_key": "author",
            "text": "这里安静，水边可以拍照。",
        },
        "negative": {"kind": "comment", "author_key": "author", "text": "不太安静。"},
        "activity": {"kind": "note", "author_key": "author", "text": "水边可以拍照。"},
    }
    evidence = [
        {
            "id": "good",
            "source_id": "positive",
            "mention_id": "mention-1",
            "quote": "这里安静，水边可以拍照。",
            "stance": "positive",
            "nature": "experience",
            "link_status": "linked",
        },
        {
            "id": "bad",
            "source_id": "negative",
            "mention_id": "mention-1",
            "quote": "不太安静。",
            "stance": "negative",
            "nature": "experience",
            "link_status": "linked",
        },
        {
            "id": "invented",
            "source_id": "positive",
            "mention_id": "mention-1",
            "quote": "免费停车",
            "stance": "positive",
            "nature": "experience",
            "link_status": "linked",
        },
    ]
    activities = [
        {
            "id": "a1",
            "source_id": "activity",
            "mention_id": "mention-1",
            "quote": "水边可以拍照。",
        }
    ]
    ranked = rank_candidates(
        [
            candidate(id="negative", evidence_ids=["bad"]),
            candidate(id="positive", evidence_ids=["good", "invented"]),
        ],
        {"preferences": "安静、拍照；免费停车"},
        sources,
        evidence,
        activities,
    )
    assert [row["id"] for row in ranked] == ["positive", "negative"]
    reasons = " ".join(ranked[0]["ranking_reasons"])
    assert "原文命中“安静”" in reasons and "证据good" in reasons
    assert "活动a1" in reasons and "仅字面关联" in reasons
    assert "未明确关联：免费停车" in reasons
    assert ranked[0]["evidence_ids"] == ["good", "invented"]
    assert "核对冲突" in " ".join(ranked[1]["ranking_reasons"])


def test_source_literal_preference_match_does_not_cross_sentences_or_negate_another_clause():
    sources = {"note": {"kind": "note", "text": "西湖不贵，很安静。别的景点需要预约。"}}
    row = candidate(source_ids=["note"])
    reasons = " ".join(
        rank_candidates([row], {"preferences": "安静，预约"}, sources)[0][
            "ranking_reasons"
        ]
    )
    assert "原文命中“安静”" in reasons
    assert "偏好短语“安静”" not in reasons
    assert "未明确关联：预约" in reasons


def test_rank_has_stable_ties_and_negative_evidence_changes_order():
    rows = [
        candidate(id="negative", stats={"author_count": 2, "negative_count": 2}),
        candidate(id="positive", stats={"author_count": 2, "positive_count": 1}),
    ]
    assert [row["id"] for row in rank_candidates(rows, {})] == ["positive", "negative"]
    assert [
        row["id"]
        for row in rank_candidates([candidate(id="first"), candidate(id="second")], {})
    ] == ["first", "second"]
