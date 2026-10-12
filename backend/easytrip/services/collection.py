"""Bounded source normalization; access handles stay separate from source text."""

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from jsonschema import ValidationError as SchemaError
from jsonschema import validate

from ..domain.discovery import SourceComment, SourceNote
from ..integrations.model import image_url
from ..integrations.xhs import (
    READ_ONLY,
    SourcePaused,
    ToolFailure,
    connect,
    result_json,
    result_text,
)


def _external_id(item, *names):
    for name in names:
        value = item.get(name)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
            return value
    return None


def _author(item):
    user = item.get("user", item.get("userInfo", {}))
    if not isinstance(user, dict):
        return None
    identity = _external_id(user, "userId", "user_id", "id")
    return (
        hashlib.sha256(("xiaohongshu:author:" + identity).encode("utf-8")).hexdigest()
        if identity
        else None
    )


def _published(value):
    try:
        if type(value) in {int, float}:
            result = datetime.fromtimestamp(
                value / 1000 if value > 10_000_000_000 else value, timezone.utc
            )
        elif isinstance(value, str) and value.strip():
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.utcoffset() is None:
                return None
        else:
            return None
        if not 2010 <= result.year <= 2100:
            return None
        return result.isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _text(value, settings, limit):
    text = value if isinstance(value, str) else ""
    redacted = settings.redact(text)
    return redacted[:limit], len(redacted) > limit


def _data(payload):
    if not isinstance(payload, dict):
        raise ValueError("来源返回必须是对象")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("来源 data 必须是对象")
    return data


def normalize_search(payload, settings):
    data = _data(payload)
    feeds = data.get("feeds")
    if not isinstance(feeds, list):
        raise ValueError("搜索返回缺少 feeds 列表")
    selected, handles, seen = [], {}, set()
    duplicate, missing = 0, 0
    for feed in feeds:
        if not isinstance(feed, dict):
            missing += 1
            continue
        identity = _external_id(feed, "id", "noteId", "note_id")
        if identity is None:
            missing += 1
            continue
        token = feed.get("xsecToken", feed.get("xsec_token"))
        if not isinstance(token, str) or not token:
            url = feed.get("url")
            try:
                token = (
                    parse_qs(urlsplit(url).query).get("xsec_token", [None])[0]
                    if isinstance(url, str)
                    else None
                )
            except ValueError:
                token = None
        if isinstance(token, str) and token:
            handles.setdefault(identity, {"xsec_token": token})
        if identity in seen:
            duplicate += 1
            continue
        seen.add(identity)
        card = feed.get("noteCard", feed.get("note_card", feed))
        card = card if isinstance(card, dict) else {}
        title, _ = _text(card.get("displayTitle", card.get("title", "")), settings, 500)
        selected.append(
            {
                "external_id": identity,
                "title": title,
                "author_key": _author(card),
                "published_at": _published(card.get("time", card.get("publishTime"))),
            }
        )
    for item in selected:
        for handle in handles.values():
            item["title"] = item["title"].replace(handle["xsec_token"], "[REDACTED]")
    return {
        "feeds": selected,
        "handles": handles,
        "coverage": {
            "returned": len(feeds),
            "unique": len(selected),
            "duplicates": duplicate,
            "missing_ids": missing,
        },
    }


def _comments(block):
    if isinstance(block, dict):
        values = block.get("list", [])
    else:
        values = block
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("评论返回必须是列表")
    return [item for item in values if isinstance(item, dict)]


def _comment(item, settings, *, reply_to=None):
    text, truncated = _text(item.get("content", item.get("text", "")), settings, 30000)
    if not text.strip():
        return None
    return {
        "external_id": _external_id(item, "id", "commentId", "comment_id"),
        "text": text,
        "published_at": _published(item.get("createTime", item.get("time"))),
        "author_key": _author(item),
        "reply_to_external_id": reply_to,
        "completeness": "partial" if truncated else "complete",
    }


