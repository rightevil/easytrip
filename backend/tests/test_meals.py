import asyncio
import sqlite3
from copy import deepcopy
from uuid import uuid4

import pytest

from easytrip.agent.discovery import DiscoveryRunner
from easytrip.config import Settings
from easytrip.domain.discovery import Evidence, SourceComment, SourceNote
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.integrations.amap import AmapBudgetExceeded
from easytrip.integrations.xhs import SourcePaused
from easytrip.services.meals import meal_queries, supplement_meals
from easytrip.storage.database import Database


def frozen_input():
    return {
        "request": {
            "destination": "杭州",
            "start_at": "2030-10-15T09:00:00+08:00",
            "duration_minutes": 600,
        },
        "input_hash": "f" * 64,
        "selected": [
            {
                "candidate_id": f"park-{index}",
                "name": name,
                "poi": {
                    "id": f"poi-{index}",
                    "name": name,
                    "city": "杭州市",
                    "location": [120.1, 30.2],
                    "read_at": "2030-10-14T09:00:00+08:00",
                    "api_version": "v5",
                },
            }
            for index, name in enumerate(("浙江省博物馆(孤山馆区)", "河坊街"), 1)
        ],
    }


def suggestions():
    return [
        {
            "payload": {
                "unknowns": [
                    {"code": "meal_gap", "period": period}
                    for period in ("lunch", "dinner")
                ],
                "itinerary": [
                    {
                        "kind": "activity",
                        "candidate_id": "park-1",
                        "start_at": "2030-10-15T09:30:00+08:00",
                        "end_at": "2030-10-15T12:00:00+08:00",
                    },
                    {
                        "kind": "activity",
                        "candidate_id": "park-2",
                        "start_at": "2030-10-15T16:00:00+08:00",
                        "end_at": "2030-10-15T18:00:00+08:00",
                    },
                ],
            }
        }
    ]


@pytest.fixture
def runtime(tmp_path):
    database = Database(tmp_path / "meals.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_planning(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="plan")
    )
    run = database.claim_run()
    settings = Settings(
        xhs_mcp="https://mcp.example/mcp",
        model_base_url="https://model.example/v1",
        model_api_key="test-only",
        model_id="agnes-test",
    )
    return DiscoveryRunner(database, settings), run


class MockAmap:
    def __init__(
        self, *, ambiguous=False, far=False, wrong_detail=False, exhausted=False
    ):
        self.calls = []
        self.ambiguous, self.far, self.wrong_detail, self.exhausted = (
            ambiguous,
            far,
            wrong_detail,
            exhausted,
        )

    def poi(self, name, identifier):
        return {
            "id": identifier,
            "name": name,
            "cityname": "杭州市",
            "type": "餐饮服务;中餐厅",
            "address": "孤山路测试地址",
            "location": "121,31" if self.far else "120.101,30.201",
        }

    async def search_candidates(self, name, city):
        self.calls.append(("search", name, city))
        if self.exhausted:
            raise AmapBudgetExceeded()
        pois = [self.poi(name, name)]
        if self.ambiguous:
            pois.append(self.poi(name, name + "other"))
        return {"status": "1", "search_complete": True, "pois": pois}

    async def detail(self, identifier):
        self.calls.append(("detail", identifier))
        return {
            "status": "1",
            "pois": [
                self.poi(identifier, "wrong-id" if self.wrong_detail else identifier)
            ],
        }


