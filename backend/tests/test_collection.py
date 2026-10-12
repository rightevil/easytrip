import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from easytrip.agent.discovery import DiscoveryRunner
from easytrip.config import Settings
from easytrip.domain.discovery_tasks import DiscoveryBudget
from easytrip.domain.models import CreateTrip, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.services.collection import (
    CollectionService,
    normalize_detail,
    normalize_search,
)
from easytrip.storage.database import Database


def payload(comments=None, **note):
    return {
        "data": {
            "note": {
                "noteId": "real-note-id",
                "title": "西湖攻略",
                "desc": "走湖边，再去吃面",
                **note,
            },
            "comments": {"list": comments or [], "hasMore": True},
        }
    }


def comment(identity, text="很好玩", **extra):
    return {
        "id": identity,
        "content": text,
        "user": {"userId": "actual-author-id", "nickname": "不应导出昵称"},
        **extra,
    }


def test_search_preserves_real_ids_separates_handles_and_hashes_authors():
    settings = Settings(secrets={"known-secret"})
    raw = {
        "feeds": [
            {
                "id": "note-real",
                "xsecToken": "private-handle",
                "noteCard": {
                    "displayTitle": "西湖 known-secret 13800138000",
                    "user": {"userId": "author-real", "nickname": "private"},
                },
            },
            {"id": "note-real", "xsecToken": "another-handle"},
            {
                "id": "note-two",
                "url": "https://www.xiaohongshu.com/explore/note-two?xsec_token=url-private",
            },
            {"title": "没有ID的搜索卡"},
            None,
        ]
    }
    normalized = normalize_search(raw, settings)
    assert [item["external_id"] for item in normalized["feeds"]] == [
        "note-real",
        "note-two",
    ]
    assert normalized["handles"] == {
        "note-real": {"xsec_token": "private-handle"},
        "note-two": {"xsec_token": "url-private"},
    }
    assert normalized["feeds"][0]["title"] == "西湖 [REDACTED] [PHONE]"
    assert (
        normalized["feeds"][0]["author_key"]
        == hashlib.sha256(b"xiaohongshu:author:author-real").hexdigest()
    )
    assert "private-handle" not in str(normalized["feeds"])
    assert "private" not in str(normalized["feeds"])
    assert normalized["coverage"] == {
        "returned": 5,
        "unique": 2,
        "duplicates": 1,
        "missing_ids": 2,
    }


def test_detail_first_level_default_cap_and_no_invented_comment_identity():
    first = [comment(f"real-comment-{i}", f"正文{i}") for i in range(25)]
    first[0]["subComments"] = [comment("real-reply", "回复")]
    first += [
        first[0],
        {"content": "缺ID评论"},
        {"content": "缺ID评论"},
        {"content": ""},
    ]
    normalized = normalize_detail(payload(first), Settings())
    assert normalized["note"]["external_id"] == "real-note-id"
    assert [item["external_id"] for item in normalized["comments"]] == [
        f"real-comment-{i}" for i in range(20)
    ]
    assert all(item["reply_to_external_id"] is None for item in normalized["comments"])
    assert normalized["coverage"]["first_level_unique"] == 26
    assert normalized["coverage"]["first_level_selected"] == 20
    assert normalized["coverage"]["replies_selected"] == 0
    assert normalized["coverage"]["has_more_comments"] is True
    expanded = normalize_detail(payload(first), Settings(), comment_limit=50)
    assert expanded["comments"][-1]["external_id"] is None
    assert all(
        "nickname" not in item and "user" not in item for item in expanded["comments"]
    )


def test_replies_are_explicit_bounded_and_preserve_real_parent_context():
    first = [
        comment(
            "parent-1",
            subComments=[comment(f"reply-{i}", f"答复{i}") for i in range(15)],
        ),
        comment(
            "parent-2",
            subComments=[
                comment(f"reply-{i + 15}", f"答复{i + 15}") for i in range(15)
            ],
        ),
        {"content": "缺ID父评论", "subComments": [comment("orphan", "无法安全关联")]},
    ]
    result = normalize_detail(payload(first), Settings(), include_replies=True)
    replies = [
        item for item in result["comments"] if item["reply_to_external_id"] is not None
    ]
    assert len(replies) == 20
    assert [item["reply_to_external_id"] for item in replies] == ["parent-1"] * 15 + [
        "parent-2"
    ] * 5
    assert result["coverage"]["replies_received"] == 31
    assert result["coverage"]["replies_skipped_missing_context"] == 1
    assert "orphan" not in str(result["comments"])
    rest = normalize_detail(
        payload(first), Settings(), include_replies=True, reply_limit=3
    )
    assert rest["coverage"]["replies_selected"] == 3


