import asyncio
import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from test_collection import FakeMCP, comment, install, payload
from test_model_recovery import intent

from easytrip.agent import content
from easytrip.agent.discovery import DiscoveryRunner
from easytrip.config import Settings
from easytrip.domain.models import CreateTrip, TripRequest
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


def setup(
    tmp_path,
    monkeypatch,
    *,
    scheduled=False,
    fail_second=False,
    invalid_extraction=False,
    with_images=False,
    generic=False,
):
    database = Database(tmp_path / "pipeline.sqlite3")
    database.initialize()
    request = TripRequest(
        destination="杭州",
        start_at="2026-10-12T09:00:00+08:00" if scheduled else None,
        duration_minutes=1800 if scheduled else None,
    )
    trip = database.create_trip(CreateTrip(operation_id=uuid4(), request=request))

    def responder(name, arguments):
        if name == "check_login_status":
            return "已登录"
        if name == "search_feeds":
            query = arguments["keyword"]
            identity = (
                "meal" if "餐饮" in query else "stay" if "住宿" in query else "play"
            )
            return {"feeds": [{"id": identity, "xsecToken": "private-transport"}]}
        identity = arguments["feed_id"]
        place = {"play": "西湖", "meal": "阿婆饭店", "stay": "西湖宾馆"}[identity]
        if generic:
            place = "山脚民宿"
        return payload(
            [
                comment(identity + "-p", place + "体验很好"),
                comment(identity + "-n", place + "排队太久"),
            ],
            noteId=identity,
            title="杭州" + place,
            desc="杭州" + place + "旅行记录",
            imageList=[
                {"urlDefault": f"https://sns-img.xhscdn.com/{identity}-{index}.jpg"}
                for index in range(2)
            ]
            if with_images
            else [],
        )

    mcp = FakeMCP(responder)
    install(monkeypatch, mcp)
    requests = []

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["chat_template_kwargs"]["enable_thinking"] is True
        user = body["messages"][1]["content"]
        text = user[0]["text"] if isinstance(user, list) else user
        inputs = json.loads(text)
        if "request" in inputs:
            data = intent()
            data["query_plan"] = [{"query": "杭州一日游", "purpose": "main"}]
        else:
            if fail_second and len(requests) > 2:
                raise httpx.ReadTimeout("result unknown")
            title = "".join(line["text"] for line in inputs["title"])
            inputs["comments"] = [
                {**row, "text": "".join(line["text"] for line in row["text"])}
                for row in inputs["comments"]
            ]
            place = title.removeprefix("杭州")
            category = (
                "eat" if "饭店" in place else "stay" if "宾馆" in place else "play"
            )
            if generic:
                category = "stay"
            data = {
                "mentions": [
                    {
                        "id": "m1",
                        "source_id": inputs["source_id"],
                        "name": place,
                        "kind": "generic" if generic else "place",
                        "city": "杭州",
                        "category": category,
                        "quote": {
                            "part": "title",
                            "start_line": 1,
                            "end_line": len(inputs["title"]),
                        },
                    }
                ],
                "activities": [],
                "image_observations": [],
                "evidence": [
                    {
                        "source_id": row["source_id"],
                        "mention_id": "m1",
                        "object_text": place,
                        "dimension": "体验" if "很好" in row["text"] else "排队",
                        "stance": "positive" if "很好" in row["text"] else "negative",
                        "nature": "experience",
                        "quote": row["text"],
                        "link_status": "linked",
                    }
                    for row in inputs["comments"]
                ],
            }
            if invalid_extraction:
                data["mentions"][0]["quote"] = "不在任何原文中的伪造引用"
            if with_images and any(row.get("type") == "image_url" for row in user):
                data["image_observations"] = [
                    {
                        "source_id": inputs["source_id"],
                        "image_index": 1,
                        "text": "图片内容待核验",
                        "uncertain": True,
                    }
                ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(data, ensure_ascii=False)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            },
        )

    monkeypatch.setattr(
        "easytrip.agent.discovery.direct_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(model), trust_env=False
        ),
    )
    runner = DiscoveryRunner(
        database,
        Settings(
            xhs_mcp="http://example.invalid/mcp",
            model_base_url="https://model.invalid/v1",
            model_api_key="dummy",
            model_id="test-model",
        ),
    )
    return database, trip, runner, mcp, requests