def install_sources(
    monkeypatch,
    *,
    paused=False,
    invalid_model=False,
    model_unknown=False,
    repair=False,
    comment_evidence=False,
    category="eat",
):
    calls = {"queries": [], "comment_limits": [], "extract_comments": [], "models": 0}

    class Collector:
        def __init__(self, runner, run, client):
            self.runner, self.run, self.budget = runner, run, run["budget"]

        async def discover(self, queries, *, gap, note_target):
            if paused:
                raise SourcePaused("test source unavailable")
            assert gap is True and note_target <= 2
            action = await self.runner.db(
                "begin_action",
                self.run["id"],
                self.run["generation"],
                f"search-{len(calls['queries'])}",
                "search",
                {"query": queries[0]["query"], "gap": True},
            )
            assert action is not None
            await self.runner.db("finish_action", action, status="completed", result={})
            calls["queries"].extend(queries)
            feeds = await self.runner.db(
                "get_run_item", self.run["id"], "collection:feeds", []
            )
            feeds.append(
                {
                    "external_id": f"meal-{len(feeds) + 1}",
                    "query": queries[0]["query"],
                    "purpose": "meal_gap",
                }
            )
            await self.runner.db(
                "save_run_item",
                self.run["id"],
                self.run["generation"],
                "collection:feeds",
                feeds,
            )
            return {"feeds": feeds}

        async def fetch(self, feeds, *, note_target):
            assert len(feeds) == 1 and note_target <= 2
            limit = self.budget["comment_limit"]
            calls["comment_limits"].append(limit)
            identity = feeds[0]["external_id"]
            name = f"{'酒店' if category == 'stay' else '餐馆'}{identity}(孤山店)"
            action = await self.runner.db(
                "begin_action",
                self.run["id"],
                self.run["generation"],
                "detail:" + identity,
                "detail",
                {},
            )
            assert action is not None
            await self.runner.db("finish_action", action, status="completed", result={})
            source = await self.runner.db(
                "save_source",
                self.run["trip_id"],
                self.run["id"],
                self.run["generation"],
                SourceNote(external_id=identity, title=name, text=name + "可以吃饭。"),
                query=feeds[0]["query"],
                purpose="meal_gap",
            )
            comments = []
            for index in range(limit):
                comment = await self.runner.db(
                    "save_source",
                    self.run["trip_id"],
                    self.run["id"],
                    self.run["generation"],
                    SourceComment(
                        external_id=identity + f"-c{index}",
                        parent_source_id=source["id"],
                        text="我去吃过。",
                    ),
                    query=feeds[0]["query"],
                    purpose="meal_gap",
                )
                comments.append(
                    {
                        "source_id": comment["id"],
                        "text": comment["text"],
                        "reply_to_source_id": None,
                    }
                )
            await self.runner.db(
                "save_run_item",
                self.run["id"],
                self.run["generation"],
                "note:" + identity,
                {
                    "status": "ready",
                    "note": {
                        "source_id": source["id"],
                        "title": name,
                        "text": source["text"],
                        "comments": comments,
                        "images": [],
                    },
                },
            )
            identities = await self.runner.db(
                "get_run_item", self.run["id"], "collection:notes", []
            )
            if identity not in identities:
                await self.runner.db(
                    "save_run_item",
                    self.run["id"],
                    self.run["generation"],
                    "collection:notes",
                    [*identities, identity],
                )
            current = await self.runner.db("get_run", self.run["id"])
            await self.runner.db(
                "set_discovery_progress",
                self.run["id"],
                self.run["generation"],
                {
                    "comments_selected": current["usage"].get("comments_selected", 0)
                    + len(comments)
                },
            )

    async def extract(client, settings, note, **kwargs):
        assert kwargs["max_images"] == kwargs["reply_limit"] == 0
        calls["extract_comments"].append(len(note["comments"]))
        source = note["source_id"]
        mention = {
            "id": source + "-mention",
            "source_id": source,
            "name": note["title"],
            "city": "杭州",
            "category": category,
            "quote": note["text"],
        }
        data = {
            "mentions": [mention],
            "activities": [
                {
                    "id": source + "-activity",
                    "mention_id": mention["id"],
                    "source_id": source,
                    "description": "可以吃饭",
                    "quote": note["text"],
                }
            ],
            "evidence": [],
            "image_observations": [],
        }
        if comment_evidence and note["comments"]:
            comment = note["comments"][0]
            data["evidence"] = [
                Evidence(
                    source_id=comment["source_id"],
                    mention_id=mention["id"],
                    object_text=note["title"],
                    dimension="到访",
                    stance="unknown",
                    nature="experience",
                    quote=comment["text"],
                    link_status="linked",
                    model_id=settings.model_id,
                ).model_dump(mode="json")
            ]
        if repair:
            first_action = await kwargs["before_request"](
                {"max_tokens": 32768, "images_submitted": 0}
            )
            if not first_action:
                return {
                    "status": "failed",
                    "requests": [],
                    "error": "request_not_allowed",
                }
            calls["models"] += 1
            await kwargs["after_request"](
                first_action,
                {
                    "status": "failed",
                    "model_data": None,
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                    "usage_complete": True,
                },
            )
        action = await kwargs["before_request"](
            {"max_tokens": 32768, "images_submitted": 0}
        )
        if not action:
            return {"status": "failed", "requests": [], "error": "request_not_allowed"}
        calls["models"] += 1
        record = {
            "status": "passed",
            "model_data": data,
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            "usage_complete": True,
        }
        if model_unknown:
            record.update(status="failed", result_unknown=True, model_data=None)
        await kwargs["after_request"](action, record)
        if invalid_model:
            return {"status": "failed", "requests": [], "error": "invalid"}
        return {
            "status": record["status"],
            "data": data,
            "submitted_images": [],
            "requests": [record],
        }

    monkeypatch.setattr("easytrip.services.meals.CollectionService", Collector)
    monkeypatch.setattr("easytrip.services.meals.extract_note", extract)
    return calls


