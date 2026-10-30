import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_discovery_pipeline import setup

from easytrip.agent import content
from easytrip.agent.discovery import DiscoveryPaused
from easytrip.domain.models import MessageCreate, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.services.collection import CollectionService


@pytest.mark.parametrize("interrupt", [False, True])
def test_continuation_analyzes_saved_notes_before_any_new_search(
    tmp_path, monkeypatch, interrupt
):
    async def check():
        database, trip, runner, mcp, calls = setup(tmp_path, monkeypatch)

        async def pause(*args):
            raise DiscoveryPaused("model_result_unknown")

        runner.stages["extracting"] = pause
        database.enqueue_run(
            trip["id"],
            CreateRun(operation_id=uuid4(), expected_version=1, kind="discover", budget={"target_notes": 1}),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        previous = database.get_run(parent["id"])
        before = len(calls), len(mcp.calls)
        runner.stages.clear()
        original_extract = content.extract_note
        events = []

        async def extract(*args, **kwargs):
            events.append("analysis")
            if interrupt:
                raise DiscoveryPaused("model_result_unknown")
            return await original_extract(*args, **kwargs)

        monkeypatch.setattr(content, "extract_note", extract)
        responder = mcp.responder

        def search_after_analysis(name, arguments):
            if name == "search_feeds":
                assert events == ["analysis"]
                events.append("search")
                return {"feeds": [{"id": "meal", "xsecToken": "private-transport"}]}
            return responder(name, arguments)

        mcp.responder = search_after_analysis
        database.enqueue_run(
            trip["id"],
            CreateRun(operation_id=uuid4(), expected_version=1, kind="discover", parent_run_id=parent["id"], budget={"target_notes": 1}),
        )
        child = database.claim_run()
        await runner.execute(child)
        final = database.get_run(child["id"])
        assert final["checkpoint"]["understanding"] == previous["checkpoint"]["understanding"]
        assert database.get_run(parent["id"]) == previous
        if interrupt:
            assert events == ["analysis"]
            assert final["stop_reason"] == "model_result_unknown"
            assert (len(calls), len(mcp.calls)) == before
        else:
            assert events == ["analysis", "search", "analysis"]
            assert len(calls) == before[0] + 2
            assert final["usage"]["searches"] == 1
            assert final["usage"]["model_calls"] == 2
            assert final["usage"]["notes_obtained"] == 1
            assert final["result"]["coverage"]["notes_reused_analyzed"] == 1

    asyncio.run(check())


@pytest.mark.parametrize("changed", ["messages", "conditions", "configuration"])
def test_continuation_reinterprets_changed_user_input(tmp_path, monkeypatch, changed):
    async def check():
        database, trip, runner, _, calls = setup(tmp_path, monkeypatch)
        database.enqueue_run(
            trip["id"],
            CreateRun(operation_id=uuid4(), expected_version=1, kind="discover", budget={"target_notes": 1}),
        )
        parent = database.claim_run()
        await runner.execute(parent)
        before = len(calls)
        version = 1
        if changed == "messages":
            database.add_message(trip["id"], MessageCreate(operation_id=uuid4(), content="也想看看海边"))
        elif changed == "conditions":
            updated = database.update_trip(trip["id"], TripPatch(expected_version=1, request={**trip["request"], "preferences": "安静"}))
            version = updated["request_version"]
        else:
            runner.settings = replace(runner.settings, model_api_key="")
        database.enqueue_run(
            trip["id"],
            CreateRun(operation_id=uuid4(), expected_version=version, kind="discover", parent_run_id=parent["id"], budget={"target_notes": 1}),
        )
        child = database.claim_run()
        await runner.execute(child)
        actions = database.list_actions(child["id"])
        if changed == "configuration":
            assert database.get_run(child["id"])["stop_reason"] == "model_configuration_missing"
            assert len(calls) == before and not actions
            return
        assert len(calls) > before
        assert any(row["params"].get("task_type") == "understanding" for row in actions)
        if changed == "conditions":
            assert database.get_run(child["id"])["result"]["coverage"]["notes_reused"] == 0

    asyncio.run(check())


def test_collection_cutoff_keeps_saved_notes_and_leaves_time_for_analysis(
    tmp_path, monkeypatch
):
    async def check():
        database, trip, runner, _, _ = setup(tmp_path, monkeypatch)
        original_fetch = CollectionService.fetch

        async def slow_fetch(self, feeds, **kwargs):
            await original_fetch(self, feeds, **kwargs)
            await asyncio.sleep(10)

        monkeypatch.setattr(CollectionService, "fetch", slow_fetch)
        database.enqueue_run(
            trip["id"],
            CreateRun(operation_id=uuid4(), expected_version=1, kind="discover", budget={"max_seconds": 6, "target_notes": 1, "reserved_gap_searches": 0}),
        )
        run = database.claim_run()
        await runner.execute(run)
        final = database.get_run(run["id"])
        assert final["stage"] == "done"
        assert final["stop_reason"] == "collection_time_reserved"
        assert final["usage"]["notes_analyzed"] == 1
        assert final["usage"]["elapsed_seconds"] < 6
        assert database.list_candidates(trip["id"], run["id"])["total"] == 1

    asyncio.run(check())
