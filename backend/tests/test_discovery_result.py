import copy
import json
from uuid import UUID

import pytest

from easytrip.services.evidence import assemble_candidates


def request(**values):
    return {
        "start_at": "2026-10-13T10:00:00+08:00",
        "duration_minutes": 2880,
        **values,
    }


def sample(
    *, city="杭州", name="西湖", category="play", source_id="note-a", picture=False
):
    citation = {"citation_kind": "image", "image_index": 1} if picture else {}
    return {
        "mentions": [
            {
                "id": "mention-" + source_id,
                "source_id": source_id,
                "name": name,
                "city": city,
                "category": category,
                "quote": name,
                **citation,
            }
        ],
        "evidence": [],
        "activities": [],
        "image_observations": [],
    }


def source(source_id="note-a", *, city="杭州", name="西湖"):
    return {
        "id": source_id,
        "kind": "note",
        "title": (city or "") + name,
        "text": name + "旅行线索",
        "author_key": "private-author-" + source_id,
    }


def test_effective_conditions_only_use_confirmed_intent_without_mutating_saved_trip():
    saved = {}
    understanding = {
        "intent": {
            "destination": {"value": "杭州", "provenance": "inferred"},
            "origin": {"value": "杭州东", "provenance": "user"},
            "start_at": {"value": "2026-10-13T10:00:00+08:00", "provenance": "user"},
            "duration_minutes": {"value": 180, "provenance": "inferred"},
            "transport": {"value": "walking", "provenance": "user"},
            "participants": {"value": 2, "provenance": "user"},
            "budget_cny": {"value": 0, "provenance": "user"},
            "preferences": {"value": "安静", "provenance": "default"},
        }
    }
    before = copy.deepcopy(understanding)
    result = assemble_candidates("run-a", saved, understanding, [], [])
    assert saved == {} and understanding == before
    effective = result["effective_request"]
    assert effective["destination"] is None and effective["duration_minutes"] is None
    assert effective["origin"] == "杭州东" and effective["participants"] == 2
    assert (
        effective["transport"] == "walking" and effective["transport_source"] == "user"
    )
    assert effective["budget_cny"] == 0
    assert {gap["status"] for gap in result["gaps"]} == {"undetermined"}
    assert result["gap_queries"] == []


def test_saved_confirmed_destination_is_not_replaced_by_an_inference():
    saved = request(destination="杭州")
    before = copy.deepcopy(saved)
    result = assemble_candidates(
        "run-a",
        saved,
        {"intent": {"destination": {"value": "南京", "provenance": "inferred"}}},
        [],
        [],
    )
    assert saved == before and result["effective_request"]["destination"] == "杭州"
    assert {query["purpose"] for query in result["gap_queries"]} == {
        "meal_gap",
        "lodging_gap",
    }
    assert all(query["query"].startswith("杭州 ") for query in result["gap_queries"])


def test_confirmed_conversation_timing_drives_gaps_without_saving_it_to_trip():
    result = assemble_candidates(
        "run-a",
        {},
        {
            "intent": {
                "destination": {"value": "杭州", "provenance": "user"},
                "start_at": {
                    "value": "2026-10-13T10:00:00+08:00",
                    "provenance": "user",
                },
                "duration_minutes": {"value": 600, "provenance": "user"},
            }
        },
        [],
        [],
    )
    assert [gap["period"] for gap in result["gaps"]] == ["lunch", "dinner"]
    assert (
        len(result["gap_queries"]) == 1
        and result["gap_queries"][0]["purpose"] == "meal_gap"
    )


def test_candidate_and_gap_ids_are_stable_within_run_and_distinct_between_runs():
    extractions = [sample(), sample(source_id="note-b")]
    sources = [source(), source("note-b")]
    first = assemble_candidates("run-a", request(), {}, extractions, sources)
    repeat = assemble_candidates(
        "run-a", request(), {}, list(reversed(extractions)), list(reversed(sources))
    )
    other = assemble_candidates("run-b", request(), {}, extractions, sources)
    for key in ("candidates", "directions", "gaps"):
        first_ids = {row["id"] for row in first[key]}
        assert first_ids == {row["id"] for row in repeat[key]}
        assert first_ids.isdisjoint({row["id"] for row in other[key]})
        assert all(UUID(identifier).version == 5 for identifier in first_ids)
    assert first["candidates"][0]["mention_ids"] == ["mention-note-a", "mention-note-b"]
    assert "private-author-" not in json.dumps(first)