def test_queries_bind_both_actual_meal_windows_and_confirmed_poi_areas():
    queries = meal_queries(frozen_input(), suggestions())
    assert len(queries) == 2
    assert "浙江省博物馆(孤山馆区)" in queries[0]["query"]
    assert "午餐 11:00-14:00" in queries[0]["query"]
    assert "河坊街" in queries[1]["query"]
    assert "晚餐 17:00-19:00" in queries[1]["query"]
    assert queries[0]["anchor_candidate_id"] == "park-1"
    assert queries[1]["anchor_candidate_id"] == "park-2"


def test_without_actual_time_or_place_no_generic_citywide_search():
    frozen = frozen_input()
    del frozen["request"]["start_at"]
    assert meal_queries(frozen, suggestions()) == []
    value = suggestions()
    value[0]["payload"]["itinerary"] = []
    assert meal_queries(frozen_input(), value) == []


def test_actual_gap_windows_and_progress_skip_past_meals():
    frozen = frozen_input()
    frozen["request"]["planning"] = {
        "progress_at": "2030-10-15T12:30:00+08:00",
        "progress_origin": {
            "name": "用户确认当前位置",
            "location": [120.1, 30.2],
            "source": "map",
        },
    }
    value = suggestions()
    value[0]["payload"]["unknowns"][0].update(
        window_start="2030-10-15T12:00:00+08:00", window_end="2030-10-15T13:30:00+08:00"
    )
    queries = meal_queries(frozen, value)
    assert "午餐 12:30-13:30" in queries[0]["query"]
    frozen["request"]["planning"]["progress_at"] = "2030-10-15T14:30:00+08:00"
    assert [query["period"] for query in meal_queries(frozen, value)] == ["dinner"]