def test_full_pipeline_keeps_real_counts_and_source_quotes(tmp_path, monkeypatch):
    async def check():
        database, trip, runner, mcp, calls = setup(tmp_path, monkeypatch)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["status"] == "completed" and final["stage"] == "done"
        assert len(calls) == final["usage"]["model_calls"] == 2
        assert (
            final["usage"]["comments_analyzed"]
            == final["usage"]["comments_judged"]
            == 2
        )
        assert final["usage"]["external_calls"] == len(mcp.calls) + len(calls) == 5
        candidate = next(
            row
            for row in database.list_candidates(trip["id"], run["id"])["items"]
            if row["kind"] == "place"
        )
        assert (
            candidate["stats"]["positive_count"]
            == candidate["stats"]["negative_count"]
            == 1
        )
        assert all(
            row["quote"] in row["source"]["text"]
            for row in database.candidate_evidence(trip["id"], candidate["id"])
        )
        assert final["result"]["model"]["semantic_quality"] == "not_validated"

    asyncio.run(check())


def test_generic_only_result_keeps_source_hints_and_reports_no_specific_place(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, _, calls = setup(tmp_path, monkeypatch, generic=True)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1, "reserved_gap_searches": 0},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        candidates = database.list_candidates(trip["id"], run["id"])["items"]
        assert len(calls) == 2 and len(candidates) == 1
        assert candidates[0]["kind"] == "generic"
        assert "no_source_backed_candidates" in final["result"]["partial_reasons"]
        assert "0个待定位地点、1条泛称线索、0个探索方向" in final["result"]["summary"]
        assert database.candidate_sources(trip["id"], candidates[0]["id"])

    asyncio.run(check())