def test_time_is_aware_or_explicitly_unknown_and_text_is_redacted():
    raw = payload(
        [
            comment(
                "c",
                "联系13800138000 me@example.test https://example.test/xsec?token=signed",
                createTime=1_728_000_000_000,
            )
        ],
        time=1_728_000_000_000,
        desc="known-secret 我的电话13800138000",
    )
    result = normalize_detail(raw, Settings(secrets={"known-secret"}))
    note_time = datetime.fromisoformat(result["note"]["published_at"])
    assert note_time.utcoffset() is not None
    assert datetime.fromisoformat(result["comments"][0]["published_at"]) == note_time
    assert result["comments"][0]["text"] == "联系[PHONE] [EMAIL] [URL]"
    assert result["note"]["text"] == "[REDACTED] 我的电话[PHONE]"
    for value in (None, "昨天", "2026-10-12T08:00:00", 0, True, float("inf")):
        assert (
            normalize_detail(payload(time=value), Settings())["note"]["published_at"]
            is None
        )


def test_only_safe_cdn_images_remain_and_video_is_marked_unanalysed():
    raw = payload(
        imageList=[
            {
                "urlDefault": "http://sns.xhscdn.com/one.jpg",
                "urlPre": "https://sns.xhscdn.com/preview.jpg",
            },
            {"urlDefault": "https://evil.test/a.jpg"},
            {"urlDefault": "https://sns.xhscdn.com/signed.jpg?xsec_token=secret"},
            {"urlDefault": "https://user:secret@sns.xhscdn.com/private.jpg"},
            {"urlDefault": "https://sns.xhscdn.com/ok.jpg?format=webp"},
        ],
        type="video",
    )
    result = normalize_detail(raw, Settings())
    assert result["note"]["images"] == [
        "https://sns.xhscdn.com/one.jpg",
        "",
        "",
        "",
        "https://sns.xhscdn.com/ok.jpg?format=webp",
    ]
    assert result["coverage"]["images_received"] == 5
    assert result["coverage"]["images_retained"] == 2
    assert result["video_unanalysed"] is True


def test_image_position_survives_filtering_and_safe_image_keeps_original_index():
    result = normalize_detail(
        payload(
            imageList=[
                {"urlDefault": "https://unsafe.test/first.jpg"},
                {"urlDefault": "https://sns.xhscdn.com/second.jpg"},
            ]
        ),
        Settings(),
    )
    assert result["note"]["images"] == ["", "https://sns.xhscdn.com/second.jpg"]
    actual_submitted = [
        {"image_index": index, "url": image}
        for index, image in enumerate(result["note"]["images"], 1)
        if image
    ]
    assert actual_submitted == [
        {"image_index": 2, "url": "https://sns.xhscdn.com/second.jpg"}
    ]


def test_image_position_cap_keeps_raw_received_count_explicit():
    result = normalize_detail(
        payload(
            imageList=[
                {"urlDefault": f"https://sns.xhscdn.com/{i}.jpg"} for i in range(105)
            ]
        ),
        Settings(),
    )
    assert len(result["note"]["images"]) == 100
    assert result["coverage"]["images_received"] == 105
    assert (
        result["coverage"]["images_retained"]
        == result["coverage"]["image_slots_retained"]
        == 100
    )
    assert result["coverage"]["images_omitted_over_limit"] == 5
    assert result["note"]["completeness"] == "partial"


def test_note_identity_checks_and_missing_ids_remain_none():
    with pytest.raises(ValueError):
        normalize_detail(payload(), Settings(), external_id="different-note")
    raw = payload()
    del raw["data"]["note"]["noteId"]
    assert normalize_detail(raw, Settings())["note"]["external_id"] is None
    assert (
        normalize_detail(raw, Settings(), external_id="requested-real-id")["note"][
            "external_id"
        ]
        == "requested-real-id"
    )