def _comment_key(item):
    if item["external_id"] is not None:
        return "external:" + item["external_id"]
    # Missing external identity remains None; this fingerprint only suppresses repeated payloads.
    return (
        "low:"
        + hashlib.sha256(
            (
                str(item["author_key"])
                + "\0"
                + str(item["reply_to_external_id"])
                + "\0"
                + item["text"]
            ).encode("utf-8")
        ).hexdigest()
    )


def normalize_detail(
    payload,
    settings,
    *,
    external_id=None,
    comment_limit=20,
    include_replies=False,
    reply_limit=20,
):
    if (
        type(comment_limit) is not int
        or not 0 <= comment_limit <= 50
        or type(include_replies) is not bool
        or type(reply_limit) is not int
        or not 0 <= reply_limit <= 100
    ):
        raise ValueError("评论采集上限无效")
    data = _data(payload)
    note = data.get("note")
    if not isinstance(note, dict) or not (note.get("title") or note.get("desc")):
        raise ValueError("详情缺少 note.title/desc")
    actual_id = _external_id(note, "noteId", "note_id", "id")
    if actual_id is not None and external_id is not None and actual_id != external_id:
        raise ValueError("详情笔记标识与请求不一致")
    identity = actual_id or external_id
    title, title_truncated = _text(note.get("title", ""), settings, 500)
    text, text_truncated = _text(note.get("desc", ""), settings, 30000)
    if not title.strip() and not text.strip():
        raise ValueError("详情缺少可分析的标题或正文")
    raw_images = note.get("imageList", []) or []
    if not isinstance(raw_images, list):
        raise ValueError("详情 imageList 必须是列表")
    images = []
    for item in raw_images[:100]:
        # Keep the original 1-based position; unsafe/missing URLs occupy an empty slot.
        retained = ""
        if isinstance(item, dict):
            for value in (item.get("urlDefault"), item.get("urlPre")):
                safe = image_url(value) if isinstance(value, str) else None
                if safe and len(safe) <= 2000:
                    retained = safe
                    break
        images.append(retained)
    block = data.get("comments", {})
    first = _comments(block)
    unique, seen, replies_available = [], set(), 0
    for item in first:
        normalized = _comment(item, settings)
        if normalized is None:
            continue
        key = _comment_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        unique.append((item, normalized))
        replies_available += len(
            _comments(item.get("subComments", item.get("sub_comments", [])))
        )
    selected = [normalized for _, normalized in unique[:comment_limit]]
    selected_replies, skipped_context = [], 0
    if include_replies:
        for item, parent in unique[:comment_limit]:
            replies = _comments(item.get("subComments", item.get("sub_comments", [])))
            # Without a real parent ID a reply cannot safely preserve its relationship.
            if parent["external_id"] is None:
                skipped_context += len(replies)
                continue
            for reply in replies:
                normalized = _comment(reply, settings, reply_to=parent["external_id"])
                if normalized is None:
                    continue
                key = _comment_key(normalized)
                if key in seen or len(selected_replies) >= reply_limit:
                    continue
                seen.add(key)
                selected_replies.append(normalized)
    has_more = (
        block.get("hasMore", block.get("has_more")) if isinstance(block, dict) else None
    )
    has_more = has_more if type(has_more) is bool else None
    source = SourceNote(
        external_id=identity,
        title=title,
        text=text,
        images=images,
        published_at=_published(note.get("time", note.get("publishTime"))),
        author_key=_author(note),
        completeness="partial"
        if title_truncated or text_truncated or len(raw_images) > 100
        else "complete",
    )
    return {
        "note": source.model_dump(mode="json"),
        "comments": selected + selected_replies,
        "video_unanalysed": note.get("type") == "video",
        "coverage": {
            "first_level_received": len(first),
            "first_level_unique": len(unique),
            "first_level_selected": len(selected),
            "replies_received": replies_available,
            "replies_selected": len(selected_replies),
            "replies_skipped_missing_context": skipped_context,
            "comments_selected": len(selected) + len(selected_replies),
            "has_more_comments": has_more,
            "images_received": len(raw_images),
            "images_retained": sum(bool(value) for value in images),
            "image_slots_retained": len(images),
            "images_omitted_over_limit": max(0, len(raw_images) - 100),
            "text_truncated": title_truncated or text_truncated,
        },
    }


