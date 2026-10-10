import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema.exceptions import ValidationError
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from backend.app.config import Settings
from backend.app.integrations import xhs
from backend.app.integrations.xhs import (
    ReadOnlyMCP,
    SourcePaused,
    normalize_note,
    result_json,
)


def tool(name="search_feeds"):
    return SimpleNamespace(
        name=name,
        inputSchema={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
            "additionalProperties": False,
        },
    )


def result(text="{}", is_error=False, structured=None):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        isError=is_error,
        structuredContent=structured,
    )


def test_mcp_rejects_writes_missing_tools_and_invalid_arguments_before_calling():
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result()))

    async def run():
        mcp = ReadOnlyMCP(session, None, [tool(), tool("publish_content")])
        with pytest.raises(PermissionError):
            await mcp.call("publish_content", {"keyword": "西湖"})
        with pytest.raises(ValueError):
            await mcp.call("get_feed_detail", {})
        for arguments in (
            {},
            {"keyword": 123},
            {"keyword": "西湖", "unexpected": True},
        ):
            with pytest.raises(ValidationError):
                await mcp.call("search_feeds", arguments)
        assert mcp.calls == []

    asyncio.run(run())
    session.call_tool.assert_not_awaited()


def test_mcp_calls_are_serialized():
    active = 0
    maximum = 0
    received = []

    async def call_tool(name, arguments):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        received.append(arguments["keyword"])
        await asyncio.sleep(0)
        active -= 1
        return result()

    async def run():
        mcp = ReadOnlyMCP(SimpleNamespace(call_tool=call_tool), None, [tool()])
        await asyncio.gather(
            *(mcp.call("search_feeds", {"keyword": str(index)}) for index in range(3))
        )
        assert len(mcp.calls) == 3

    asyncio.run(run())
    assert maximum == 1
    assert received == ["0", "1", "2"]


def test_mcp_timeout_retries_stop_after_three_attempts(monkeypatch):
    session = SimpleNamespace(call_tool=AsyncMock(side_effect=TimeoutError))
    delays = AsyncMock()
    monkeypatch.setattr(xhs.asyncio, "sleep", delays)

    async def run():
        mcp = ReadOnlyMCP(session, None, [tool()])
        with pytest.raises(TimeoutError):
            await mcp.call("search_feeds", {"keyword": "西湖"})
        assert [call["attempt"] for call in mcp.calls] == [1, 2, 3]

    asyncio.run(run())
    assert session.call_tool.await_count == 3
    assert [call.args for call in delays.await_args_list] == [(1,), (2,)]


@pytest.mark.parametrize("code, attempts", [(408, 3), (-32602, 1), (500, 1)])
def test_sdk_timeout_code_is_retried_but_other_mcp_errors_are_not(
    monkeypatch, code, attempts
):
    error = McpError(ErrorData(code=code, message="fixture error"))
    session = SimpleNamespace(call_tool=AsyncMock(side_effect=error))
    delays = AsyncMock()
    monkeypatch.setattr(xhs.asyncio, "sleep", delays)

    async def run():
        with pytest.raises(McpError) as raised:
            await ReadOnlyMCP(session, None, [tool()]).call(
                "search_feeds", {"keyword": "西湖"}
            )
        assert raised.value is error

    asyncio.run(run())
    assert session.call_tool.await_count == attempts
    assert delays.await_count == attempts - 1


def test_successful_json_note_mentioning_captcha_does_not_pause_source():
    successful = result('{"data":{"note":{"desc":"预约需要手机验证码"}}}')
    session = SimpleNamespace(call_tool=AsyncMock(return_value=successful))

    async def run():
        returned = await ReadOnlyMCP(session, None, [tool()]).call(
            "search_feeds", {"keyword": "西湖"}
        )
        assert returned is successful

    asyncio.run(run())
    session.call_tool.assert_awaited_once()


