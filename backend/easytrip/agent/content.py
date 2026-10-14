"""Persist per-note analysis, reuse prior evidence, and publish bounded draft results."""

import hashlib
import json
from collections import Counter

from ..domain.evidence_metrics import object_dimension_judged
from ..integrations.model import TOKEN_BUDGETS
from ..integrations.xhs import ToolFailure
from ..services.collection import CollectionService
from ..services.evidence import NoteExtraction, assemble_candidates, extract_note

# Bump when extraction prompts or semantic validation change.
EXTRACTION_CONTRACT = "p2-evidence-contextual-nature-v8"


def note_hash(note):
    return hashlib.sha256(
        json.dumps(
            {"contract": EXTRACTION_CONTRACT, "note": note},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


async def records(runner, run):
    result, seen = [], set()
    for identity in await runner.db("get_run_item", run["id"], "collection:notes", []):
        record = await runner.db("get_run_item", run["id"], "note:" + identity)
        if record and record.get("status") == "ready":
            result.append(record)
            seen.add(record["note"]["source_id"])
    for inherited in await runner.db(
        "get_run_item", run["id"], "collection:inherited", []
    ):
        record = inherited["note_record"]
        source_id = record["note"]["source_id"]
        if source_id in seen:
            continue
        copied = await runner.db(
            "reuse_note_sources",
            run["trip_id"],
            run["id"],
            run["generation"],
            inherited["run_id"],
            source_id,
        )
        if copied is None:
            from .discovery import DiscoveryStopped

            raise DiscoveryStopped("run_changed")
        seen.add(source_id)
        result.append({**record, "reused_from_run": inherited["run_id"]})
    return result


async def persist_extraction(runner, run, result):
    data = NoteExtraction.model_validate(
        {
            key: value
            for key, value in result["data"].items()
            if key != "activity_validation"
        }
    ).model_dump(mode="json")
    return await runner.db(
        "save_discovery_batch",
        run["trip_id"],
        run["id"],
        run["generation"],
        **data,
        submitted_images=result.get("submitted_images", []),
    )


async def inherited_extraction(runner, run, note):
    """Use the nearest compatible analysis, independently of source collection ancestry."""
    from ..storage.database import NotFound

    parent_id = run.get("params", {}).get("parent_run_id")
    visited = {run["id"]}
    digest = note_hash(note)
    while parent_id and parent_id not in visited:
        visited.add(parent_id)
        try:
            parent = await runner.db("get_run", parent_id, run["trip_id"])
        except NotFound:
            break
        if (
            parent["kind"] != "discover"
            or parent["request_version"] != run["request_version"]
        ):
            break
        previous = await runner.db(
            "get_run_item", parent_id, "extract:" + note["source_id"]
        )
        if (
            previous
            and previous.get("status") == "passed"
            and previous.get("note_hash") == digest
            and previous.get("model_id") == runner.settings.model_id
        ):
            return {**previous, "reused_from_run": parent_id}
        parent_id = parent.get("params", {}).get("parent_run_id")
    return None


async def analyse(runner, run, snapshot, client):
    from .discovery import DiscoveryPaused, DiscoveryStopped

    for record in await records(runner, run):
        note = record["note"]
        key = "extract:" + note["source_id"]
        result = await runner.db("get_run_item", run["id"], key)
        if result is None and record.get("reused_from_run"):
            result = await inherited_extraction(runner, run, note)
        if result is None:
            digest = note_hash(note)
            cached, before, after = await runner.model_hooks(run, key)
            if cached is not None:
                actions = await runner.db("list_actions", run["id"])
                successful = next(
                    action
                    for action in actions
                    if action["action_key"].startswith(key + ":model:")
                    and action["status"] == "completed"
                    and (action["result"] or {}).get("model_data") == cached
                )
                result = {
                    "status": "passed",
                    "data": cached,
                    "submitted_images": successful["usage"].get("image_context", []),
                    "coverage": {
                        "submission_details_unknown": True,
                        "replies_submitted": sum(
                            bool(item.get("reply_to_source_id"))
                            and item["source_id"]
                            in {row["source_id"] for row in cached["evidence"]}
                            for item in note["comments"]
                        ),
                    },
                    "model_id": successful["params"].get("model_id"),
                    # A recovered legacy response must not become a current-contract cache.
                    "note_hash": successful["params"].get("input_hash"),
                }
            else:
                current = await runner.db("get_run", run["id"])
                remaining = max(
                    0, run["budget"]["max_images"] - current["usage"]["images"]
                )
                reply_used = 0
                for previous_record in await records(runner, run):
                    previous_result = await runner.db(
                        "get_run_item",
                        run["id"],
                        "extract:" + previous_record["note"]["source_id"],
                    )
                    if previous_result and not previous_result.get("reused_from_run"):
                        reply_used += previous_result.get("coverage", {}).get(
                            "replies_submitted", 0
                        )

                physical_model_id = runner.settings.model_id

                async def versioned_before(info):
                    return await before(
                        {**info, "input_hash": digest, "model_id": physical_model_id}
                    )

                output = await extract_note(
                    client,
                    runner.settings,
                    note,
                    max_images=min(3, remaining),
                    comment_limit=run["budget"]["comment_limit"],
                    reply_limit=max(0, run["budget"]["reply_limit"] - reply_used)
                    if run["budget"]["include_replies"]
                    else 0,
                    before_request=versioned_before,
                    after_request=after,
                )
                if output["status"] != "passed":
                    if output.get("error") == "request_not_allowed":
                        raise DiscoveryStopped("model_budget_exhausted")
                    if any(item.get("result_unknown") for item in output["requests"]):
                        raise DiscoveryPaused("model_result_unknown")
                    # Known invalid output stays visible; do not charge another round automatically.
                    await runner.db(
                        "save_run_item",
                        run["id"],
                        run["generation"],
                        key,
                        {
                            "status": "failed",
                            "error_code": "model_output_invalid",
                            "coverage": output["coverage"],
                        },
                    )
                    continue
                result = {
                    key: value
                    for key, value in output.items()
                    if key not in {"requests", "usage"}
                }
                result["note_hash"] = digest
        if result.get("status") != "passed":
            continue
        if not await runner.db(
            "save_run_item", run["id"], run["generation"], key, result
        ):
            raise DiscoveryStopped("run_changed")
        if await persist_extraction(runner, run, result) is None:
            raise DiscoveryStopped("run_changed")
        await publish(runner, run, snapshot)
    return await analysis_progress(runner, run)


async def analysed(runner, run):
    result = []
    for record in await records(runner, run):
        extraction = await runner.db(
            "get_run_item", run["id"], "extract:" + record["note"]["source_id"]
        )
        if extraction and extraction.get("status") == "passed":
            result.append((record, extraction))
    return result


async def analysis_progress(runner, run):
    reused_records = [
        record for record in await records(runner, run) if record.get("reused_from_run")
    ]
    rows = await analysed(runner, run)
    current_rows = [
        (record, extraction)
        for record, extraction in rows
        if not record.get("reused_from_run")
    ]
    comments, judged, images, evidence = set(), set(), set(), set()
    for record, extraction in current_rows:
        comment_ids = {item["source_id"] for item in record["note"]["comments"]}
        for item in extraction["data"]["evidence"]:
            evidence.add(item["id"])
            if item["source_id"] in comment_ids:
                comments.add(item["source_id"])
                if object_dimension_judged(item):
                    judged.add(item["source_id"])
        images.update(
            (item["source_id"], item["image_index"])
            for item in extraction["data"]["image_observations"]
        )
    current = await runner.db("get_run", run["id"])
    updates = {
        "notes_analyzed": len(current_rows),
        "comments_analyzed": len(comments),
        "comments_judged": len(judged),
        "images_analyzed_unique": len(images),
        "images_unanalyzed": max(0, current["usage"]["images_obtained"] - len(images)),
        "evidence_count": len(evidence),
        "judgement_rows": sum(
            len(extraction["data"]["evidence"]) for _, extraction in current_rows
        ),
    }
    await runner.db("set_discovery_progress", run["id"], run["generation"], updates)
    reused_comments, reused_judged, reused_images = set(), set(), set()
    reused_rows = [
        (record, extraction)
        for record, extraction in rows
        if record.get("reused_from_run")
    ]
    for record, extraction in reused_rows:
        comment_ids = {item["source_id"] for item in record["note"]["comments"]}
        for item in extraction["data"]["evidence"]:
            if item["source_id"] in comment_ids:
                reused_comments.add(item["source_id"])
                if object_dimension_judged(item):
                    reused_judged.add(item["source_id"])
        reused_images.update(
            (item["source_id"], item["image_index"])
            for item in extraction["data"]["image_observations"]
        )
    reused_image_count = sum(
        record.get("coverage", {}).get("images_received", len(record["note"]["images"]))
        for record in reused_records
    )
    return {
        **updates,
        "notes_reused": len(reused_records),
        "comments_reused": sum(
            len(record["note"]["comments"]) for record in reused_records
        ),
        "notes_reused_analyzed": len(reused_rows),
        "comments_reused_analyzed": len(reused_comments),
        "comments_reused_judged": len(reused_judged),
        "images_reused_obtained": reused_image_count,
        "images_reused_analyzed_unique": len(reused_images),
        "images_reused_unanalyzed": max(0, reused_image_count - len(reused_images)),
    }


async def assembled(runner, run, snapshot):
    rows = await analysed(runner, run)
    sources = await runner.db(
        "list_run_sources", run["trip_id"], run["id"], include_private=True
    )
    searched = await runner.db("get_run_item", run["id"], "gaps:searched", [])
    return assemble_candidates(
        run["id"],
        snapshot.request.model_dump(mode="json"),
        run["checkpoint"].get("understanding", {}),
        [extraction for _, extraction in rows],
        sources,
        searched_purposes=searched,
    )


async def evaluate(runner, run, snapshot, client):
    from .discovery import DiscoveryPaused, DiscoveryStopped

    queries = await runner.db("get_run_item", run["id"], "gaps:plan")
    if queries is None:
        initial = await assembled(runner, run, snapshot)
        queries = initial["gap_queries"][
            : min(2, run["budget"]["reserved_gap_searches"])
        ]
        if not await runner.db(
            "save_run_item", run["id"], run["generation"], "gaps:plan", queries
        ):
            raise DiscoveryStopped("run_changed")
    if queries:
        actions = await runner.db("list_actions", run["id"])
        remaining = max(
            0,
            run["budget"]["reserved_gap_searches"]
            - sum(
                action["kind"] == "search" and action["params"].get("gap", False)
                for action in actions
            ),
        )
        pending = []
        for query in queries:
            previous = [
                action
                for action in actions
                if action["kind"] == "search"
                and action["params"].get("query")
                == runner.settings.redact(query["query"])
            ]
            if any(
                action["status"] == "completed" and action["result"] is not None
                for action in previous
            ):
                pending.append(
                    query
                )  # Restore a completed response if its feed checkpoint was interrupted.
            elif not previous and remaining > 0:
                pending.append(query)
                remaining -= 1
            # Failed or unknown physical searches consume the finite allowance; never replay them.
        service = CollectionService(runner, run, client)
        try:
            if pending:
                discovered = await service.discover(pending, gap=True)
                await service.fetch(discovered["feeds"])
            else:
                await service.fetch(
                    await runner.db("get_run_item", run["id"], "collection:feeds", [])
                )
        except ToolFailure:
            raise DiscoveryPaused("source_request_failed") from None
        finally:
            actions = await runner.db("list_actions", run["id"])
            # Persist physical attempts even when collection was cancelled before its checkpoint.
            attempted = [
                query["purpose"]
                for query in queries
                if any(
                    action["kind"] == "search"
                    and action["params"].get("query")
                    == runner.settings.redact(query["query"])
                    for action in actions
                )
            ]
            await runner.db(
                "save_run_item",
                run["id"],
                run["generation"],
                "gaps:searched",
                attempted,
            )
    # Ready supplementary notes and successful model ledgers must resume even after gaps close.
    await analyse(runner, run, snapshot, client)
    return await publish(runner, run, snapshot)


async def publish(runner, run, snapshot, *, stop_reason=None):
    current = await runner.db("get_run", run["id"])
    if current["generation"] != run["generation"] or not await runner.db(
        "run_is_current", run["id"], run["generation"]
    ):
        return None
    content = await assembled(runner, current, snapshot)
    saved = await runner.db(
        "save_discovery_batch",
        run["trip_id"],
        run["id"],
        run["generation"],
        candidates=content["candidates"] + content["directions"],
        gaps=content["gaps"],
        replace_candidates=True,
    )
    if saved is None:
        return None
    progress = await analysis_progress(runner, current)
    current = await runner.db("get_run", run["id"])
    usage = current["usage"]
    reasons = [stop_reason] if stop_reason else []
    if usage["notes_obtained"] < run["budget"]["target_notes"]:
        reasons.append("note_target_not_reached")
    if (
        usage["comments_analyzed"] < usage["comments_selected"]
        or progress["comments_reused_analyzed"] < progress["comments_reused"]
    ):
        reasons.append("comments_partly_unanalysed")
    counts = Counter(row["kind"] for row in content["candidates"])
    if not counts["place"]:
        reasons.append("no_source_backed_candidates")
    if (
        not content["effective_request"]["destination"]
        and len(content["directions"]) < 2
    ):
        reasons.append("insufficient_exploration_directions")
    activities_rejected = 0
    for record in await records(runner, run):
        extraction = await runner.db(
            "get_run_item", run["id"], "extract:" + record["note"]["source_id"]
        )
        if extraction and extraction.get("error_code") == "model_output_invalid":
            reasons.append("model_output_invalid")
        if extraction and extraction.get("status") == "passed":
            activities_rejected += (
                extraction["data"].get("activity_validation", {}).get("rejected", 0)
            )
    if activities_rejected:
        reasons.append("activities_partly_unanalysed")
    queries = await runner.db("get_run_item", run["id"], "gaps:plan", [])
    actions = await runner.db("list_actions", run["id"])
    if any(
        not any(
            action["kind"] == "search"
            and action["status"] == "completed"
            and action["result"] is not None
            and action["params"].get("query") == runner.settings.redact(query["query"])
            for action in actions
        )
        for query in queries
    ):
        reasons.append("gap_search_unfinished")
    summary = {
        "summary": (
            f"本轮新增取得{usage['notes_obtained']}篇笔记，分析{usage['comments_analyzed']}条评论；"
            f"复用{progress['notes_reused']}篇已有笔记，取得{counts['place']}个待定位地点、"
            f"{counts['generic']}条泛称线索、{counts['direction']}个探索方向。"
            + (
                f"其中{activities_rejected}条活动未通过校验，已保留独立校验通过的地点与评论。"
                if activities_rejected
                else ""
            )
        ),
        "understanding": current["checkpoint"].get("understanding"),
        "directions": content["directions"],
        "gaps": content["gaps"],
        "coverage": {
            **{
                key: usage[key]
                for key in (
                    "notes_obtained",
                    "notes_failed",
                    "comments_obtained",
                    "comments_selected",
                    "comments_analyzed",
                    "comments_judged",
                    "notes_analyzed",
                    "images_obtained",
                    "images_analyzed_unique",
                    "images_unanalyzed",
                    "evidence_count",
                    "judgement_rows",
                )
            },
            **{
                key: progress[key]
                for key in (
                    "notes_reused",
                    "comments_reused",
                    "notes_reused_analyzed",
                    "comments_reused_analyzed",
                    "comments_reused_judged",
                    "images_reused_obtained",
                    "images_reused_analyzed_unique",
                    "images_reused_unanalyzed",
                )
            },
            "targets": {
                "notes": run["budget"]["target_notes"],
                "comment_limit": run["budget"]["comment_limit"],
                "reply_limit": run["budget"]["reply_limit"],
            },
            "video_unanalysed": any(
                record["note"].get("video_unanalysed")
                for record in await records(runner, run)
            ),
            "activities_rejected": activities_rejected,
        },
        "model": {
            "model_id": runner.settings.model_id,
            "schema_version": "p2-v1",
            "token_budgets": TOKEN_BUDGETS,
            "thinking_enabled": True,
            "semantic_quality": "not_validated",
        },
        "source_capability": await runner.db(
            "get_run_item", run["id"], "source:capability"
        ),
        "trip_usage": await runner.db("trip_usage", run["trip_id"]),
        "partial_reasons": list(dict.fromkeys(reasons)),
    }
    await runner.db("save_discovery_summary", run["id"], run["generation"], summary)
    return summary