def test_detail_text_caps_mark_partial_instead_of_claiming_full_content():
    result = normalize_detail(payload(desc="长" * 30001, title="题" * 501), Settings())
    assert len(result["note"]["text"]) == 30000
    assert len(result["note"]["title"]) == 500
    assert result["note"]["completeness"] == "partial"
    assert result["coverage"]["text_truncated"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "  ", "desc": "\n\t"},
        {"title": 123, "desc": []},
        {"title": None, "desc": ""},
    ],
)
def test_detail_rejects_no_analyzable_title_or_body(fields):
    with pytest.raises(ValueError):
        normalize_detail(payload(**fields), Settings())


@pytest.mark.parametrize(
    "fields", [{"title": "西湖", "desc": ""}, {"title": "", "desc": "西湖散步"}]
)
def test_title_only_or_body_only_still_preserves_real_content(fields):
    note = normalize_detail(payload(**fields), Settings())["note"]
    assert note["title"] == fields["title"] and note["text"] == fields["desc"]


@pytest.mark.parametrize(
    "bad",
    [{}, {"data": None}, {"data": {"note": []}}, {"data": {"note": {"title": ""}}}],
)
def test_malformed_detail_fails_safely(bad):
    with pytest.raises(ValueError):
        normalize_detail(bad, Settings())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"comment_limit": True},
        {"comment_limit": 51},
        {"reply_limit": -1},
        {"include_replies": "true"},
    ],
)
def test_invalid_limits_are_rejected(kwargs):
    with pytest.raises(ValueError):
        normalize_detail(payload(), Settings(), **kwargs)


class FakeRunner:
    def __init__(self, **budget):
        self.settings = Settings(xhs_mcp="http://localhost:18060/mcp")
        self.run = {
            "id": "run",
            "trip_id": "trip",
            "generation": 1,
            "budget": DiscoveryBudget(**budget).model_dump(),
            "usage": {},
            "checkpoint": {},
        }
        self.actions, self.items, self.handles, self.sources = [], {}, {}, {}
        self.current, self.progress = True, {}

    async def db(self, method, *args, **kwargs):
        if method == "get_run":
            return copy.deepcopy(self.run)
        if method == "list_actions":
            return copy.deepcopy(self.actions)
        if method == "begin_action":
            if not self.current or args[1] != self.run["generation"]:
                return None
            kind = args[3]
            limits = {"search": self.run["budget"]["max_searches"]}
            if (
                kind in limits
                and sum(action["kind"] == kind for action in self.actions)
                >= limits[kind]
            ):
                return None
            if len(self.actions) >= self.run["budget"]["max_steps"]:
                return None
            identity = f"action-{len(self.actions)}"
            self.actions.append(
                {
                    "id": identity,
                    "action_key": args[2],
                    "kind": kind,
                    "params": args[4],
                    "status": "started",
                    "result": None,
                }
            )
            return identity
        if method == "finish_action":
            next(action for action in self.actions if action["id"] == args[0]).update(
                status=kwargs["status"], result=kwargs.get("result")
            )
            return True
        if method == "get_run_item":
            return copy.deepcopy(
                self.items.get(args[1], args[2] if len(args) > 2 else None)
            )
        if method == "save_run_item":
            if not self.current:
                return False
            self.items[args[2]] = copy.deepcopy(args[3])
            return True
        if method == "save_source_handle":
            if not self.current:
                return False
            self.handles[args[2]] = args[3]
            return True
        if method == "get_source_handle":
            return self.handles.get(args[1])
        if method == "get_source":
            return next(item for item in self.sources.values() if item["id"] == args[1])
        if method == "save_source":
            if not self.current:
                return None
            source = (
                args[3].model_dump(mode="json")
                if hasattr(args[3], "model_dump")
                else args[3]
            )
            key = (source["kind"], source["external_id"] or source["text"])
            self.sources.setdefault(
                key, {**source, "id": f"source-{len(self.sources)}"}
            )
            return self.sources[key]
        if method == "set_discovery_progress":
            if not self.current:
                return False
            self.progress.update(args[2])
            self.run["usage"].update(args[2])
            return True
        raise AssertionError(method)