def test_whole_trip_judgements_count_as_coverage_without_place_votes(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, mcp, _ = setup(tmp_path, monkeypatch)
        responder = mcp.responder

        def whole_trip_comments(name, arguments):
            if name == "get_feed_detail":
                return payload(
                    [
                        comment("trip-p", "整体行程体验很好"),
                        comment("trip-n", "整体行程排队太久"),
                    ],
                    noteId="play",
                    title="杭州西湖",
                    desc="杭州西湖旅行记录",
                    imageList=[],
                )
            return responder(name, arguments)

        mcp.responder = whole_trip_comments
        extract = content.extract_note

        async def unlinked_trip_evidence(*args, **kwargs):
            result = await extract(*args, **kwargs)
            for row in result["data"]["evidence"]:
                row.update(
                    mention_id=None, object_text="整体行程", link_status="unlinked"
                )
            return result

        monkeypatch.setattr(content, "extract_note", unlinked_trip_evidence)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["usage"]["comments_judged"] == 2
        candidates = database.list_candidates(trip["id"], run["id"])["items"]
        assert candidates and all(
            row["stats"]["positive_count"] == row["stats"]["negative_count"] == 0
            for row in candidates
        )

    asyncio.run(check())


@pytest.mark.parametrize(
    "fields, judged",
    [
        ({}, 2),
        ({"object_text": "未知对象"}, 0),
        ({"dimension": "unknown"}, 0),
        ({"link_status": "uncertain"}, 0),
    ],
    ids=["neutral-and-question", "unknown-object", "unknown-dimension", "uncertain"],
)
def test_main_metric_is_consistent_for_new_and_inherited_sources_without_votes(
    tmp_path, monkeypatch, fields, judged
):
    async def check():
        database, trip, runner, mcp, _ = setup(tmp_path, monkeypatch)
        responder = mcp.responder

        def fee_comments(name, arguments):
            if name == "get_feed_detail":
                return payload(
                    [
                        comment("fee", "西湖门票15元"),
                        comment("fee-question", "西湖门票免费吗？"),
                    ],
                    noteId="play",
                    title="杭州西湖",
                    desc="杭州西湖旅行记录",
                    imageList=[],
                )
            return responder(name, arguments)

        mcp.responder = fee_comments
        extract = content.extract_note

        async def classify_fee_evidence(*args, **kwargs):
            result = await extract(*args, **kwargs)
            for row in result["data"]["evidence"]:
                nature = "question" if "吗" in row["quote"] else "unknown"
                row.update(dimension="门票费用", nature=nature, stance=nature)
                row.update(fields)
            return result

        monkeypatch.setattr(content, "extract_note", classify_fee_evidence)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        parent_final = database.get_run(parent["id"])
        assert parent_final["usage"]["comments_analyzed"] == 2
        assert parent_final["usage"]["comments_judged"] == judged
        assert parent_final["usage"]["judgement_rows"] == 2
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        final = database.get_run(child["id"])
        assert (
            final["usage"]["comments_analyzed"]
            == final["usage"]["comments_judged"]
            == 0
        )
        assert final["result"]["coverage"]["comments_reused_analyzed"] == 2
        assert final["result"]["coverage"]["comments_reused_judged"] == judged
        assert database.get_run(parent["id"])["usage"]["comments_judged"] == judged
        for run_id in (parent["id"], child["id"]):
            candidates = database.list_candidates(trip["id"], run_id)["items"]
            assert candidates and all(
                row["stats"]["positive_count"] == row["stats"]["negative_count"] == 0
                for row in candidates
            )

    asyncio.run(check())


@pytest.mark.parametrize("fail_second", [False, True])
def test_supplement_is_finite_and_unknown_preserves_prior_drafts(
    tmp_path, monkeypatch, fail_second
):
    async def check():
        database, trip, runner, _, calls = setup(
            tmp_path, monkeypatch, scheduled=True, fail_second=fail_second
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 4},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["usage"]["searches"] == 3
        candidates = database.list_candidates(trip["id"], run["id"])["items"]
        assert any(row["name"] == "西湖" for row in candidates)
        assert final["result"] is not None
        if fail_second:
            assert (
                final["status"] == "paused"
                and final["stop_reason"] == "model_result_unknown"
            )
            assert final["usage"]["unknown_cost_calls"] == 1 and len(calls) == 3
        else:
            assert final["status"] == "partial" and final["stage"] == "done"
            assert {row["category"] for row in candidates} == {"eat", "play", "stay"}
            assert all(
                row["status"] == "undetermined"
                for row in database.list_gaps(trip["id"], run["id"])
            )
            assert len(calls) == 4

    asyncio.run(check())


def test_continuation_reuses_analysis_without_new_detail_or_image_fee(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, mcp, calls = setup(tmp_path, monkeypatch)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        previous = (
            len(calls),
            len([item for item in mcp.calls if item[0] == "get_feed_detail"]),
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        final = database.get_run(child["id"])
        assert len(calls) == previous[0] + 1
        assert (
            len([item for item in mcp.calls if item[0] == "get_feed_detail"])
            == previous[1]
        )
        assert final["usage"]["notes_obtained"] == final["usage"]["images"] == 0
        assert final["result"]["coverage"]["notes_reused"] == 1
        assert final["result"]["coverage"]["notes_reused_analyzed"] == 1
        assert final["result"]["coverage"]["comments_reused_analyzed"] == 2
        assert final["result"]["coverage"]["comments_reused_judged"] == 2
        assert database.list_candidates(trip["id"], child["id"])["total"] > 0
        assert final["usage"]["model_calls"] == 1
        assert database.trip_usage(trip["id"])["model_tokens"] == 90

    asyncio.run(check())


@pytest.mark.parametrize("analyze_inherited", [False, True])
def test_inherited_coverage_shows_sources_before_and_after_analysis(
    tmp_path, monkeypatch, analyze_inherited
):
    async def check():
        database, trip, runner, _, calls = setup(
            tmp_path, monkeypatch, with_images=True
        )
        original = content.extract_note

        async def invalid(*args, **kwargs):
            return {
                "status": "failed",
                "error": "invalid schema",
                "requests": [],
                "coverage": {},
            }

        monkeypatch.setattr(content, "extract_note", invalid)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        if analyze_inherited:
            monkeypatch.setattr(content, "extract_note", original)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        final = database.get_run(child["id"])
        coverage = final["result"]["coverage"]
        assert coverage["notes_reused"] == 1 and coverage["comments_reused"] == 2
        assert coverage["notes_reused_analyzed"] == int(analyze_inherited)
        assert (
            coverage["comments_reused_analyzed"]
            == coverage["comments_reused_judged"]
            == 2 * int(analyze_inherited)
        )
        assert coverage["images_reused_obtained"] == 2
        assert coverage["images_reused_analyzed_unique"] == int(analyze_inherited)
        assert coverage["images_reused_unanalyzed"] == 2 - int(analyze_inherited)
        assert final["usage"]["notes_obtained"] == final["usage"]["notes_analyzed"] == 0
        assert (
            final["usage"]["comments_selected"]
            == final["usage"]["comments_analyzed"]
            == 0
        )
        assert (
            final["usage"]["images_obtained"]
            == final["usage"]["images_analyzed_unique"]
            == 0
        )
        assert final["usage"]["model_calls"] == 1 + int(analyze_inherited)
        assert final["usage"]["images"] == 2 * int(analyze_inherited)
        assert len(calls) == 2 + int(analyze_inherited)
        assert (
            "comments_partly_unanalysed" in final["result"]["partial_reasons"]
        ) is not analyze_inherited

    asyncio.run(check())


def resume(database, run):
    current = database.get_run(run["id"])
    database.resume_run(run["id"], run["trip_id"], current["generation"])
    return database.claim_run()


@pytest.mark.parametrize("interrupt_at", ["analysis", "search_checkpoint"])
def test_supplement_resume_restores_completed_search_and_model_ledgers(
    tmp_path, monkeypatch, interrupt_at
):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, scheduled=True
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 4},
            ),
        )
        run = database.claim_run()
        original = database.save_run_item
        interrupted = False

        def interrupt(run_id, generation, key, value):
            nonlocal interrupted
            matched = (
                interrupt_at == "analysis"
                and key.startswith("extract:")
                and value.get("status") == "passed"
                and value["data"]["mentions"][0]["category"] == "stay"
            ) or (
                interrupt_at == "search_checkpoint"
                and key == "collection:feeds"
                and any(row["external_id"] == "stay" for row in value)
            )
            if matched and not interrupted:
                interrupted = True
                raise asyncio.CancelledError()
            return original(run_id, generation, key, value)

        monkeypatch.setattr(database, "save_run_item", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await runner.execute(run)
        plan = database.get_run_item(run["id"], "gaps:plan")
        assert {row["purpose"] for row in plan} == {"meal_gap", "lodging_gap"}
        assert database.get_run_item(run["id"], "gaps:searched") == [
            "meal_gap",
            "lodging_gap",
        ]
        before = len(calls), len(mcp.calls)
        database.recover_runs(graceful=True)
        await runner.execute(resume(database, run))
        final = database.get_run(run["id"])
        assert final["stage"] == "done" and final["status"] == "partial"
        assert len(calls) == final["usage"]["model_calls"] == 4
        assert final["usage"]["searches"] == 3
        assert final["usage"]["comments_analyzed"] == 6
        assert {
            row["category"]
            for row in database.list_candidates(trip["id"], run["id"])["items"]
        } == {"eat", "play", "stay"}
        assert database.get_run_item(run["id"], "gaps:plan") == plan
        assert "gap_search_unfinished" not in final["result"]["partial_reasons"]
        if interrupt_at == "analysis":
            assert (len(calls), len(mcp.calls)) == before
        else:
            assert len([row for row in mcp.calls if row[0] == "search_feeds"]) == 3

    asyncio.run(check())


def test_supplement_unknown_search_never_spends_a_second_round_on_resume(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, scheduled=True
        )
        original = mcp.responder

        def fail_meal(name, arguments):
            if name == "search_feeds" and "餐饮" in arguments["keyword"]:
                return TimeoutError("result unavailable")
            return original(name, arguments)

        mcp.responder = fail_meal
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 4},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        paused = database.get_run(run["id"])
        assert (
            paused["status"] == "paused"
            and paused["stop_reason"] == "source_result_unknown"
        )
        plan = database.get_run_item(run["id"], "gaps:plan")
        assert database.get_run_item(run["id"], "gaps:searched") == ["meal_gap"]
        assert "gap_search_unfinished" in paused["result"]["partial_reasons"]
        mcp.responder = original
        original_assembled = content.assembled

        async def changed_gap_hint(*args):
            assembled = await original_assembled(*args)
            # Newly assembled hints must not replace the already persisted query plan.
            assembled["gap_queries"] = [
                {"query": "杭州 新区域 附近餐饮", "purpose": "meal_gap"}
            ]
            return assembled

        monkeypatch.setattr(content, "assembled", changed_gap_hint)
        await runner.execute(resume(database, run))
        final = database.get_run(run["id"])
        searches = [row[1]["keyword"] for row in mcp.calls if row[0] == "search_feeds"]
        assert searches == ["杭州一日游", plan[0]["query"], plan[1]["query"]]
        assert final["usage"]["searches"] == 3
        assert (
            sum(
                row["kind"] == "search" and row["status"] == "unknown"
                for row in database.list_actions(run["id"])
            )
            == 1
        )
        assert len(calls) == 3
        assert database.get_run_item(run["id"], "gaps:plan") == plan
        assert "gap_search_unfinished" in final["result"]["partial_reasons"]
        assert any(
            row["kind"] == "meal" and row["status"] == "unfilled"
            for row in final["result"]["gaps"]
        )
        assert {
            row["category"]
            for row in database.list_candidates(trip["id"], run["id"])["items"]
        } == {"play", "stay"}

    asyncio.run(check())