def test_fills_two_source_backed_meals_without_changing_original_input(
    runtime, monkeypatch
):
    calls = install_sources(monkeypatch)
    runner, run = runtime
    frozen = frozen_input()
    original = deepcopy(frozen)

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen, suggestions(), MockAmap()
        )
        assert len(result["candidates"]) == 2, [
            (item["reason"], item["period"]) for item in result["gaps"]
        ]
        assert result["gaps"] == []
        assert calls["comment_limits"] == [5, 0]
        assert sum(calls["extract_comments"]) == 5
        assert calls["models"] == 2
        for candidate in result["candidates"]:
            assert candidate["category"] == "eat" and candidate["run_id"] == run["id"]
            assert (
                candidate["sources"]
                and candidate["mentions"]
                and candidate["activities"]
            )
            assert candidate["constraint"]["duration_source"] == "estimated"
            assert candidate["constraint"]["cost_cny"] is None
            assert (
                candidate["constraint"]["not_before"]
                == candidate["meal_window"]["start_at"]
            )
            saved = runner.database.list_candidates(run["trip_id"], run["id"])
            assert candidate["candidate_id"] in {item["id"] for item in saved["items"]}
        assert frozen == original
        again = await supplement_meals(
            runner, run, None, frozen, suggestions(), MockAmap(exhausted=True)
        )
        assert again == result
        assert calls["models"] == 2 and len(calls["queries"]) == 2

    asyncio.run(check())


def test_successful_meal_analysis_counts_evidence_and_cached_reuse_does_not_repeat(
    runtime, monkeypatch
):
    calls = install_sources(monkeypatch, comment_evidence=True)
    runner, run = runtime

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap()
        )
        assert len(result["candidates"]) == 2 and result["gaps"] == []
        usage = runner.database.get_run(run["id"])["usage"]
        assert {
            key: usage[key]
            for key in (
                "notes_analyzed",
                "comments_analyzed",
                "comments_judged",
                "evidence_count",
                "judgement_rows",
            )
        } == {
            "notes_analyzed": 2,
            "comments_analyzed": 1,
            "comments_judged": 1,
            "evidence_count": 1,
            "judgement_rows": 1,
        }
        evidence = result["candidates"][0]["evidence"][0]
        source = runner.database.get_source(
            run["trip_id"], evidence["source_id"], run["id"]
        )
        assert evidence["quote"] == source["text"] == "我去吃过。"
        counts = deepcopy(calls)
        # Recalculate a legacy cached result without resending its source or model requests.
        runner.database.set_discovery_progress(
            run["id"],
            run["generation"],
            {
                "notes_analyzed": 0,
                "comments_analyzed": 0,
                "comments_judged": 0,
                "evidence_count": 0,
                "judgement_rows": 0,
            },
        )
        cached = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap(exhausted=True)
        )
        assert cached == result and calls == counts
        assert runner.database.get_run(run["id"])["usage"] == usage

    asyncio.run(check())


@pytest.mark.parametrize(
    "amap", [MockAmap(ambiguous=True), MockAmap(far=True), MockAmap(wrong_detail=True)]
)
def test_ambiguous_outside_area_or_wrong_detail_never_enters_plan(
    runtime, monkeypatch, amap
):
    install_sources(monkeypatch)
    runner, run = runtime

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), amap
        )
        assert result["candidates"] == []
        assert len(result["gaps"]) == 2
        assert all(gap["reason"] == "poi_not_confirmed" for gap in result["gaps"])

    asyncio.run(check())


@pytest.mark.parametrize(
    "failure", ["paused", "invalid_model", "model_unknown", "route_budget"]
)
def test_failure_preserves_visible_meal_gaps(runtime, monkeypatch, failure):
    calls = install_sources(
        monkeypatch, **({failure: True} if failure != "route_budget" else {})
    )
    runner, run = runtime

    async def check():
        result = await supplement_meals(
            runner,
            run,
            None,
            frozen_input(),
            suggestions(),
            MockAmap(exhausted=failure == "route_budget"),
        )
        assert result["candidates"] == []
        assert len(result["gaps"]) == 2
        if failure in ("model_unknown", "route_budget"):
            assert calls["models"] == 1

    asyncio.run(check())


def test_dietary_restriction_is_not_proved_by_model_or_positive_notes(
    runtime, monkeypatch
):
    install_sources(monkeypatch)
    runner, run = runtime
    frozen = frozen_input()
    frozen["request"]["preferences"] = "花生过敏，忌口"

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen, suggestions(), MockAmap()
        )
        assert any(gap["code"] == "dietary_unverified" for gap in result["gaps"])
        assert all(
            "allergy_safe" not in candidate for candidate in result["candidates"]
        )

    asyncio.run(check())


