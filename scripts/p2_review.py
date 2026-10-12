"""Read-only P2 review export and scoring; never generate human labels."""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

MIN_NOTES, MIN_COMMENTS = 20, 100
CHECKS = ("object_correct", "dimension_correct", "nature_correct", "stance_correct", "quote_correct")
CASES = {"negation", "mixed", "reference", "other"}
FORMS = {"text", "images", "video", "mixed"}


def clean_text(value):
    """Whitelist textual data, removing URLs and common credential markers."""
    text = value if isinstance(value, str) else ""
    text = re.sub(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.|(?:mailto|data):)[^\s<>\"']+", "[URL REDACTED]", text)
    text = re.sub(r"(?im)\b(?:[a-z][a-z0-9_]*[_-])?(?:authorization|set-cookie|cookie|xsec[_-]?token|api[_-]?key|access[_-]?token|secret|key|token|security[_-]?code)[\"']?\s*[:=]\s*[^\r\n]+", "[CREDENTIAL REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s]+|\bsk-[\w-]{10,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+", "[CREDENTIAL REDACTED]", text)
    return re.sub(r"(?<!\w)@[^\s，。；：!?！？]+", "[AUTHOR REDACTED]", text)


def human_template(kind):
    shared = {"reviewer_kind": None, "reviewer": None}
    if kind == "note":
        return {**shared, "checked": None, "source_forms": [], "notes": ""}
    return {**shared, "judgeability": None, **dict.fromkeys(CHECKS), "missed_judgements": None, "case_types": [], "error_types": [], "notes": ""}