def test_known_invalid_extraction_is_reported_without_automatic_recharge(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, _, calls = setup(
            tmp_path, monkeypatch, invalid_extraction=True
        )
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["status"] == "partial" and final["stage"] == "done"
        assert "model_output_invalid" in final["result"]["partial_reasons"]
        assert final["usage"]["notes_analyzed"] == 0
        assert (
            len(calls) == 3
        )  # One intent request, one extraction, one bounded text repair.
        note = database.get_run_item(run["id"], "note:play")["note"]
        extraction_actions = [
            row
            for row in database.list_actions(run["id"])
            if row["action_key"].startswith("extract:")
        ]
        assert len(extraction_actions) == 2
        assert all(
            row["params"]["input_hash"] == content.note_hash(note)
            for row in extraction_actions
        )
        assert all(
            row["params"]["model_id"] == runner.settings.model_id
            for row in extraction_actions
        )
        assert database.list_candidates(trip["id"], run["id"])["total"] == 0

    asyncio.run(check())


def test_three_rounds_reuse_nearest_successful_analysis_of_inherited_sources(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, mcp, calls = setup(tmp_path, monkeypatch)
        original_extract = content.extract_note

        async def invalid_first_round(*args, **kwargs):
            return {
                "status": "failed",
                "error": "invalid schema",
                "requests": [],
                "coverage": {},
            }

        monkeypatch.setattr(content, "extract_note", invalid_first_round)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        first = database.claim_run()
        await runner.execute(first)
        assert database.get_run(first["id"])["result"]["coverage"]["notes_reused"] == 0
        monkeypatch.setattr(content, "extract_note", original_extract)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=first["id"],
                budget={"target_notes": 1},
            ),
        )
        second = database.claim_run()
        await runner.execute(second)
        assert database.get_run_item(second["id"], "collection:notes", []) == []
        source = database.get_run_item(first["id"], "note:play")["note"]["source_id"]
        assert (
            database.get_run_item(first["id"], "extract:" + source)["status"]
            == "failed"
        )
        assert (
            database.get_run_item(second["id"], "extract:" + source)["status"]
            == "passed"
        )
        before = len(calls), len(mcp.calls)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=second["id"],
                budget={"target_notes": 1},
            ),
        )
        third = database.claim_run()
        await runner.execute(third)
        final = database.get_run(third["id"])
        assert (
            len(calls) == before[0] + 1
        )  # Only the new round's understanding request.
        assert len([row for row in mcp.calls if row[0] == "get_feed_detail"]) == 1
        assert final["usage"]["model_calls"] == 1 and final["usage"]["images"] == 0
        assert (
            database.get_run_item(third["id"], "collection:inherited")[0]["run_id"]
            == first["id"]
        )
        assert (
            database.get_run_item(third["id"], "extract:" + source)["reused_from_run"]
            == second["id"]
        )
        assert final["result"]["coverage"]["notes_reused"] == 1
        assert database.list_candidates(trip["id"], third["id"])["total"] > 0

    asyncio.run(check())