class CollectionService:
    def __init__(self, runner, run, client):
        self.runner, self.run, self.client = runner, run, client
        self.settings = runner.settings
        self.budget = run["budget"]

    async def _db(self, method, *args, **kwargs):
        return await self.runner.db(method, *args, **kwargs)

    @staticmethod
    def _paused(reason):
        from ..agent.discovery import DiscoveryPaused

        return DiscoveryPaused(reason)

    @staticmethod
    def _stopped(reason):
        from ..agent.discovery import DiscoveryStopped

        return DiscoveryStopped(reason)

    def _arguments(self, mcp, name, arguments, required_fields=()):
        tool = mcp.tools.get(name)
        if name not in READ_ONLY or tool is None:
            raise self._paused("source_schema_incompatible")
        schema = tool.inputSchema
        if any(field not in schema.get("properties", {}) for field in required_fields):
            raise self._paused("source_schema_incompatible")
        try:
            validate(arguments, schema)
        except SchemaError:
            raise self._paused("source_schema_incompatible") from None
        return arguments

    async def _call(
        self, mcp, name, arguments, key, kind, normalize, *, safe_params=None
    ):
        self._arguments(mcp, name, arguments)
        actions = await self._db("list_actions", self.run["id"])
        previous = [
            item
            for item in actions
            if item["action_key"] == key
            or item["action_key"].startswith(key + ":retry:")
        ]
        for action in previous:
            if action["status"] == "completed" and action["result"] is not None:
                return action["result"]
        if previous and any(
            item["params"].get("generation") == self.run["generation"]
            for item in previous
        ):
            raise self._paused(
                "source_result_unknown"
                if any(item["status"] in {"started", "unknown"} for item in previous)
                else "source_request_failed"
            )
        action_key = key if not previous else key + f":retry:{len(previous)}"
        params = {
            "tool": name,
            "generation": self.run["generation"],
            **(safe_params or {}),
        }
        action_id = await self._db(
            "begin_action",
            self.run["id"],
            self.run["generation"],
            action_key,
            kind,
            params,
        )
        if action_id is None:
            raise self._stopped("source_budget_or_run_changed")
        status, saved = "unknown", None
        try:
            async with mcp.lock:
                # One physical request, no P0 automatic retry. Every request has its own ledger row.
                result = await mcp.session.call_tool(name, arguments)
            text = result_text(result)
            if (
                name == "check_login_status"
                or result.isError
                or not text.lstrip().startswith(("{", "["))
            ) and re.search(
                r"未登录|登录失效|验证码|访问被拒|captcha|login required", text, re.I
            ):
                status = "failed"
                raise SourcePaused("登录/验证/访问异常，已暂停该来源")
            if result.isError:
                status = "failed"
                raise ToolFailure("只读来源业务失败")
            try:
                saved = await normalize(result)
            except (ValueError, TypeError, KeyError):
                status = "failed"
                raise self._paused("source_payload_invalid") from None
            status = "completed"
            return saved
        except asyncio.CancelledError:
            raise
        except (SourcePaused, ToolFailure):
            status = "failed"
            raise
        except Exception:
            if status == "unknown":
                raise self._paused("source_result_unknown") from None
            raise
        finally:
            await asyncio.shield(
                self._db("finish_action", action_id, status=status, result=saved)
            )

    async def _login(self, mcp):
        initialized = getattr(mcp, "initialized", None)
        protocol = getattr(initialized, "protocolVersion", None) or "unknown"
        info = getattr(initialized, "serverInfo", None)
        if hasattr(info, "model_dump"):
            info = info.model_dump(mode="json")
        elif info is not None and not isinstance(info, dict):
            info = {
                "name": getattr(info, "name", None),
                "version": getattr(info, "version", None),
            }
        schemas = {
            name: mcp.tools[name].inputSchema
            for name in sorted(READ_ONLY)
            if name in mcp.tools
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                schemas, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        previous = await self._db("get_run_item", self.run["id"], "source:capability")
        first = (
            previous.get(
                "first_schema_fingerprint",
                previous.get("schema_fingerprint", fingerprint),
            )
            if previous
            else fingerprint
        )
        capability = self.settings.redact(
            {
                "protocol_version": protocol,
                "server_info": info,
                "tool_schemas": schemas,
                "schema_fingerprint": fingerprint,
                "first_schema_fingerprint": first,
                "schema_changed": fingerprint != first,
            }
        )
        if not await self._db(
            "save_run_item",
            self.run["id"],
            self.run["generation"],
            "source:capability",
            capability,
        ):
            raise self._stopped("run_changed")
        arguments = self._arguments(mcp, "check_login_status", {})

        async def normalize(result):
            if "已登录" not in result_text(result):
                raise SourcePaused("登录未确认，已暂停该来源")
            return {"logged_in": True}

        await self._call(
            mcp, "check_login_status", arguments, "source:login", "login", normalize
        )

    async def _inherited_success(self):
        from ..storage.database import NotFound

        cached = await self._db("get_run_item", self.run["id"], "collection:inherited")
        if cached is not None:
            return cached
        parent_id = self.run.get("params", {}).get("parent_run_id")
        if not parent_id:
            return []
        inherited, seen, visited = [], set(), {self.run["id"]}
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            try:
                parent = await self._db("get_run", parent_id, self.run["trip_id"])
            except NotFound:
                break
            if (
                parent["kind"] != "discover"
                or parent["request_version"] != self.run["request_version"]
            ):
                break
            identities = await self._db(
                "get_run_item", parent_id, "collection:notes", []
            )
            for identity in identities:
                if (
                    _external_id({"external_id": identity}, "external_id") is None
                    or identity in seen
                ):
                    continue
                record = await self._db("get_run_item", parent_id, "note:" + identity)
                if (
                    not isinstance(record, dict)
                    or record.get("status") != "ready"
                    or not isinstance(record.get("note"), dict)
                ):
                    continue
                seen.add(identity)
                inherited.append(
                    {
                        "external_id": identity,
                        "run_id": parent_id,
                        "note_record": record,
                    }
                )
            parent_id = parent.get("params", {}).get("parent_run_id")
        if not await self._db(
            "save_run_item",
            self.run["id"],
            self.run["generation"],
            "collection:inherited",
            inherited,
        ):
            raise self._stopped("run_changed")
        return inherited

    async def discover(self, query_plan, *, gap=False, note_target=None):
        target = (
            note_target
            if note_target is not None
            else (
                self.budget["target_notes"]
                if gap
                else math.ceil(self.budget["target_notes"] * 0.8)
            )
        )
        if type(target) is not int or not 1 <= target <= self.budget["target_notes"]:
            raise ValueError("采集目标无效")
        inherited = {item["external_id"] for item in await self._inherited_success()}
        feeds = await self._db("get_run_item", self.run["id"], "collection:feeds", [])
        feeds = list(feeds)
        seen = {item["external_id"] for item in feeds}
        coverage = {
            "returned": 0,
            "unique": len(feeds),
            "duplicates": 0,
            "missing_ids": 0,
            "queries_completed": 0,
            "over_target": 0,
            "reused_skipped": 0,
        }
        stop_reason = None
        if not self.settings.xhs_mcp:
            raise self._paused("source_configuration_missing")
        async with connect(self.settings.xhs_mcp, self.client) as mcp:
            await self._login(mcp)
            for item in query_plan:
                item = item.model_dump() if hasattr(item, "model_dump") else item
                query, purpose = item.get("query"), item.get("purpose", "main")
                if (
                    not isinstance(query, str)
                    or not query.strip()
                    or len(query) > 120
                    or purpose not in {"main", "discovery", "meal_gap", "lodging_gap"}
                ):
                    raise ValueError("搜索计划无效")
                if len(feeds) >= target:
                    stop_reason = "note_target_reached"
                    break
                query = self.settings.redact(query)
                logical_key = (
                    "source:search:" + hashlib.sha256(query.encode("utf-8")).hexdigest()
                )
                actions = await self._db("list_actions", self.run["id"])
                known = any(
                    action["status"] == "completed"
                    and action["result"] is not None
                    and (
                        action["action_key"] == logical_key
                        or action["action_key"].startswith(logical_key + ":retry:")
                    )
                    for action in actions
                )
                main_searches = sum(
                    action["kind"] == "search"
                    and not action["params"].get("gap", False)
                    for action in actions
                )
                if (
                    not gap
                    and not known
                    and main_searches
                    >= self.budget["max_searches"]
                    - self.budget["reserved_gap_searches"]
                ):
                    stop_reason = "main_search_budget_exhausted"
                    break
                arguments = self._arguments(
                    mcp, "search_feeds", {"keyword": query}, ("keyword",)
                )

                async def normalize(result):
                    normalized = normalize_search(result_json(result), self.settings)
                    for identity, handle in normalized.pop("handles").items():
                        self.settings.secrets.add(handle["xsec_token"])
                        await self._db(
                            "save_source_handle",
                            self.run["id"],
                            self.run["generation"],
                            identity,
                            handle,
                        )
                    return normalized

                result = await self._call(
                    mcp,
                    "search_feeds",
                    arguments,
                    logical_key,
                    "search",
                    normalize,
                    safe_params={"query": query, "gap": gap},
                )
                for key in ("returned", "duplicates", "missing_ids"):
                    coverage[key] += result["coverage"][key]
                coverage["queries_completed"] += 1
                for feed in result["feeds"]:
                    if feed["external_id"] in inherited:
                        coverage["reused_skipped"] += 1
                        continue
                    if feed["external_id"] in seen:
                        coverage["duplicates"] += 1
                        existing_feed = next(
                            value
                            for value in feeds
                            if value["external_id"] == feed["external_id"]
                        )
                        links = existing_feed.setdefault(
                            "queries",
                            [
                                {
                                    "query": existing_feed["query"],
                                    "purpose": existing_feed["purpose"],
                                }
                            ],
                        )
                        link = {"query": query, "purpose": purpose}
                        if link not in links:
                            links.append(link)
                        continue
                    if len(feeds) >= target:
                        coverage["over_target"] += 1
                        continue
                    seen.add(feed["external_id"])
                    feeds.append(
                        {
                            "external_id": feed["external_id"],
                            "title": feed["title"],
                            "query": query,
                            "purpose": purpose,
                            "direction_hint": item.get("direction_hint"),
                            "queries": [{"query": query, "purpose": purpose}],
                        }
                    )
                coverage["unique"] = len(feeds)
                if not await self._db(
                    "save_run_item",
                    self.run["id"],
                    self.run["generation"],
                    "collection:feeds",
                    feeds,
                ):
                    raise self._stopped("run_changed")
        return {
            "feeds": feeds,
            "coverage": coverage,
            **({"stop_reason": stop_reason} if stop_reason else {}),
        }

    async def _progress(self, identities):
        records = [
            await self._db("get_run_item", self.run["id"], "note:" + identity)
            for identity in identities
        ]
        ready = [item for item in records if item and item["status"] == "ready"]
        counts = {
            "notes_obtained": len(ready),
            "notes_failed": sum(
                item is not None and item["status"] == "failed" for item in records
            ),
            "comments_obtained": sum(
                item["coverage"]["first_level_unique"]
                + item["coverage"]["replies_received"]
                for item in ready
            ),
            "comments_selected": sum(len(item["note"]["comments"]) for item in ready),
            "images_obtained": sum(
                item["coverage"]["images_received"] for item in ready
            ),
        }
        current = await self._db("get_run", self.run["id"])
        counts["images_unanalyzed"] = max(
            0,
            counts["images_obtained"]
            - current["usage"].get("images_analyzed_unique", 0),
        )
        await self._db(
            "set_discovery_progress", self.run["id"], self.run["generation"], counts
        )
        return counts, sum(item["coverage"]["replies_selected"] for item in ready)

    def _detail_arguments(self, mcp, identity, handle, remaining_replies):
        schema = mcp.tools.get("get_feed_detail")
        if schema is None:
            raise self._paused("source_schema_incompatible")
        properties = schema.inputSchema.get("properties", {})
        required = {"feed_id", "xsec_token"}
        arguments = {"feed_id": identity, "xsec_token": handle["xsec_token"]}
        limit = self.budget["comment_limit"]
        include = self.budget["include_replies"] and remaining_replies > 0
        if "load_all_comments" in properties:
            arguments["load_all_comments"] = limit > 10 or include
        elif limit > 10 or include:
            raise self._paused("source_schema_incompatible")
        if "limit" in properties:
            arguments["limit"] = limit
        elif limit > 10:
            raise self._paused("source_schema_incompatible")
        if "click_more_replies" in properties:
            arguments["click_more_replies"] = include
        elif include:
            raise self._paused("source_schema_incompatible")
        if "reply_limit" in properties:
            arguments["reply_limit"] = remaining_replies
        elif include:
            raise self._paused("source_schema_incompatible")
        return self._arguments(mcp, "get_feed_detail", arguments, required)

    async def fetch(self, feeds, *, note_target=None):
        target = note_target if note_target is not None else self.budget["target_notes"]
        if type(target) is not int or not 1 <= target <= self.budget["target_notes"]:
            raise ValueError("详情目标无效")
        inherited = {item["external_id"] for item in await self._inherited_success()}
        identities = await self._db(
            "get_run_item", self.run["id"], "collection:notes", []
        )
        counts, replies_used = await self._progress(identities)
        seen, stop_reason = set(), None
        if not self.settings.xhs_mcp:
            raise self._paused("source_configuration_missing")
        async with connect(self.settings.xhs_mcp, self.client) as mcp:
            await self._login(mcp)
            for feed in feeds:
                identity = feed["external_id"]
                if identity in seen or identity in inherited:
                    continue
                seen.add(identity)
                existing = await self._db(
                    "get_run_item", self.run["id"], "note:" + identity
                )
                if existing is not None:
                    if existing["status"] == "ready":
                        saved_source = await self._db(
                            "get_source",
                            self.run["trip_id"],
                            existing["note"]["source_id"],
                            self.run["id"],
                            include_private=True,
                        )
                        body = SourceNote(
                            **{
                                key: saved_source[key]
                                for key in SourceNote.model_fields
                            }
                        )
                        for link in feed.get(
                            "queries",
                            [
                                {
                                    "query": feed.get("query", ""),
                                    "purpose": feed.get("purpose", "main"),
                                }
                            ],
                        ):
                            if (
                                await self._db(
                                    "save_source",
                                    self.run["trip_id"],
                                    self.run["id"],
                                    self.run["generation"],
                                    body,
                                    **link,
                                )
                                is None
                            ):
                                raise self._stopped("run_changed")
                    if identity not in identities:
                        identities.append(identity)
                        if not await self._db(
                            "save_run_item",
                            self.run["id"],
                            self.run["generation"],
                            "collection:notes",
                            identities,
                        ):
                            raise self._stopped("run_changed")
                        counts, replies_used = await self._progress(identities)
                    continue
                if counts["notes_obtained"] >= target:
                    stop_reason = "note_target_reached"
                    break
                handle = await self._db("get_source_handle", self.run["id"], identity)
                if (
                    not handle
                    or not isinstance(handle.get("xsec_token"), str)
                    or not handle["xsec_token"]
                ):
                    record = {"status": "failed", "error_code": "source_handle_missing"}
                else:
                    self.settings.secrets.add(handle["xsec_token"])
                    remaining = max(0, self.budget["reply_limit"] - replies_used)
                    arguments = self._detail_arguments(mcp, identity, handle, remaining)

                    async def normalize(result):
                        return normalize_detail(
                            result_json(result),
                            self.settings,
                            external_id=identity,
                            comment_limit=self.budget["comment_limit"],
                            include_replies=self.budget["include_replies"],
                            reply_limit=remaining,
                        )

                    try:
                        normalized = await self._call(
                            mcp,
                            "get_feed_detail",
                            arguments,
                            "source:detail:" + identity,
                            "detail",
                            normalize,
                            safe_params={
                                "external_id": identity,
                                "comment_limit": self.budget["comment_limit"],
                                "reply_limit": remaining,
                            },
                        )
                    except ToolFailure:
                        record = {
                            "status": "failed",
                            "error_code": "source_detail_failed",
                        }
                    else:
                        source = await self._db(
                            "save_source",
                            self.run["trip_id"],
                            self.run["id"],
                            self.run["generation"],
                            normalized["note"],
                            query=feed.get("query", ""),
                            purpose=feed.get("purpose", "main"),
                        )
                        if source is None:
                            raise self._stopped("run_changed")
                        for link in feed.get("queries", []):
                            if (
                                await self._db(
                                    "save_source",
                                    self.run["trip_id"],
                                    self.run["id"],
                                    self.run["generation"],
                                    normalized["note"],
                                    **link,
                                )
                                is None
                            ):
                                raise self._stopped("run_changed")
                        comments, mapping = [], {}
                        for item in normalized["comments"]:
                            parent_id = (
                                mapping.get(item["reply_to_external_id"])
                                if item["reply_to_external_id"]
                                else None
                            )
                            if item["reply_to_external_id"] and parent_id is None:
                                continue
                            body = SourceComment(
                                **{
                                    key: value
                                    for key, value in item.items()
                                    if key != "reply_to_external_id"
                                },
                                parent_source_id=source["id"],
                                reply_to_source_id=parent_id,
                            )
                            saved = await self._db(
                                "save_source",
                                self.run["trip_id"],
                                self.run["id"],
                                self.run["generation"],
                                body,
                                query=feed.get("query", ""),
                                purpose=feed.get("purpose", "main"),
                            )
                            if saved is None:
                                raise self._stopped("run_changed")
                            if item["external_id"]:
                                mapping[item["external_id"]] = saved["id"]
                            comments.append(
                                {
                                    "source_id": saved["id"],
                                    "text": saved["text"],
                                    "reply_to_source_id": parent_id,
                                }
                            )
                        record = {
                            "status": "ready",
                            "note": {
                                "source_id": source["id"],
                                "title": source["title"],
                                "text": source["text"],
                                "images": source["images"],
                                "comments": comments,
                                "video_unanalysed": normalized["video_unanalysed"],
                            },
                            "coverage": normalized["coverage"],
                        }
                if record["status"] == "failed":
                    failed_source = await self._db(
                        "save_source",
                        self.run["trip_id"],
                        self.run["id"],
                        self.run["generation"],
                        SourceNote(
                            external_id=identity,
                            title=feed.get("title", ""),
                            read_status="failed",
                            error_code=record["error_code"],
                        ),
                        query=feed.get("query", ""),
                        purpose=feed.get("purpose", "main"),
                    )
                    if failed_source is None:
                        raise self._stopped("run_changed")
                    record["source_id"] = failed_source["id"]
                if not await self._db(
                    "save_run_item",
                    self.run["id"],
                    self.run["generation"],
                    "note:" + identity,
                    record,
                ):
                    raise self._stopped("run_changed")
                if identity not in identities:
                    identities.append(identity)
                if not await self._db(
                    "save_run_item",
                    self.run["id"],
                    self.run["generation"],
                    "collection:notes",
                    identities,
                ):
                    raise self._stopped("run_changed")
                counts, replies_used = await self._progress(identities)
        counts, _ = await self._progress(identities)
        return {**counts, **({"stop_reason": stop_reason} if stop_reason else {})}