def test_mcp_business_error_is_not_returned_as_success():
    session = SimpleNamespace(
        call_tool=AsyncMock(return_value=result("failure", is_error=True))
    )

    async def run():
        with pytest.raises(RuntimeError, match="业务失败"):
            await ReadOnlyMCP(session, None, [tool()]).call(
                "search_feeds", {"keyword": "西湖"}
            )

    asyncio.run(run())
    session.call_tool.assert_awaited_once()


@pytest.mark.parametrize(
    "message", ["未登录", "需要验证码", "captcha", "login required"]
)
def test_mcp_access_failure_pauses_source_without_retry(message):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result(message)))

    async def run():
        with pytest.raises(SourcePaused):
            await ReadOnlyMCP(session, None, [tool()]).call(
                "search_feeds", {"keyword": "西湖"}
            )

    asyncio.run(run())
    session.call_tool.assert_awaited_once()


def test_connect_injects_the_supplied_client_into_sdk_transport(monkeypatch):
    client = object()
    read, write = object(), object()
    initialized = object()
    pages = [
        SimpleNamespace(tools=[tool()], nextCursor="next"),
        SimpleNamespace(tools=[], nextCursor=None),
    ]
    session = SimpleNamespace(
        initialize=AsyncMock(return_value=initialized),
        list_tools=AsyncMock(side_effect=pages),
    )
    transport_calls = []
    session_calls = []

    @asynccontextmanager
    async def transport(url, *, http_client):
        transport_calls.append((url, http_client))
        yield read, write, lambda: None

    @asynccontextmanager
    async def client_session(actual_read, actual_write, *, read_timeout_seconds):
        session_calls.append((actual_read, actual_write, read_timeout_seconds))
        yield session

    monkeypatch.setattr(xhs, "streamable_http_client", transport)
    monkeypatch.setattr(xhs, "ClientSession", client_session)

    async def run():
        async with xhs.connect("https://mcp.example/read", client) as mcp:
            assert mcp.initialized is initialized
            assert mcp.session is session
            assert set(mcp.tools) == {"search_feeds"}

    asyncio.run(run())
    assert transport_calls == [("https://mcp.example/read", client)]
    assert session_calls == [(read, write, timedelta(seconds=90))]
    assert [call.kwargs for call in session.list_tools.await_args_list] == [
        {"cursor": None},
        {"cursor": "next"},
    ]


def test_normalize_note_deduplicates_and_caps_first_level_comments():
    comments = [
        {
            "id": str(index),
            "content": f"评论{index}",
            "user": {"nickname": "private"},
            "subComments": [{"content": "二级回复"}],
        }
        for index in range(12)
    ]
    comments += [
        comments[0],
        {"content": "无 ID 评论"},
        {"content": "无 ID 评论"},
        {"content": ""},
    ]
    payload = {
        "data": {
            "note": {
                "title": "西湖",
                "desc": "联系13800138000 https://example.com/sign?token=abc",
                "type": "video",
                "user": {"nickname": "private"},
                "imageList": [{"urlDefault": "https://cdn.example/image"}],
            },
            "comments": {"list": comments, "hasMore": True},
        }
    }
    note = normalize_note(payload, "note-1", Settings())
    assert note["comments_obtained"] == 16
    assert note["unique_first_level_comments"] == 13
    assert note["comments_selected"] == 10
    assert note["comments"] == [
        {"source_id": f"note-1-comment-{index + 1}", "text": f"评论{index}"}
        for index in range(10)
    ]
    assert note["text"] == "联系[PHONE] [URL]"
    assert note["video_unanalysed"] is True
    assert note["image_count"] == 1
    assert note["has_more_comments"] is True
    assert "user" not in note
    assert all(set(comment) == {"source_id", "text"} for comment in note["comments"])


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"note": {}}},
        {"data": {"note": {"title": "", "desc": ""}}},
        {"data": {"note": []}},
    ],
)
def test_normalize_note_rejects_missing_detail(payload):
    with pytest.raises(ValueError):
        normalize_note(payload, "note-1", Settings())


def test_result_json_prefers_structured_content():
    assert result_json(result("not JSON", structured={"feeds": []})) == {"feeds": []}
    assert result_json(result('{"feeds":[]}')) == {"feeds": []}