@pytest.mark.parametrize("old_contract", ["legacy", "older_version"])
def test_new_contract_reanalyses_old_parent_then_reuses_nearest_compatible_result(
    tmp_path, monkeypatch, old_contract
):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, with_images=True
        )
        contract = content.EXTRACTION_CONTRACT
        original_extract = content.extract_note
        original_save = database.save_run_item
        if old_contract == "older_version":
            monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "old-prompt")

        async def old_objects_missing(*args, **kwargs):
            result = await original_extract(*args, **kwargs)
            for row in result["data"]["evidence"]:
                row["object_text"] = ""
                row["link_status"] = "uncertain"
            return result

        def legacy_save(run_id, generation, key, value):
            if (
                old_contract == "legacy"
                and key.startswith("extract:")
                and value.get("status") == "passed"
            ):
                note = database.get_run_item(run_id, "note:play")["note"]
                digest = hashlib.sha256(
                    json.dumps(note, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                value = {**value, "note_hash": digest}
            return original_save(run_id, generation, key, value)

        monkeypatch.setattr(content, "extract_note", old_objects_missing)
        monkeypatch.setattr(database, "save_run_item", legacy_save)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        note = database.get_run_item(parent["id"], "note:play")["note"]
        key = "extract:" + note["source_id"]
        old_result = database.get_run_item(parent["id"], key)
        assert old_result["status"] == "passed"
        assert all(not row["object_text"] for row in old_result["data"]["evidence"])
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", contract)
        monkeypatch.setattr(content, "extract_note", original_extract)
        monkeypatch.setattr(database, "save_run_item", original_save)
        assert old_result["note_hash"] != content.note_hash(note)
        before = len(calls), len(mcp.calls)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        revised = database.get_run_item(child["id"], key)
        assert revised["note_hash"] == content.note_hash(note)
        assert all(row["object_text"] == "西湖" for row in revised["data"]["evidence"])
        assert len(calls) == before[0] + 2
        assert database.get_run(child["id"])["usage"]["images"] == 2
        assert database.get_run_item(parent["id"], key) == old_result
        assert len([row for row in mcp.calls if row[0] == "get_feed_detail"]) == 1
        before = len(calls), len(mcp.calls)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=child["id"],
                budget={"target_notes": 1},
            ),
        )
        grandchild = database.claim_run()
        await runner.execute(grandchild)
        reused = database.get_run_item(grandchild["id"], key)
        assert reused["reused_from_run"] == child["id"]
        assert reused["note_hash"] == revised["note_hash"]
        assert len(calls) == before[0] + 1
        assert database.get_run(grandchild["id"])["usage"]["images"] == 0
        assert len([row for row in mcp.calls if row[0] == "get_feed_detail"]) == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "recorded_contract",
    ["legacy", "legacy_model", "older_version", "current", "model_changed"],
)
def test_successful_action_recovery_preserves_its_physical_input_hash(
    tmp_path, monkeypatch, recorded_contract
):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, with_images=True
        )
        contract = content.EXTRACTION_CONTRACT
        if recorded_contract == "older_version":
            monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "old-prompt")
        if recorded_contract == "model_changed":
            runner.settings = replace(runner.settings, model_id="model-a")
        original_begin = database.begin_action
        original_save = database.save_run_item

        def record_legacy(run_id, generation, key, kind, params):
            if recorded_contract in {"legacy", "legacy_model"} and key.startswith(
                "extract:"
            ):
                missing = {"model_id"}
                if recorded_contract == "legacy":
                    missing.add("input_hash")
                params = {
                    name: value for name, value in params.items() if name not in missing
                }
            return original_begin(run_id, generation, key, kind, params)

        def interrupt_checkpoint(run_id, generation, key, value):
            if key.startswith("extract:") and value.get("status") == "passed":
                raise asyncio.CancelledError()
            return original_save(run_id, generation, key, value)

        monkeypatch.setattr(database, "begin_action", record_legacy)
        monkeypatch.setattr(database, "save_run_item", interrupt_checkpoint)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        with pytest.raises(asyncio.CancelledError):
            await runner.execute(parent)
        note = database.get_run_item(parent["id"], "note:play")["note"]
        key = "extract:" + note["source_id"]
        action = next(
            row
            for row in database.list_actions(parent["id"])
            if row["action_key"].startswith(key + ":model:")
        )
        assert action["status"] == "completed"
        recorded_hash = action["params"].get("input_hash")
        assert (recorded_hash is None) is (recorded_contract == "legacy")
        recorded_model = action["params"].get("model_id")
        assert (recorded_model is None) is (
            recorded_contract in {"legacy", "legacy_model"}
        )
        if recorded_contract == "model_changed":
            assert recorded_model == "model-a"
            assert all(request["model"] == "model-a" for request in calls)
            runner.settings = replace(runner.settings, model_id="model-b")
        assert database.get_run_item(parent["id"], key) is None
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", contract)
        monkeypatch.setattr(database, "begin_action", original_begin)
        monkeypatch.setattr(database, "save_run_item", original_save)
        before = len(calls), len(mcp.calls)
        usage = database.get_run(parent["id"])["usage"]
        database.recover_runs(graceful=True)
        await runner.execute(resume(database, parent))
        restored = database.get_run_item(parent["id"], key)
        assert restored["note_hash"] == recorded_hash
        assert restored["model_id"] == recorded_model
        if recorded_contract == "model_changed":
            assert all(
                row["model_id"] == "model-a" for row in restored["data"]["evidence"]
            )
        assert restored["submitted_images"] == [
            {"source_id": note["source_id"], "image_index": 1},
            {"source_id": note["source_id"], "image_index": 2},
        ]
        assert (len(calls), len(mcp.calls)) == before
        final = database.get_run(parent["id"])
        assert final["status"] == "completed"
        assert final["usage"]["model_calls"] == usage["model_calls"] == 2
        assert final["usage"]["model_tokens"] == usage["model_tokens"] == 60
        assert final["usage"]["images"] == usage["images"] == 2
        if recorded_contract in {"legacy", "older_version"}:
            assert restored["note_hash"] != content.note_hash(note)
        else:
            assert restored["note_hash"] == content.note_hash(note)
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        child_result = database.get_run_item(child["id"], key)
        assert child_result["note_hash"] == content.note_hash(note)
        assert child_result["model_id"] == runner.settings.model_id
        assert all(
            row["model_id"] == runner.settings.model_id
            for row in child_result["data"]["evidence"]
        )
        if recorded_contract == "model_changed":
            assert all(request["model"] == "model-b" for request in calls[before[0] :])
        assert len(calls) == before[0] + (1 if recorded_contract == "current" else 2)
        assert database.get_run(child["id"])["usage"]["images"] == (
            0 if recorded_contract == "current" else 2
        )
        assert len([row for row in mcp.calls if row[0] == "get_feed_detail"]) == 1

    asyncio.run(check())


