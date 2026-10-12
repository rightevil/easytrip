"""Synthetic labels here test arithmetic; they are not real human acceptance."""

import importlib.util
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from easytrip.domain.discovery import Evidence, PlaceMention, SourceComment, SourceNote
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database

spec = importlib.util.spec_from_file_location("p2_review", Path(__file__).resolve().parents[2] / "scripts" / "p2_review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def corpus(tmp_path, *, notes=20, comments_per_note=5, judged=100):
    database = Database(tmp_path / "review.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    run = database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
    run = database.claim_run()
    ordinal = 0
    for index in range(notes):
        note = database.save_source(trip["id"], run["id"], run["generation"], SourceNote(external_id=f"private-note-{index}", title="西湖散步", text="西湖散步，评论包括景色和排队。 @private_author https://private.test/path?xsec_token=private-url-token\nCookie=private-cookie-value", images=["https://private.test/image?token=private-image-token"], author_key="a" * 64), query="西湖")
        mention = PlaceMention(id=f"mention-{index}", source_id=note["id"], name="西湖", city="杭州", category="play", quote="西湖散步")
        evidence = []
        for count in range(comments_per_note):
            comment = database.save_source(trip["id"], run["id"], run["generation"], SourceComment(external_id=f"private-comment-{index}-{count}", parent_source_id=note["id"], text="西湖景色好，但西湖排队久。\napi_key=private-api-value", author_key="b" * 64), query="西湖")
            attempted = ordinal < judged
            for dimension, stance, quote in (("景色", "positive", "西湖景色好"), ("排队", "negative", "西湖排队久")):
                evidence.append(Evidence(id=f"evidence-{ordinal}-{dimension}", source_id=comment["id"], mention_id=mention.id if attempted else None, object_text="西湖" if attempted else "", dimension=dimension, nature="experience" if attempted else "unknown", stance=stance if attempted else "unknown", quote=quote, link_status="linked" if attempted else "unlinked", model_id="synthetic-model"))
            ordinal += 1
        database.save_discovery_batch(trip["id"], run["id"], run["generation"], mentions=[mention], evidence=evidence)
    return database.path, run["id"]


def human_labels(rows):
    result = []
    for index, row in enumerate(rows):
        item = json.loads(json.dumps(row))
        item["human"].update(reviewer_kind="human", reviewer="虚构单元测试标注者")
        if item["kind"] == "note":
            item["human"].update(checked=True, source_forms=["text"])
        else:
            item["human"].update(judgeability="judgeable", **dict.fromkeys(review.CHECKS, True), missed_judgements=0 if item["model_judged"] else 1, case_types=[["negation"], ["mixed"], ["reference"]][index % 3])
        result.append(item)
    return result


def labels_file(tmp_path, rows):
    path = tmp_path / "labels.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def test_export_is_read_only_private_and_completely_unlabelled(tmp_path):
    path, run_id = corpus(tmp_path)
    before = path.read_bytes()
    target = tmp_path / "template"
    metadata = review.export_review(path, run_id, target)
    assert metadata["selected_notes"] == 20
    assert metadata["selected_comments"] == 100
    content = (target / "review.jsonl").read_text(encoding="utf-8")
    for secret in ("private-note-", "private-comment-", "private_author", "private.test", "private-url-token", "private-image-token", "private-cookie-value", "private-api-value", "author_key", "a" * 64, "b" * 64):
        assert secret not in content
        assert secret not in (target / "review.md").read_text(encoding="utf-8")
    report = review.score_review(path, run_id, target / "review.jsonl", tmp_path / "blank-score")
    assert report["verdict"] == "not_passed"
    assert report["counts"]["unlabelled"] == 120
    assert report["accuracy"]["rate"] is None
    assert report["coverage"]["rate"] is None
    assert path.read_bytes() == before


def test_comment_denominators_do_not_count_two_evidence_rows_twice(tmp_path):
    path, run_id = corpus(tmp_path, judged=80)
    rows, _ = review.load_sample(path, run_id)
    reviewed = human_labels(rows)
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "score")
    assert report["verdict"] == "pass"
    assert report["accuracy"] == {"numerator": 80, "denominator": 80, "rate": 1.0}
    assert report["coverage"] == {"numerator": 80, "denominator": 100, "rate": 0.8}
    assert report["counts"]["abstained_judgeable"] == 20
    assert report["counts"]["missed_judgements"] == 20


def test_omitted_objects_reduce_accuracy_even_when_existing_rows_are_correct(tmp_path):
    path, run_id = corpus(tmp_path)
    rows, _ = review.load_sample(path, run_id)
    reviewed = human_labels(rows)
    for row in [item for item in reviewed if item["kind"] == "comment"][:11]:
        row["human"].update(missed_judgements=1, error_types=["遗漏第二对象/主要维度"])
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "score")
    assert report["accuracy"] == {"numerator": 89, "denominator": 100, "rate": 0.89}
    assert report["counts"]["missed_judgements"] == 11
    assert report["error_types"]["遗漏第二对象/主要维度"] == 11
    assert report["verdict"] == "not_passed"


def test_unable_is_reported_and_excluded_from_judgeable_denominators(tmp_path):
    path, run_id = corpus(tmp_path, notes=1, comments_per_note=5, judged=4)
    rows, _ = review.load_sample(path, run_id)
    reviewed = human_labels(rows)
    comments = [item for item in reviewed if item["kind"] == "comment"]
    judged = next(item for item in comments if item["model_judged"])
    judged["human"].update(judgeability="unable", notes="指代对象没有足够上下文")
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "score")
    assert report["counts"]["unable"] == 1
    assert report["counts"]["model_judged_unable"] == 1
    assert report["accuracy"]["denominator"] == 3
    assert report["coverage"] == {"numerator": 3, "denominator": 4, "rate": 0.75}
    assert report["verdict"] == "not_passed"