class FakeMCP:
    def __init__(self, responder=None):
        self.lock, self.session = asyncio.Lock(), self
        self.calls, self.active, self.peak = [], 0, 0
        self.responder = responder or self.default
        self.tools = {
            "check_login_status": SimpleNamespace(inputSchema={"type": "object"}),
            "search_feeds": SimpleNamespace(
                inputSchema={
                    "type": "object",
                    "properties": {"keyword": {"type": "string"}},
                    "required": ["keyword"],
                    "additionalProperties": False,
                }
            ),
            "get_feed_detail": SimpleNamespace(
                inputSchema={
                    "type": "object",
                    "properties": {
                        "feed_id": {"type": "string"},
                        "xsec_token": {"type": "string"},
                        "load_all_comments": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "click_more_replies": {"type": "boolean"},
                        "reply_limit": {"type": "integer"},
                    },
                    "required": ["feed_id", "xsec_token"],
                    "additionalProperties": False,
                }
            ),
        }

    @staticmethod
    def default(name, arguments):
        if name == "check_login_status":
            return "已登录"
        if name == "search_feeds":
            return {
                "feeds": [
                    {
                        "id": arguments["keyword"],
                        "xsecToken": "private-token",
                        "noteCard": {"title": "攻略"},
                    }
                ]
            }
        return payload(
            [
                comment(
                    arguments["feed_id"] + "-c1",
                    subComments=[comment(arguments["feed_id"] + "-r1", "回复")],
                )
            ],
            noteId=arguments["feed_id"],
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            value = self.responder(name, arguments)
            if isinstance(value, BaseException):
                raise value
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                structuredContent=value if isinstance(value, dict) else None,
                isError=False,
            )
        finally:
            self.active -= 1


def install(monkeypatch, mcp):
    @asynccontextmanager
    async def fake_connect(url, client):
        yield mcp

    monkeypatch.setattr("easytrip.services.collection.connect", fake_connect)


def query(value, purpose="main"):
    return {"query": value, "purpose": purpose, "direction_hint": None}


def test_collector_reserves_gap_budget_and_counts_every_physical_call(monkeypatch):
    async def run():
        runner = FakeRunner(max_searches=4, reserved_gap_searches=2, target_notes=30)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        main = await service.discover([query(f"note-{i}") for i in range(5)])
        assert main["stop_reason"] == "main_search_budget_exhausted"
        assert len(main["feeds"]) == 2
        gap = await service.discover(
            [query("meal", "meal_gap"), query("hotel", "lodging_gap")], gap=True
        )
        assert len(gap["feeds"]) == 4
        assert len(runner.actions) == len(mcp.calls) == 5
        assert [item["kind"] for item in runner.actions].count("search") == 4
        assert "private-token" not in str(runner.items) + str(runner.actions)
        assert runner.handles["meal"] == {"xsec_token": "private-token"}
        assert mcp.peak == 1

    asyncio.run(run())


def test_collector_completed_call_cache_restores_without_refetch(monkeypatch):
    async def run():
        runner, mcp = FakeRunner(), FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        found = await service.discover([query("note-1")])
        fetched = await service.fetch(found["feeds"])
        assert fetched["notes_obtained"] == 1 and fetched["comments_selected"] == 1
        before = len(mcp.calls)
        runner.items.pop("collection:feeds")
        runner.items.pop("note:note-1")
        # Crash between a completed call and stage checkpoint: restore the safe cached payload.
        restored = await service.discover([query("note-1")])
        assert await service.fetch(restored["feeds"]) == fetched
        assert len(mcp.calls) == before
        assert len(runner.sources) == 2
        assert runner.progress["notes_obtained"] == 1

    asyncio.run(run())


def test_collector_timeout_is_unknown_and_only_explicit_new_generation_retries(
    monkeypatch,
):
    async def run():
        runner = FakeRunner(max_searches=3, reserved_gap_searches=1)
        failed = True

        def respond(name, arguments):
            return (
                TimeoutError("raw URL/token must never be recorded")
                if name == "search_feeds" and failed
                else FakeMCP.default(name, arguments)
            )

        mcp = FakeMCP(respond)
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        with pytest.raises(RuntimeError, match="source_result_unknown"):
            await service.discover([query("note")])
        assert runner.actions[-1]["status"] == "unknown"
        count = len(mcp.calls)
        with pytest.raises(RuntimeError, match="source_result_unknown"):
            await service.discover([query("note")])
        assert len(mcp.calls) == count
        failed = False
        runner.run["generation"] += 1
        assert len((await service.discover([query("note")]))["feeds"]) == 1
        assert runner.actions[-1]["action_key"].endswith(":retry:1")
        assert len(mcp.calls) == count + 1
        assert "raw URL" not in str(runner.actions)

    asyncio.run(run())


