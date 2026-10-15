"""Only three read-only tools are callable, in one serialized HTTP session."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from ..config import validate_url
from .mcp_schema import validate_arguments

READ_ONLY = frozenset({"check_login_status", "search_feeds", "get_feed_detail"})


class SourcePaused(RuntimeError):
    pass


class ToolFailure(RuntimeError):
    def __init__(self, message):
        self.service_message = message[:500]
        super().__init__("MCP 工具业务失败")


def result_text(result):
    return "\n".join(item.text for item in result.content if item.type == "text")


def result_json(result):
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result_text(result))


class ReadOnlyMCP:
    def __init__(self, session, initialized, tools):
        self.session = session
        self.initialized = initialized
        self.tools = {tool.name: tool for tool in tools}
        self.lock = asyncio.Lock()
        self.calls = []

    async def call(self, name, arguments):
        if name not in READ_ONLY:
            raise PermissionError("工具不在只读白名单")
        if name not in self.tools:
            raise ValueError("服务未提供必要只读工具")
        validate_arguments(arguments, self.tools[name].inputSchema)
        async with self.lock:
            for attempt in range(3):
                self.calls.append({"tool": name, "attempt": attempt + 1})
                try:
                    result = await self.session.call_tool(name, arguments)
                    break
                except (TimeoutError, httpx.TimeoutException, McpError) as error:
                    if isinstance(error, McpError) and error.error.code != 408:
                        raise
                    if attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)
            text = result_text(result)
            if (
                name == "check_login_status"
                or result.isError
                or not text.lstrip().startswith(("{", "["))
            ) and re.search(
                r"未登录|登录失效|验证码|访问被拒|captcha|login required",
                text,
                re.IGNORECASE,
            ):
                raise SourcePaused("登录/验证/访问异常，已暂停该来源")
            if result.isError:
                raise ToolFailure(text)
            return result


@asynccontextmanager
async def connect(url, client):
    validate_url(url)
    async with streamable_http_client(url, http_client=client) as (read, write, _):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=90)
        ) as session:
            initialized = await session.initialize()
            tools, cursor = [], None
            for _ in range(10):
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            if cursor:
                raise ValueError("工具列表超过 P0 探测页数上限")
            yield ReadOnlyMCP(session, initialized, tools)


def normalize_note(payload, source_id, settings, comment_limit=10):
    """Keep evidence text, remove identity/handles, and cap only first-level comments."""
    data = payload.get("data", payload)
    note = data.get("note", {})
    if not isinstance(note, dict) or not (note.get("desc") or note.get("title")):
        raise ValueError("详情缺少预期 note.title/desc 字段")
    block = data.get("comments", {})
    comments = block.get("list", []) if isinstance(block, dict) else block
    unique, seen = [], set()
    for comment in comments or []:
        identity = comment.get("id") or comment.get("content")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique.append(comment)
    selected = [
        {
            "source_id": f"{source_id}-comment-{i + 1}",
            "text": settings.redact(str(comment.get("content", ""))[:1000]),
        }
        for i, comment in enumerate(unique[:comment_limit])
    ]
    images = []
    for item in note.get("imageList", []) or []:
        url = item.get("urlDefault") or item.get("urlPre")
        if url:
            images.append(url)
    return {
        "source_id": source_id,
        "title": settings.redact(str(note.get("title", ""))),
        "text": settings.redact(str(note.get("desc", ""))[:8000]),
        "published_at": note.get("time") or None,
        "images": images,
        "image_count": len(images),
        "video_unanalysed": note.get("type") == "video",
        "comments": selected,
        "comments_obtained": len(comments or []),
        "unique_first_level_comments": len(unique),
        "comments_selected": len(selected),
        "has_more_comments": block.get("hasMore") if isinstance(block, dict) else None,
        "actual_note_fields": sorted(note),
        "actual_comment_fields": sorted(unique[0]) if unique else [],
    }