def test_missing_comment_or_assistant_labels_cannot_pass(tmp_path):
    path, run_id = corpus(tmp_path)
    rows, _ = review.load_sample(path, run_id)
    reviewed = human_labels(rows)
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed[:-1]), tmp_path / "missing-score")
    assert report["verdict"] == "not_passed"
    assert report["counts"]["missing_rows"] == 1
    assert report["counts"]["unlabelled"] == 1
    for item in reviewed:
        item["human"]["reviewer_kind"] = "assistant"
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "assistant-score")
    assert report["verdict"] == "not_passed"
    assert report["counts"]["invalid_or_nonhuman_labels"] == 120
    assert report["accuracy"]["denominator"] == 0


def test_all_unable_or_tampered_predictions_cannot_manufacture_success(tmp_path):
    path, run_id = corpus(tmp_path)
    rows, _ = review.load_sample(path, run_id)
    reviewed = human_labels(rows)
    for item in reviewed:
        if item["kind"] == "comment":
            item["human"].update(judgeability="unable", notes="不足以确定对象")
    report = review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "unable-score")
    assert report["counts"]["unable"] == 100
    assert report["accuracy"]["rate"] is None
    assert report["coverage"]["rate"] is None
    assert report["verdict"] == "not_passed"
    reviewed[-1]["model"]["evidence"][0]["object_text"] = "改写结果"
    with pytest.raises(ValueError, match="原文和模型结果不可改写"):
        review.score_review(path, run_id, labels_file(tmp_path, reviewed), tmp_path / "tampered-score")


def test_whole_trip_judgement_does_not_require_a_poi_link(tmp_path):
    path, run_id = corpus(tmp_path, notes=1, comments_per_note=1, judged=0)
    with sqlite3.connect(path) as connection:
        for row in connection.execute("SELECT id,payload_json FROM evidence").fetchall():
            payload = json.loads(row[1])
            payload.update(object_text="整体行程", dimension="节奏", nature="suggestion", stance="negative", link_status="unlinked", mention_id=None)
            connection.execute("UPDATE evidence SET payload_json=? WHERE id=?", (json.dumps(payload), row[0]))
    rows, _ = review.load_sample(path, run_id)
    comment = next(row for row in rows if row["kind"] == "comment")
    assert comment["model_judged"] is True
    assert comment["model_poi_linked"] is False


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"link_status": "linked", "mention_id": "mention-0"}, True),
        ({}, True),
        ({"nature": "question", "stance": "question"}, True),
        ({"object_text": ""}, False),
        ({"object_text": "未知对象"}, False),
        ({"dimension": "unknown"}, False),
        ({"link_status": "uncertain"}, False),
        ({"citation_kind": "image", "image_index": 1}, False),
    ],
    ids=["neutral-linked", "neutral-unlinked", "question", "empty-object",
         "unknown-object", "unknown-dimension", "uncertain", "image"],
)
def test_review_main_metric_is_independent_of_nature_and_stance(tmp_path, fields, expected):
    path, run_id = corpus(tmp_path, notes=1, comments_per_note=1, judged=0)
    with sqlite3.connect(path) as connection:
        for identity, encoded in connection.execute("SELECT source_id,snapshot_json FROM source_snapshots").fetchall():
            snapshot = json.loads(encoded)
            if snapshot["kind"] == "comment":
                snapshot["text"] = "西湖门票15元。西湖门票免费吗？"
                connection.execute("UPDATE source_snapshots SET snapshot_json=? WHERE source_id=?", (json.dumps(snapshot), identity))
        for identity, encoded in connection.execute("SELECT id,payload_json FROM evidence").fetchall():
            item = json.loads(encoded)
            item.update(object_text="西湖", dimension="门票费用", nature="unknown", stance="unknown", quote="西湖门票15元", mention_id=None, link_status="unlinked")
            item.update(fields)
            if item["nature"] == "question":
                item["quote"] = "西湖门票免费吗？"
            connection.execute("UPDATE evidence SET payload_json=? WHERE id=?", (json.dumps(item), identity))
    rows, _ = review.load_sample(path, run_id)
    comment = next(row for row in rows if row["kind"] == "comment")
    assert comment["model_judged"] is expected
    report = review.score_review(path, run_id, labels_file(tmp_path, human_labels(rows)), tmp_path / "score")
    assert report["accuracy"]["denominator"] == int(expected)
    assert report["coverage"] == {"numerator": int(expected), "denominator": 1, "rate": float(expected)}
    assert report["counts"]["abstained_judgeable"] == int(not expected)


def test_bad_label_shapes_and_disguised_machine_reviewers_are_not_human_labels():
    note = {"kind": "note"}
    comment = {"kind": "comment"}
    assert not review.labelled({"reviewer_kind": "human", "reviewer": "助手标注", "checked": True, "source_forms": [{}]}, note)
    assert not review.labelled({"reviewer_kind": "human", "reviewer": "用户", "case_types": [[]]}, comment)
    assert not review.labelled({"reviewer_kind": "human", "reviewer": "assistant", "checked": True, "source_forms": ["text"]}, note)


@pytest.mark.parametrize("text", ["MODEL_API_KEY=private", "AMAP_WEB_SERVICE_KEY=private", '"AMAP_JS_SECURITY_CODE":"private"', "FTP://private.test/secret", "data:private"])
def test_prefixed_credentials_and_other_private_url_schemes_are_redacted(text):
    assert "private" not in review.clean_text(text)