def test_collector_cancel_gate_stops_before_new_remote_request(monkeypatch):
    async def run():
        runner, mcp = FakeRunner(), FakeMCP()
        runner.current = False
        install(monkeypatch, mcp)
        with pytest.raises(
            RuntimeError, match="source_budget_or_run_changed|run_changed"
        ):
            await CollectionService(runner, runner.run, None).discover([query("note")])
        assert mcp.calls == []

    asyncio.run(run())


def test_collector_cancelled_inflight_call_is_recorded_unknown(monkeypatch):
    async def run():
        runner = FakeRunner()
        mcp = FakeMCP(
            lambda name, arguments: (
                asyncio.CancelledError()
                if name == "search_feeds"
                else FakeMCP.default(name, arguments)
            )
        )
        install(monkeypatch, mcp)
        with pytest.raises(asyncio.CancelledError):
            await CollectionService(runner, runner.run, None).discover([query("note")])
        assert runner.actions[-1]["status"] == "unknown"

    asyncio.run(run())


def test_collector_reply_total_shared_across_notes_and_source_context_preserved(
    monkeypatch,
):
    async def run():
        runner = FakeRunner(include_replies=True, reply_limit=1)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        feeds = (await service.discover([query("n1"), query("n2")]))["feeds"]
        output = await service.fetch(feeds)
        assert output["notes_obtained"] == 2 and output["comments_selected"] == 3
        details = [
            arguments for name, arguments in mcp.calls if name == "get_feed_detail"
        ]
        assert [item["click_more_replies"] for item in details] == [True, False]
        assert [item["reply_limit"] for item in details] == [1, 0]
        reply = runner.items["note:n1"]["note"]["comments"][1]
        assert (
            reply["reply_to_source_id"]
            == runner.items["note:n1"]["note"]["comments"][0]["source_id"]
        )

    asyncio.run(run())


def test_collector_schema_must_support_requested_comment_features(monkeypatch):
    async def run():
        runner, mcp = FakeRunner(include_replies=True), FakeMCP()
        del mcp.tools["get_feed_detail"].inputSchema["properties"]["click_more_replies"]
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        feeds = (await service.discover([query("note")]))["feeds"]
        with pytest.raises(RuntimeError, match="source_schema_incompatible"):
            await service.fetch(feeds)
        assert not any(name == "get_feed_detail" for name, _ in mcp.calls)

    asyncio.run(run())


def real_runner(tmp_path, **budget):
    db = Database(tmp_path / "collection.sqlite3")
    db.initialize()
    trip = db.create_trip(CreateTrip(operation_id=uuid4()))
    db.enqueue_run(
        trip["id"],
        CreateRun(
            operation_id=uuid4(), expected_version=1, kind="discover", budget=budget
        ),
    )
    run = db.claim_run()
    return DiscoveryRunner(db, Settings(xhs_mcp="http://localhost:18060/mcp")), run


def test_sqlite_collector_restores_note_tracker_and_usage_after_reopen(
    tmp_path, monkeypatch
):
    async def execute():
        runner, run = real_runner(tmp_path, include_replies=True, reply_limit=1)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, run, None)
        found = await service.discover([query("n1"), query("n2")])
        result = await service.fetch(found["feeds"])
        assert result["notes_obtained"] == 2 and result["comments_selected"] == 3
        before = len(mcp.calls)
        db = runner.database
        with db.connection(write=True) as connection:
            # Saved note but missing index/checkpoint: this must not lose it on resume.
            connection.execute(
                "DELETE FROM run_items WHERE run_id=? AND item_key='collection:notes'",
                (run["id"],),
            )
        reopened = Database(db.path)
        reopened.initialize()
        again = CollectionService(
            DiscoveryRunner(reopened, runner.settings),
            reopened.get_run(run["id"]),
            None,
        )
        assert await again.fetch(found["feeds"]) == result
        assert len(mcp.calls) == before
        assert reopened.get_run_item(run["id"], "collection:notes") == ["n1", "n2"]
        usage = reopened.get_run(run["id"])["usage"]
        assert usage["external_calls"] == len(mcp.calls) == 5
        assert (
            usage["searches"] == usage["detail_calls"] == usage["notes_obtained"] == 2
        )
        assert usage["comments_selected"] == 3
        sources = reopened.list_run_sources(run["trip_id"], run["id"])
        assert len(sources) == 5 and all("author_key" not in item for item in sources)
        assert "private-token" not in str(sources) + str(
            reopened.list_actions(run["id"])
        ) + str(reopened.get_trip(run["trip_id"]))
        assert (
            reopened.get_source_handle(run["id"], "n1")["xsec_token"] == "private-token"
        )

    asyncio.run(execute())