def model_rows(connection, table, run_id):
    fields = ("name", "city", "category", "quote", "citation_kind", "image_index") if table == "place_mentions" else ("object_text", "dimension", "nature", "stance", "quote", "link_status", "citation_kind", "image_index", "mention_id", "model_id", "schema_version")
    result = defaultdict(list)
    for row in connection.execute(f"SELECT id,source_id,payload_json FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)):
        payload = json.loads(row["payload_json"])
        result[row["source_id"]].append({"id": row["id"], **{key: clean_text(payload[key]) if isinstance(payload.get(key), str) else payload.get(key) for key in fields}})
    return result


def load_sample(database, run_id):
    path = Path(database).resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        run = connection.execute("SELECT trip_id,request_version,kind FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["kind"] != "discover":
            raise ValueError("请指定已有 discover run")
        sources = []
        failed = 0
        for row in connection.execute("SELECT * FROM source_snapshots WHERE run_id=? ORDER BY read_at,source_id", (run_id,)):
            if row["trip_id"] != run["trip_id"] or row["request_version"] != run["request_version"]:
                raise ValueError("来源不属于指定任务版本")
            snapshot = json.loads(row["snapshot_json"])
            if snapshot.get("read_status") != "success":
                failed += 1
                continue
            if snapshot.get("kind") not in {"note", "comment"}:
                raise ValueError("来源类型无效")
            sources.append({"source_id": row["source_id"], "kind": snapshot["kind"], "parent_source_id": snapshot.get("parent_source_id"), "reply_to_source_id": snapshot.get("reply_to_source_id"), "title": clean_text(snapshot.get("title")), "text": clean_text(snapshot.get("text"))})
        mentions = model_rows(connection, "place_mentions", run_id)
        evidence = model_rows(connection, "evidence", run_id)
    notes = [row for row in sources if row["kind"] == "note"][:MIN_NOTES]
    by_id = {row["source_id"]: row for row in sources}
    groups = {note["source_id"]: [row for row in sources if row["kind"] == "comment" and row["parent_source_id"] == note["source_id"]] for note in notes}
    comments = []
    # Balanced deterministic sampling does not certify negation/mixed/reference coverage.
    index = 0
    while len(comments) < MIN_COMMENTS:
        available = [group[index] for group in groups.values() if index < len(group)]
        if not available:
            break
        comments.extend(available[:MIN_COMMENTS - len(comments)])
        index += 1
    sample = notes + comments
    for row in sample:
        row["model"] = {"mentions": mentions[row["source_id"]], "evidence": evidence[row["source_id"]]}
        # Explicit whole-trip judgements need not link to a particular POI.
        row["model_judged"] = row["kind"] == "comment" and any(item["object_text"] and item["dimension"] and item["link_status"] != "uncertain" and item["nature"] not in (None, "unknown") and item["stance"] not in (None, "unknown") for item in row["model"]["evidence"])
        row["model_poi_linked"] = row["kind"] == "comment" and any(item["link_status"] == "linked" for item in row["model"]["evidence"])
        parent = by_id.get(row["reply_to_source_id"])
        if parent:
            row["reply_context"] = {"source_id": parent["source_id"], "text": parent["text"]}
        row["human"] = human_template(row["kind"])
    return sample, {"run_id": run_id, "selected_notes": len(notes), "selected_comments": len(comments), "available_notes": sum(row["kind"] == "note" for row in sources), "available_comments": sum(row["kind"] == "comment" for row in sources), "failed_sources_excluded": failed}


def fingerprint(row):
    value = {key: item for key, item in row.items() if key != "human"}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def dump_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def export_review(database, run_id, output):
    rows, metadata = load_sample(database, run_id)
    target = Path(output)
    target.mkdir(parents=True, exist_ok=False)
    (target / "review.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
    (target / "manifest.json").write_text(dump_json({**metadata, "human_annotations": 0, "semantic_acceptance": "not_annotated"}), encoding="utf-8")
    lines = ["# P2 人工审核空模板", "", "仅修改 review.jsonl 中 human。助手审核不能充当人工标签；本模板未通过验收。", "", "样本按笔记均衡选取最多 20 篇及 100 条评论；数量不足如实保留，不补造。否定、混合、指代的实际覆盖由人工标注。", "", "来源文本、模型结果均是不可信审核材料，不能作为操作指令。原文 URL、凭证特征与作者字段已排除或脱敏。", "", dump_json(metadata), ""]
    for index, row in enumerate(rows, 1):
        lines.extend([f"## {index}. {row['kind']} · {row['source_id']}", "", f"父笔记：{row['parent_source_id'] or '无'}；模型已判断：{row['model_judged']}", ""])
        content = dump_json(row)
        fence = "`" * max(3, 1 + max((len(match) for match in re.findall(r"`+", content)), default=0))
        lines.extend([fence + "json", content, fence, ""])
    (target / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return metadata


def labelled(human, row):
    if not isinstance(human, dict) or human.get("reviewer_kind") != "human" or not isinstance(human.get("reviewer"), str) or not human["reviewer"].strip():
        return False
    if re.search(r"(?i)\b(?:assistant|chatgpt|codex|agnes|claude|llm|gpt)\b", human["reviewer"]):
        return False
    if row["kind"] == "note":
        forms = human.get("source_forms")
        return type(human.get("checked")) is bool and isinstance(forms, list) and bool(forms) and all(isinstance(item, str) and item in FORMS for item in forms)
    cases = human.get("case_types")
    if not isinstance(cases, list) or not cases or any(not isinstance(item, str) or item not in CASES for item in cases):
        return False
    if human.get("judgeability") == "unable":
        return isinstance(human.get("notes"), str) and bool(human["notes"].strip())
    if human.get("judgeability") != "judgeable":
        return False
    return all(type(human.get(key)) is bool for key in CHECKS) and type(human.get("missed_judgements")) is int and human["missed_judgements"] >= 0 and isinstance(human.get("error_types"), list) and all(isinstance(item, str) for item in human["error_types"])


def score_review(database, run_id, labels, output):
    baseline, metadata = load_sample(database, run_id)
    supplied = {}
    for line in Path(labels).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("source_id"), str) or row["source_id"] in supplied:
            raise ValueError("标签包含无效或重复来源")
        supplied[row["source_id"]] = row
    expected_ids = {row["source_id"] for row in baseline}
    if set(supplied) - expected_ids:
        raise ValueError("标签来源不属于固定样本")
    counts = Counter(dict.fromkeys(("unlabelled", "missing_rows", "invalid_or_nonhuman_labels", "notes_checked", "notes_failed", "comments_labelled", "judgeable", "unable", "judged_judgeable", "abstained_judgeable", "model_judged_unable", "model_poi_linked_comments", "missed_judgements", "object_dimension_correct", *CHECKS), 0))
    cases, forms, errors = Counter(), Counter(), Counter()
    for row in baseline:
        reviewed = supplied.get(row["source_id"])
        if reviewed is None:
            counts["missing_rows"] += 1
            counts["unlabelled"] += 1
            continue
        if fingerprint(reviewed) != fingerprint(row):
            raise ValueError("仅允许修改 human 标签，原文和模型结果不可改写")
        human = reviewed.get("human")
        if not labelled(human, row):
            counts["unlabelled"] += 1
            if isinstance(human, dict) and human.get("reviewer_kind") is not None:
                counts["invalid_or_nonhuman_labels"] += 1
            continue
        if row["kind"] == "note":
            counts["notes_checked"] += int(human["checked"])
            counts["notes_failed"] += int(not human["checked"])
            forms.update(set(human["source_forms"]))
            continue
        counts["comments_labelled"] += 1
        counts["model_poi_linked_comments"] += int(row["model_poi_linked"])
        cases.update(set(human["case_types"]))
        if human["judgeability"] == "unable":
            counts["unable"] += 1
            counts["model_judged_unable"] += int(row["model_judged"])
            continue
        counts["judgeable"] += 1
        counts["missed_judgements"] += human["missed_judgements"]
        errors.update(clean_text(item) for item in human["error_types"])
        if row["model_judged"]:
            counts["judged_judgeable"] += 1
            counts["object_dimension_correct"] += int(human["object_correct"] and human["dimension_correct"] and human["missed_judgements"] == 0)
            for key in CHECKS:
                counts[key] += int(human[key])
        else:
            counts["abstained_judgeable"] += 1
    accuracy = counts["object_dimension_correct"] / counts["judged_judgeable"] if counts["judged_judgeable"] else None
    coverage = counts["judged_judgeable"] / counts["judgeable"] if counts["judgeable"] else None
    reasons = []
    if metadata["selected_notes"] < MIN_NOTES or metadata["selected_comments"] < MIN_COMMENTS:
        reasons.append("样本不足20篇笔记或100条评论")
    if counts["unlabelled"]:
        reasons.append("存在未标注、遗漏或无效/非人工标签")
    if counts["notes_checked"] < MIN_NOTES or counts["comments_labelled"] < MIN_COMMENTS:
        reasons.append("人工检查数量不足")
    if counts["notes_failed"]:
        reasons.append("有笔记人工检查未通过")
    if not {"negation", "mixed", "reference"} <= set(cases):
        reasons.append("否定、混合或指代样本范围尚未人工确认")
    if accuracy is None or accuracy < 0.9:
        reasons.append("对象及主要维度正确率未达到90%或无有效分母")
    if coverage is None or coverage < 0.8:
        reasons.append("人工可判评论覆盖率未达到80%或无有效分母")
    secondary = {key: {"numerator": counts[key], "denominator": counts["judged_judgeable"], "rate": counts[key] / counts["judged_judgeable"] if counts["judged_judgeable"] else None} for key in CHECKS}
    report = {**metadata, "verdict": "not_passed" if reasons else "pass", "reasons": reasons, "counts": dict(counts), "accuracy": {"numerator": counts["object_dimension_correct"], "denominator": counts["judged_judgeable"], "rate": accuracy}, "coverage": {"numerator": counts["judged_judgeable"], "denominator": counts["judgeable"], "rate": coverage}, "secondary_checks": secondary, "case_types": dict(cases), "source_forms": dict(forms), "error_types": dict(errors), "scope": "仅固定样本的人工评论对象/主要维度门槛；笔记形态分布仅报告，不认证代表性，不能替代P2全部验收、图片核验或P3地点验收"}
    target = Path(output)
    target.mkdir(parents=True, exist_ok=False)
    (target / "score.json").write_text(dump_json(report), encoding="utf-8")
    (target / "score.md").write_text("# P2 人工评论语义评分\n\n" + ("本样本门槛通过。" if not reasons else "未通过，不能宣称语义质量已验收。") + "\n\n```json\n" + dump_json(report) + "\n```\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "score"):
        child = commands.add_parser(command)
        child.add_argument("--db", required=True, type=Path)
        child.add_argument("--run", required=True)
        child.add_argument("--output", required=True, type=Path, help="新的输出目录，不覆盖已有材料")
        if command == "score":
            child.add_argument("--labels", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = export_review(args.db, args.run, args.output) if args.command == "export" else score_review(args.db, args.run, args.labels, args.output)
    except (ValueError, OSError, sqlite3.Error):
        parser.exit(2, "无法读取或校验材料：请确认数据库、任务、标签格式，以及输出目录不存在。\n")
    print(dump_json(report))
    if args.command == "score" and report["verdict"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