def test_single_text_supported_region_allows_two_queries_without_adopting_destination():
    result = assemble_candidates("run-a", request(), {}, [sample()], [source()])
    assert result["effective_request"]["destination"] is None
    assert len(result["gap_queries"]) == 2
    assert all(
        query["query"].startswith("杭州 西湖 ") for query in result["gap_queries"]
    )
    assert {query["purpose"] for query in result["gap_queries"]} == {
        "meal_gap",
        "lodging_gap",
    }
    assert all(
        "尚未由用户采用" in " ".join(gap["conditions"]) for gap in result["gaps"]
    )


@pytest.mark.parametrize(
    "context",
    ["unknown_city", "unsupported_city", "multi_city", "image_only", "generic"],
)
def test_unconfirmed_or_ambiguous_area_does_not_create_gap_queries(context):
    extractions, sources = [sample()], [source()]
    if context == "unknown_city":
        extractions[0]["mentions"][0]["city"] = None
    elif context == "unsupported_city":
        sources[0]["title"] = "西湖"
    elif context == "multi_city":
        extractions.append(sample(city="安吉", name="竹海", source_id="note-b"))
        sources.append(source("note-b", city="安吉", name="竹海"))
    elif context == "image_only":
        extractions[0] = sample(picture=True)
    else:
        extractions[0]["mentions"][0]["kind"] = "generic"
    result = assemble_candidates(
        "run-a",
        request(),
        {"intent": {"destination": {"value": "杭州", "provenance": "inferred"}}},
        extractions,
        sources,
    )
    assert result["gap_queries"] == []
    assert result["effective_request"]["destination"] is None


def test_image_uncertainty_and_source_chain_are_inherited_by_direction():
    result = assemble_candidates(
        "run-a", request(), {}, [sample(picture=True)], [source()]
    )
    candidate, direction = result["candidates"][0], result["directions"][0]
    assert (
        "图片待核验" in candidate["uncertainty"]
        and "图片待核验" in direction["uncertainty"]
    )
    assert direction["mention_ids"] == candidate["mention_ids"]
    assert direction["stats"]["author_count"] == 1
    assert (
        direction["stats"]["positive_count"]
        == direction["stats"]["negative_count"]
        == 0
    )


def test_image_evidence_alone_keeps_a_text_mentioned_direction_unverified():
    extracted = sample()
    extracted["evidence"] = [
        {
            "id": "image-evidence",
            "source_id": "note-a",
            "mention_id": "mention-note-a",
            "dimension": "图片开放时间",
            "quote": "西湖图片文字",
            "nature": "unknown",
            "stance": "unknown",
            "link_status": "uncertain",
            "citation_kind": "image",
            "image_index": 1,
        }
    ]
    result = assemble_candidates("run-a", request(), {}, [extracted], [source()])
    assert "图片待核验" in result["directions"][0]["uncertainty"]
    assert result["directions"][0]["evidence_ids"] == ["image-evidence"]
    assert result["candidates"][0]["stats"]["positive_count"] == 0


def test_attempted_failed_gap_searches_become_unfilled_and_are_not_queried_again():
    result = assemble_candidates(
        "run-a", request(destination="杭州"), {}, [], [], searched_purposes={"meal_gap"}
    )
    assert all(
        gap["status"] == "unfilled" and "已尝试餐饮补查" in gap["reason"]
        for gap in result["gaps"]
        if gap["kind"] == "meal"
    )
    assert result["gap_queries"] == [
        {"query": "杭州 附近住宿 民宿 酒店", "purpose": "lodging_gap"}
    ]
    both = assemble_candidates(
        "run-a",
        request(destination="杭州"),
        {},
        [],
        [],
        searched_purposes={"meal_gap", "lodging_gap"},
    )
    assert {gap["status"] for gap in both["gaps"]} == {"unfilled"}
    assert both["gap_queries"] == []


def test_existing_food_stays_unverified_after_search_and_keeps_its_source_refs():
    food = sample(name="豆浆店", category="eat")
    result = assemble_candidates(
        "run-a",
        request(destination="杭州"),
        {},
        [food],
        [source(name="豆浆店")],
        searched_purposes={"meal_gap"},
    )
    meals = [gap for gap in result["gaps"] if gap["kind"] == "meal"]
    assert all(gap["status"] == "undetermined" for gap in meals)
    assert all(gap["candidate_ids"] == [result["candidates"][0]["id"]] for gap in meals)
    assert all(gap["source_ids"] == ["note-a"] for gap in meals)
    assert not any(gap["status"] == "filled" for gap in result["gaps"])


def test_long_destination_does_not_create_unbounded_or_truncated_search_queries():
    result = assemble_candidates("run-a", request(destination="山" * 200), {}, [], [])
    assert result["gap_queries"] == []


def test_gap_search_purpose_is_validated():
    with pytest.raises(ValueError, match="补查目的"):
        assemble_candidates(
            "run-a",
            request(destination="杭州"),
            {},
            [],
            [],
            searched_purposes={"discovery"},
        )