def test_explicit_dietary_list_remains_unverified(runtime, monkeypatch):
    install_sources(monkeypatch)
    runner, run = runtime
    frozen = frozen_input()
    frozen["request"]["planning"] = {"dietary_restrictions": ["不含乳制品"]}

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen, suggestions(), MockAmap()
        )
        assert any(gap["code"] == "dietary_unverified" for gap in result["gaps"])
        assert all(
            not {"first_read_at", "last_read_at", "author_key"} & source.keys()
            for candidate in result["candidates"]
            for source in candidate["sources"]
        )

    asyncio.run(check())


def test_no_configuration_does_not_call_source(runtime, monkeypatch):
    calls = install_sources(monkeypatch)
    runner, run = runtime
    runner.settings.xhs_mcp = ""

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap()
        )
        assert len(result["gaps"]) == 2
        assert calls["queries"] == [] and calls["models"] == 0

    asyncio.run(check())


def test_two_note_repairs_are_bounded_to_four_physical_models(runtime, monkeypatch):
    calls = install_sources(monkeypatch, repair=True)
    runner, run = runtime

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap()
        )
        assert len(result["candidates"]) == 2
        assert calls["models"] == 4
        current = await runner.db("get_run", run["id"])
        assert current["usage"]["model_calls"] == 4

    asyncio.run(check())


def test_helper_honors_smaller_model_allowance_even_if_shared_ledger_has_room(
    runtime, monkeypatch
):
    calls = install_sources(monkeypatch)
    runner, run = runtime
    run["budget"]["max_model_calls"] = 1

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap()
        )
        assert len(result["candidates"]) == 1
        assert calls["models"] == 1
        assert result["gaps"][0]["period"] == "dinner"

    asyncio.run(check())


def test_cancel_propagates_without_another_source_or_model_attempt(
    runtime, monkeypatch
):
    runner, run = runtime
    calls = []

    class CancelledCollector:
        def __init__(self, runner, run, client):
            pass

        async def discover(self, *args, **kwargs):
            calls.append("search")
            raise asyncio.CancelledError()

    monkeypatch.setattr("easytrip.services.meals.CollectionService", CancelledCollector)

    async def check():
        with pytest.raises(asyncio.CancelledError):
            await supplement_meals(
                runner, run, None, frozen_input(), suggestions(), MockAmap()
            )
        assert calls == ["search"]

    asyncio.run(check())


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("external transport failed"),
        ExceptionGroup("MCP connection failed", [RuntimeError("server unavailable")]),
    ],
)
def test_connection_failure_keeps_gaps_and_stops_other_lookups(
    runtime, monkeypatch, error
):
    runner, run = runtime
    calls = []

    class BrokenCollector:
        def __init__(self, runner, run, client):
            pass

        async def discover(self, *args, **kwargs):
            calls.append("search")
            raise error

    monkeypatch.setattr("easytrip.services.meals.CollectionService", BrokenCollector)

    async def check():
        result = await supplement_meals(
            runner, run, None, frozen_input(), suggestions(), MockAmap()
        )
        assert len(result["gaps"]) == 2
        assert result["candidates"] == [] and calls == ["search"]
        assert "server unavailable" not in str(result)

    asyncio.run(check())


def test_storage_error_is_not_disguised_as_missing_restaurant(runtime, monkeypatch):
    runner, run = runtime

    class BrokenCollector:
        def __init__(self, runner, run, client):
            pass

        async def discover(self, *args, **kwargs):
            raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr("easytrip.services.meals.CollectionService", BrokenCollector)

    async def check():
        with pytest.raises(sqlite3.OperationalError):
            await supplement_meals(
                runner, run, None, frozen_input(), suggestions(), MockAmap()
            )

    asyncio.run(check())
