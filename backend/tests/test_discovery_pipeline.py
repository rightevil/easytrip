import asyncio
import json
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
            title = inputs["title"]
            place = title.removeprefix("杭州")
            category = (
                "eat" if "饭店" in place else "stay" if "宾馆" in place else "play"
            )
            data = {
                "mentions": [
                    {
                        "id": "m1",
                        "source_id": inputs["source_id"],
                        "name": place,
                        "city": "杭州",
                        "category": category,
                        "quote": title,
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