def test_contract_update_does_not_replay_unknown_action_in_same_run(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, scheduled=True, fail_second=True, with_images=True
        )
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "old-prompt")
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 4},
            ),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert (
            final["status"] == "paused"
            and final["stop_reason"] == "model_result_unknown"
        )
        unknown = [
            row
            for row in database.list_actions(run["id"])
            if row["status"] == "unknown"
        ]
        assert len(unknown) == 1
        before = len(calls), len(mcp.calls)
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "new-prompt")
        await runner.execute(resume(database, run))
        resumed = database.get_run(run["id"])
        assert (
            resumed["status"] == "paused"
            and resumed["stop_reason"] == "model_retry_requires_new_run"
        )
        assert (len(calls), len(mcp.calls)) == before
        assert database.list_actions(run["id"])[-1]["id"] == unknown[0]["id"]
        for key in (
            "model_calls",
            "model_tokens",
            "images",
            "reserved_tokens",
            "unknown_result_calls",
        ):
            assert resumed["usage"][key] == final["usage"][key]

    asyncio.run(check())


def test_new_contract_cache_miss_still_obeys_child_call_budget(tmp_path, monkeypatch):
    async def check():
        database, trip, runner, mcp, calls = setup(
            tmp_path, monkeypatch, with_images=True
        )
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "old-prompt")
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                budget={"target_notes": 1},
            ),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        monkeypatch.setattr(content, "EXTRACTION_CONTRACT", "new-prompt")
        database.enqueue_run(
            trip["id"],
            CreateRun(
                operation_id=uuid4(),
                expected_version=1,
                kind="discover",
                parent_run_id=parent["id"],
                budget={"target_notes": 1, "max_model_calls": 1},
            ),
        )
        child = database.claim_run()
        await runner.execute(child)
        final = database.get_run(child["id"])
        assert final["status"] == "partial"
        assert final["stop_reason"] == "model_budget_exhausted"
        assert final["usage"]["model_calls"] == 1 and final["usage"]["images"] == 0
        assert (
            final["usage"]["model_tokens"] == 30
            and final["usage"]["reserved_tokens"] == 0
        )
        assert len(calls) == 3
        assert final["result"]["coverage"]["notes_reused_analyzed"] == 0
        assert len([row for row in mcp.calls if row[0] == "get_feed_detail"]) == 1

    asyncio.run(check())