def test_sqlite_collector_late_remote_response_cannot_save_cancelled_sources(
    tmp_path, monkeypatch
):
    async def execute():
        runner, run = real_runner(tmp_path)

        def respond(name, arguments):
            if name == "search_feeds":
                runner.database.cancel_run(run["id"], run["trip_id"], run["generation"])
            return FakeMCP.default(name, arguments)

        mcp = FakeMCP(respond)
        install(monkeypatch, mcp)
        with pytest.raises(RuntimeError, match="run_changed"):
            await CollectionService(runner, run, None).discover(
                [query("n1"), query("n2")]
            )
        assert [name for name, _ in mcp.calls] == ["check_login_status", "search_feeds"]
        assert runner.database.get_run(run["id"])["status"] == "cancelled"
        assert runner.database.get_run(run["id"])["usage"]["external_calls"] == 2
        assert runner.database.list_run_sources(run["trip_id"], run["id"]) == []
        assert runner.database.get_source_handle(run["id"], "n1") is None
        assert runner.database.get_run_item(run["id"], "collection:feeds") is None

    asyncio.run(execute())


def test_sqlite_collector_global_step_budget_cannot_be_bypassed_by_details(
    tmp_path, monkeypatch
):
    async def execute():
        runner, run = real_runner(tmp_path, max_steps=2)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, run, None)
        found = await service.discover([query("n1")])
        with pytest.raises(RuntimeError, match="source_budget_or_run_changed"):
            await service.fetch(found["feeds"])
        assert len(mcp.calls) == 2
        assert not any(name == "get_feed_detail" for name, _ in mcp.calls)
        assert runner.database.get_run(run["id"])["usage"]["steps"] == 2

    asyncio.run(execute())


def test_sqlite_unknown_retry_requires_resume_and_retains_accumulated_budget(
    tmp_path, monkeypatch
):
    async def execute():
        runner, run = real_runner(tmp_path, max_searches=3, reserved_gap_searches=1)
        failed = True

        def respond(name, arguments):
            return (
                TimeoutError()
                if name == "search_feeds" and failed
                else FakeMCP.default(name, arguments)
            )

        mcp = FakeMCP(respond)
        install(monkeypatch, mcp)
        service = CollectionService(runner, run, None)
        with pytest.raises(RuntimeError, match="source_result_unknown"):
            await service.discover([query("n1")])
        assert runner.database.get_run(run["id"])["usage"]["unknown_result_calls"] == 1
        with pytest.raises(RuntimeError, match="source_result_unknown"):
            await service.discover([query("n1")])
        assert len(mcp.calls) == 2
        runner.database.stop_run(
            run["id"], run["generation"], "paused", "source_result_unknown"
        )
        runner.database.resume_run(run["id"], run["trip_id"], run["generation"])
        resumed = runner.database.claim_run()
        failed = False
        again = CollectionService(runner, resumed, None)
        assert len((await again.discover([query("n1")]))["feeds"]) == 1
        stats = runner.database.get_run(run["id"])["usage"]
        assert stats["searches"] == 2 and stats["external_calls"] == 3
        assert stats["unknown_result_calls"] == 1
        assert runner.database.list_actions(run["id"])[-1]["action_key"].endswith(
            ":retry:1"
        )
        main = await again.discover([query("n2")])
        assert main["stop_reason"] == "main_search_budget_exhausted"
        assert len(mcp.calls) == 3
        gap = await again.discover([query("n2", "meal_gap")], gap=True)
        assert len(gap["feeds"]) == 2
        assert runner.database.get_run(run["id"])["usage"]["searches"] == 3

    asyncio.run(execute())


def continuation(runner, parent, *, expected_version=None, **budget):
    runner.database.enqueue_run(
        parent["trip_id"],
        CreateRun(
            operation_id=uuid4(),
            expected_version=expected_version or parent["request_version"],
            kind="discover",
            parent_run_id=parent["id"],
            budget=budget,
        ),
    )
    return runner.database.claim_run()


def test_sqlite_continuation_skips_parent_notes_without_spending_new_note_budget(
    tmp_path, monkeypatch
):
    async def execute():
        runner, parent = real_runner(tmp_path, target_notes=1)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        old_service = CollectionService(runner, parent, None)
        old_feeds = (await old_service.discover([query("n1")]))["feeds"]
        await old_service.fetch(old_feeds)
        old_record = runner.database.get_run_item(parent["id"], "note:n1")
        parent_usage = runner.database.get_run(parent["id"])["usage"]
        child = continuation(runner, parent, target_notes=2)

        def mixed(name, arguments):
            if name == "search_feeds":
                return {
                    "feeds": [
                        {"id": identity, "xsecToken": "private-token"}
                        for identity in ("n1", "n2", "n3")
                    ]
                }
            return FakeMCP.default(name, arguments)

        mcp.responder = mixed
        service = CollectionService(runner, child, None)
        result = await service.discover([query("again")])
        assert result["coverage"]["reused_skipped"] == 1
        assert [feed["external_id"] for feed in result["feeds"]] == ["n2", "n3"]
        # A caller accidentally passing a parent feed still cannot fetch it a second time.
        fetched = await service.fetch(old_feeds + result["feeds"])
        assert fetched["notes_obtained"] == 2 and fetched["comments_selected"] == 2
        assert (
            len(
                [
                    args
                    for name, args in mcp.calls
                    if name == "get_feed_detail" and args["feed_id"] == "n1"
                ]
            )
            == 1
        )
        inherited = runner.database.get_run_item(child["id"], "collection:inherited")
        assert inherited == [
            {"external_id": "n1", "run_id": parent["id"], "note_record": old_record}
        ]
        assert runner.database.get_run_item(child["id"], "collection:notes") == [
            "n2",
            "n3",
        ]
        usage = runner.database.get_run(child["id"])["usage"]
        assert usage["notes_obtained"] == 2 and usage["external_calls"] == 4
        assert runner.database.get_run(parent["id"])["usage"] == parent_usage
        assert (
            runner.database.trip_usage(parent["trip_id"])["external_calls"]
            == parent_usage["external_calls"] + 4
        )
        child_sources = runner.database.list_run_sources(child["trip_id"], child["id"])
        assert {
            item["external_id"] for item in child_sources if item["kind"] == "note"
        } == {"n2", "n3"}

    asyncio.run(execute())


def test_sqlite_continuation_walks_parent_chain_once_and_excludes_failed_unknown(
    tmp_path, monkeypatch
):
    async def execute():
        runner, first = real_runner(tmp_path)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, first, None)
        await service.fetch((await service.discover([query("n1")]))["feeds"])
        runner.database.save_run_item(
            first["id"], first["generation"], "note:failed", {"status": "failed"}
        )
        runner.database.save_run_item(
            first["id"], first["generation"], "note:unknown", {"status": "unknown"}
        )
        runner.database.save_run_item(
            first["id"],
            first["generation"],
            "collection:notes",
            ["n1", "failed", "unknown"],
        )
        middle = continuation(runner, first)
        # Protect against a malformed cycle in saved ancestry as well as repeated real IDs.
        with runner.database.connection(write=True) as connection:
            params = first["params"] | {"parent_run_id": middle["id"]}
            connection.execute(
                "UPDATE runs SET params_json=? WHERE id=?",
                (json.dumps(params), first["id"]),
            )
        child = continuation(runner, middle)
        inherited = await CollectionService(runner, child, None)._inherited_success()
        assert [(item["external_id"], item["run_id"]) for item in inherited] == [
            ("n1", first["id"])
        ]
        assert runner.database.get_run(child["id"])["usage"]["external_calls"] == 0

    asyncio.run(execute())


def test_sqlite_continuation_does_not_inherit_a_parent_with_old_conditions(
    tmp_path, monkeypatch
):
    async def execute():
        runner, parent = real_runner(tmp_path)
        mcp = FakeMCP()
        install(monkeypatch, mcp)
        service = CollectionService(runner, parent, None)
        await service.fetch((await service.discover([query("n1")]))["feeds"])
        runner.database.update_trip(
            parent["trip_id"], TripPatch(expected_version=1, name="新条件版本")
        )
        child = continuation(runner, parent, expected_version=2, target_notes=1)
        result = await CollectionService(runner, child, None).discover([query("n1")])
        assert [feed["external_id"] for feed in result["feeds"]] == ["n1"]
        assert result["coverage"]["reused_skipped"] == 0
        assert runner.database.get_run_item(child["id"], "collection:inherited") == []

    asyncio.run(execute())


def test_sqlite_initial_fetch_marks_every_image_unanalyzed_and_gap_keeps_analysis_counts(
    tmp_path, monkeypatch
):
    async def execute():
        runner, run = real_runner(tmp_path)

        def with_images(name, arguments):
            if name == "get_feed_detail":
                return payload(
                    noteId=arguments["feed_id"],
                    imageList=[
                        {"urlDefault": f"https://sns.xhscdn.com/{i}.jpg"}
                        for i in range(12)
                    ],
                )
            return FakeMCP.default(name, arguments)

        mcp = FakeMCP(with_images)
        install(monkeypatch, mcp)
        service = CollectionService(runner, run, None)
        first = (await service.discover([query("n1")]))["feeds"]
        assert (await service.fetch(first))["images_unanalyzed"] == 12
        first_usage = runner.database.get_run(run["id"])["usage"]
        assert first_usage["images_obtained"] == first_usage["images_unanalyzed"] == 12
        runner.database.set_discovery_progress(
            run["id"],
            run["generation"],
            {"images_analyzed_unique": 3, "images_unanalyzed": 9},
        )
        assert (await service.fetch(first))["images_unanalyzed"] == 9
        gap = (await service.discover([query("n2", "meal_gap")], gap=True))["feeds"]
        assert (await service.fetch(gap))["images_unanalyzed"] == 21
        usage = runner.database.get_run(run["id"])["usage"]
        assert usage["images_analyzed_unique"] == 3 and usage["images_obtained"] == 24
        assert usage["images_unanalyzed"] == 21

    asyncio.run(execute())


def test_capability_records_actual_handshake_and_schema_change_against_first(
    monkeypatch,
):
    async def execute():
        runner, mcp = FakeRunner(), FakeMCP()
        runner.settings.secrets.add("capability-secret")
        mcp.initialized = SimpleNamespace(
            protocolVersion="2025-06-18",
            serverInfo={
                "name": "mock",
                "version": "v1",
                "websiteUrl": "https://example.test/private",
            },
        )
        mcp.tools["search_feeds"].inputSchema["description"] = "capability-secret"
        mcp.tools["publish_feed"] = SimpleNamespace(inputSchema={"type": "object"})
        install(monkeypatch, mcp)
        service = CollectionService(runner, runner.run, None)
        await service.discover([query("note")])
        first = runner.items["source:capability"]
        assert first["protocol_version"] == "2025-06-18"
        assert first["server_info"]["version"] == "v1"
        assert first["server_info"]["websiteUrl"] == "[URL]"
        assert first["schema_changed"] is False
        assert set(first["tool_schemas"]) == {
            "check_login_status",
            "search_feeds",
            "get_feed_detail",
        }
        assert first["tool_schemas"]["search_feeds"]["description"] == "[REDACTED]"
        count = len(mcp.calls)
        mcp.tools["search_feeds"].inputSchema["properties"]["new_required"] = {
            "type": "string"
        }
        mcp.tools["search_feeds"].inputSchema["required"].append("new_required")
        with pytest.raises(RuntimeError, match="source_schema_incompatible"):
            await service.discover([query("note")])
        changed = runner.items["source:capability"]
        assert changed["schema_changed"] is True
        assert changed["first_schema_fingerprint"] == first["schema_fingerprint"]
        assert changed["schema_fingerprint"] != first["schema_fingerprint"]
        assert len(mcp.calls) == count

    asyncio.run(execute())


def test_capability_missing_test_handshake_is_explicit_unknown(monkeypatch):
    async def execute():
        runner, mcp = FakeRunner(), FakeMCP()
        install(monkeypatch, mcp)
        await CollectionService(runner, runner.run, None).discover([query("note")])
        capability = runner.items["source:capability"]
        assert (
            capability["protocol_version"] == "unknown"
            and capability["server_info"] is None
        )
        assert capability["schema_changed"] is False

    asyncio.run(execute())
