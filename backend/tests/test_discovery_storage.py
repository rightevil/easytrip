import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from easytrip.domain.discovery import (
    Activity,
    CandidateDraft,
    Evidence,
    Gap,
    ImageObservation,
    PlaceMention,
    SourceComment,
    SourceNote,
)
from easytrip.domain.models import ArchiveTrip, CreateTrip, MessageCreate, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database, NotFound, OperationConflict


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "app.sqlite3")
    db.initialize()
    return db


def create(db):
    return db.create_trip(CreateTrip(operation_id=uuid4(), name="证据测试"))


def running(db, trip):
    db.enqueue_run(
        trip["id"],
        CreateRun(operation_id=uuid4(), expected_version=trip["request_version"]),
    )
    return db.claim_run()


def note(db, trip, run, **kwargs):
    return db.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceNote(
            external_id="note-1",
            title="杭州",
            text="西湖散步很舒服，午饭去桂花面馆。",
            **kwargs,
        ),
        query="杭州散步",
    )


def extraction(source, **kwargs):
    mention = PlaceMention(
        id="west-lake",
        source_id=source["id"],
        name="西湖",
        category="play",
        quote="西湖散步",
    )
    evidence = Evidence(
        id="lake-review",
        source_id=source["id"],
        mention_id=mention.id,
        object_text="西湖",
        dimension="散步",
        stance="positive",
        nature="experience",
        quote="西湖散步很舒服",
        link_status="linked",
    )
    candidate = CandidateDraft(
        name="西湖",
        category="play",
        city="杭州",
        mention_ids=[mention.id],
        evidence_ids=[evidence.id],
        **kwargs,
    )
    return mention, evidence, candidate


def save(db, trip, run, **kwargs):
    return db.save_discovery_batch(trip["id"], run["id"], run["generation"], **kwargs)


def test_p1_database_migrates_without_losing_data(tmp_path):
    path = tmp_path / "p1.sqlite3"
    initial = Path(__file__).parents[1] / "easytrip/storage/migrations/001_initial.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            initial.read_text(encoding="utf-8") + "\nPRAGMA user_version=1;"
        )
    db = Database(path)
    trip = create(db)
    message = db.add_message(
        trip["id"], MessageCreate(operation_id=uuid4(), content="想去散步")
    )
    run = running(db, trip)
    before = db.get_trip(trip["id"])
    db.initialize()
    assert db.get_trip(trip["id"]) == before
    assert db.get_trip(trip["id"])["messages"][0] == message
    assert db.get_run(run["id"])["generation"] == run["generation"]
    with db.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_migration_rolls_back_preserving_p1(tmp_path, monkeypatch):
    path = tmp_path / "p1.sqlite3"
    initial = Path(__file__).parents[1] / "easytrip/storage/migrations/001_initial.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            initial.read_text(encoding="utf-8") + "\nPRAGMA user_version=1;"
        )
    db = Database(path)
    trip = create(db)
    original_read = Path.read_text

    def corrupt_migration(file, *args, **kwargs):
        if file.name == "002_discovery.sql":
            return "CREATE TABLE incomplete(id TEXT); invalid SQL;"
        return original_read(file, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", corrupt_migration)
    with pytest.raises(sqlite3.OperationalError):
        db.initialize()
    assert db.get_trip(trip["id"]) == trip
    with db.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='incomplete'"
            ).fetchone()
            is None
        )


def test_source_identity_and_history_are_per_trip_and_run(database):
    trip = create(database)
    first_run = running(database, trip)
    source = note(database, trip, first_run, author_key="a" * 64)
    same = note(database, trip, first_run, author_key="a" * 64)
    assert source == same
    database.save_source(
        trip["id"],
        first_run["id"],
        first_run["generation"],
        SourceNote(
            external_id="note-1", title="杭州", text=source["text"], author_key="a" * 64
        ),
        query="午饭",
        purpose="meal_gap",
    )
    linked = database.list_run_sources(trip["id"], first_run["id"])
    assert len(linked) == 1
    assert linked[0]["queries"] == [
        {"query": "杭州散步", "purpose": "main"},
        {"query": "午饭", "purpose": "meal_gap"},
    ]
    assert "author_key" not in linked[0]
    assert (
        database.get_source(trip["id"], source["id"], include_private=True)[
            "author_key"
        ]
        == "a" * 64
    )
    second_run = running(database, trip)
    latest = database.save_source(
        trip["id"],
        second_run["id"],
        second_run["generation"],
        SourceNote(external_id="note-1", text="已编辑的新正文"),
    )
    assert latest["id"] == source["id"]
    assert latest["dedupe_confidence"] == "high"
    assert database.get_source(trip["id"], source["id"])["text"] == "已编辑的新正文"
    assert (
        database.get_source(trip["id"], source["id"], first_run["id"])["text"]
        == source["text"]
    )
    other = create(database)
    other_run = running(database, other)
    other_source = note(database, other, other_run)
    assert other_source["id"] != source["id"]
    with pytest.raises(NotFound):
        database.get_source(other["id"], source["id"])


def test_missing_external_id_is_honestly_low_confidence(database):
    trip = create(database)
    run = running(database, trip)
    body = SourceNote(text="没有外部ID的正文")
    first = database.save_source(trip["id"], run["id"], run["generation"], body)
    second = database.save_source(trip["id"], run["id"], run["generation"], body)
    changed = database.save_source(
        trip["id"], run["id"], run["generation"], SourceNote(text="不同正文")
    )
    assert first["id"] == second["id"] != changed["id"]
    assert first["external_id"] is None
    assert first["dedupe_confidence"] == "low"


def test_same_run_source_cannot_be_replaced(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    with pytest.raises(OperationConflict):
        database.save_source(
            trip["id"],
            run["id"],
            run["generation"],
            SourceNote(external_id="note-1", text="替换历史"),
        )
    assert (
        database.get_source(trip["id"], source["id"], run["id"])["text"]
        == source["text"]
    )


def test_comment_reply_requires_same_run_and_same_note(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    comment = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceComment(
            external_id="comment-1", parent_source_id=source["id"], text="排队很久"
        ),
    )
    reply = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceComment(
            external_id="reply-1",
            parent_source_id=source["id"],
            reply_to_source_id=comment["id"],
            text="周末去的吗？",
        ),
    )
    assert reply["reply_to_source_id"] == comment["id"]
    other_note = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceNote(external_id="note-2", text="另一篇"),
    )
    with pytest.raises(ValueError):
        database.save_source(
            trip["id"],
            run["id"],
            run["generation"],
            SourceComment(
                parent_source_id=other_note["id"],
                reply_to_source_id=comment["id"],
                text="错父笔记",
            ),
        )
    new_run = running(database, trip)
    with pytest.raises(NotFound):
        database.save_source(
            trip["id"],
            new_run["id"],
            new_run["generation"],
            SourceComment(parent_source_id=source["id"], text="本轮未读笔记"),
        )


def test_atomic_extraction_is_idempotent_and_resolves_historical_quotes(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    activity = Activity(
        mention_id=mention.id,
        source_id=source["id"],
        quote="西湖散步",
        description="散步",
    )
    gap = Gap(
        kind="meal",
        status="open",
        period="dinner",
        reason="缺晚餐",
        source_ids=[source["id"]],
        candidate_ids=[candidate.id],
    )
    result = save(
        database,
        trip,
        run,
        mentions=[mention],
        evidence=[evidence],
        candidates=[candidate],
        activities=[activity],
        gaps=[gap],
    )
    assert (
        save(
            database,
            trip,
            run,
            mentions=[mention],
            evidence=[evidence],
            candidates=[candidate],
            activities=[activity],
            gaps=[gap],
        )
        == result
    )
    assert database.list_candidates(trip["id"])["total"] == 1
    assert database.list_gaps(trip["id"])[0]["id"] == gap.id
    next_run = running(database, trip)
    database.save_source(
        trip["id"],
        next_run["id"],
        next_run["generation"],
        SourceNote(external_id="note-1", text="新内容"),
    )
    read = database.candidate_evidence(trip["id"], candidate.id)
    assert read[0]["quote"] == evidence.quote
    assert read[0]["source"]["text"] == source["text"]
    assert read[0]["request_version"] == trip["request_version"]


@pytest.mark.parametrize(
    "problem", ["source", "quote", "mention", "candidate", "gap", "replacement"]
)
def test_bad_batch_rolls_back_all_partial_outputs(database, problem):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    if problem == "source":
        mention = mention.model_copy(update={"source_id": "not-in-run"})
    elif problem == "quote":
        evidence = evidence.model_copy(update={"quote": "正文不存在的评价"})
    elif problem == "mention":
        evidence = evidence.model_copy(update={"mention_id": "wrong-mention"})
    elif problem == "candidate":
        candidate = candidate.model_copy(update={"evidence_ids": ["wrong-evidence"]})
    elif problem == "replacement":
        save(database, trip, run, mentions=[mention])
        mention = mention.model_copy(update={"name": "不同名字"})
    gap = Gap(
        kind="meal",
        reason="餐饮待确认",
        source_ids=["not-in-run"] if problem == "gap" else [],
    )
    with pytest.raises((ValueError, OperationConflict)):
        save(
            database,
            trip,
            run,
            mentions=[mention],
            evidence=[evidence],
            candidates=[candidate],
            gaps=[gap],
        )
    assert database.list_candidates(trip["id"])["total"] == 0
    assert database.list_gaps(trip["id"]) == []
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM place_mentions").fetchone()[
            0
        ] == (problem == "replacement")


@pytest.mark.parametrize(
    "stop", ["generation", "cancel", "request", "archive", "delete"]
)
def test_late_writes_cannot_publish_after_run_or_request_changes(database, stop):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    generation = run["generation"]
    if stop == "generation":
        generation -= 1
    elif stop == "cancel":
        database.cancel_run(run["id"], trip["id"], generation)
    elif stop == "request":
        database.update_trip(
            trip["id"], TripPatch(expected_version=1, name="新条件版本")
        )
    elif stop == "archive":
        database.archive_trip(
            trip["id"], ArchiveTrip(expected_version=1, archived=True)
        )
    else:
        database.delete_trip(trip["id"], 1)
    assert (
        database.save_source(
            trip["id"],
            run["id"],
            generation,
            SourceNote(external_id="late", text="迟到"),
        )
        is None
    )
    assert (
        database.save_discovery_batch(
            trip["id"],
            run["id"],
            generation,
            mentions=[mention],
            evidence=[evidence],
            candidates=[candidate],
        )
        is None
    )
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM sources WHERE external_id='late'"
            ).fetchone()[0]
            == 0
        )


def test_reads_select_current_latest_output_and_allow_explicit_history(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    save(
        database,
        trip,
        run,
        mentions=[mention],
        evidence=[evidence],
        candidates=[candidate],
    )
    newer = running(database, trip)
    source2 = note(database, trip, newer)
    m2 = PlaceMention(
        id="meal",
        source_id=source2["id"],
        name="桂花面馆",
        category="eat",
        quote="桂花面馆",
    )
    candidates = [
        CandidateDraft(name="桂花面馆", category="eat", mention_ids=[m2.id]),
        CandidateDraft(name="面馆分店", category="eat", mention_ids=[m2.id]),
    ]
    save(database, trip, newer, mentions=[m2], candidates=candidates)
    page = database.list_candidates(trip["id"], category="eat", limit=1, offset=1)
    assert page["total"] == 2 and page["items"][0]["id"] == candidates[1].id
    assert database.list_candidates(trip["id"], category="play")["total"] == 0
    assert (
        database.list_candidates(trip["id"], run["id"])["items"][0]["id"]
        == candidate.id
    )
    database.update_trip(trip["id"], TripPatch(expected_version=1, name="新版本"))
    assert database.list_candidates(trip["id"])["items"] == []
    assert database.list_candidates(trip["id"], run["id"])["total"] == 1
    other = create(database)
    with pytest.raises(NotFound):
        database.list_candidates(other["id"], run["id"])
    with pytest.raises(NotFound):
        database.candidate_evidence(other["id"], candidate.id)


def test_image_only_candidate_keeps_unverified_observation_and_location(database):
    trip = create(database)
    run = running(database, trip)
    source = note(
        database,
        trip,
        run,
        images=["https://example.test/1.jpg", "https://example.test/2.jpg"],
    )
    observation = ImageObservation(
        source_id=source["id"], image_index=2, text="图片显示：桂花面馆，可能是门头文字"
    )
    mention = PlaceMention(
        id="image-place",
        source_id=source["id"],
        name="桂花面馆",
        category="eat",
        quote="桂花面馆",
        citation_kind="image",
        image_index=2,
    )
    evidence = Evidence(
        source_id=source["id"],
        mention_id=mention.id,
        dimension="图片文字",
        stance="unknown",
        nature="unknown",
        quote="桂花面馆",
        link_status="uncertain",
        citation_kind="image",
        image_index=2,
    )
    candidate = CandidateDraft(
        name="桂花面馆",
        category="eat",
        mention_ids=[mention.id],
        evidence_ids=[evidence.id],
        uncertainty=["图片待核验"],
    )
    save(
        database,
        trip,
        run,
        mentions=[mention],
        evidence=[evidence],
        candidates=[candidate],
        image_observations=[observation],
        submitted_images=[{"source_id": source["id"], "image_index": 2}],
    )
    read = database.candidate_evidence(trip["id"], candidate.id)[0]
    assert read["image_index"] == 2 and read["nature"] == "unknown"
    assert read["image_observation"]["uncertain"] is True
    assert read["image_observation"]["text"] == observation.text


@pytest.mark.parametrize(
    "problem", ["unsubmitted", "index", "quote", "certainty", "candidate"]
)
def test_image_claim_rejects_unsubmitted_or_unverified_promotion(database, problem):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run, images=["https://example.test/1.jpg"])
    observation = ImageObservation(
        source_id=source["id"], image_index=1, text="图中文字是西溪"
    )
    mention = PlaceMention(
        id="image",
        source_id=source["id"],
        name="西溪",
        category="play",
        quote="西溪",
        citation_kind="image",
        image_index=1,
    )
    candidate = CandidateDraft(
        name="西溪",
        category="play",
        mention_ids=[mention.id],
        uncertainty=["图片待核验"],
    )
    submitted = [{"source_id": source["id"], "image_index": 1}]
    if problem == "unsubmitted":
        submitted = []
    elif problem == "index":
        submitted[0]["image_index"] = 2
    elif problem == "quote":
        mention = mention.model_copy(update={"quote": "不存在的景点"})
    elif problem == "certainty":
        observation = observation.model_copy(update={"uncertain": False})
    else:
        candidate = candidate.model_copy(update={"uncertainty": []})
    with pytest.raises(ValueError):
        save(
            database,
            trip,
            run,
            mentions=[mention],
            candidates=[candidate],
            image_observations=[observation],
            submitted_images=submitted,
        )
    with database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM image_observations").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM place_mentions").fetchone()[0] == 0
        )


def test_delete_cascades_discovery_and_foreign_keys_block_cross_trip_rows(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    save(
        database,
        trip,
        run,
        mentions=[mention],
        evidence=[evidence],
        candidates=[candidate],
        gaps=[Gap(kind="meal", reason="待补充")],
    )
    other = create(database)
    with (
        pytest.raises(sqlite3.IntegrityError),
        database.connection(write=True) as connection,
    ):
        connection.execute(
            "INSERT INTO source_snapshots(run_id,source_id,trip_id,request_version,snapshot_json,read_at) VALUES(?,?,?,?,?,?)",
            (run["id"], source["id"], other["id"], 1, "{}", "now"),
        )
    database.delete_trip(trip["id"], 1)
    with database.connection() as connection:
        for table in (
            "sources",
            "source_snapshots",
            "run_sources",
            "place_mentions",
            "evidence",
            "candidates",
            "candidate_evidence",
            "gaps",
        ):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_domain_rejects_raw_identity_handles_and_false_certainty():
    for kwargs in (
        {"author_key": "raw-user-id"},
        {"cookie": "secret"},
        {"xsec_token": "handle"},
        {"published_at": "2026-10-12T08:00:00"},
    ):
        with pytest.raises(ValidationError):
            SourceNote(**kwargs)
    with pytest.raises(ValidationError):
        CandidateDraft(name="杭州", category="play", kind="direction")
    with pytest.raises(ValidationError):
        Evidence(
            source_id="s",
            dimension="图片",
            quote="门头",
            stance="positive",
            nature="experience",
            citation_kind="image",
            image_index=1,
        )


def test_reading_malformed_saved_payload_fails_without_reset(database):
    trip = create(database)
    run = running(database, trip)
    source = note(database, trip, run)
    mention, evidence, candidate = extraction(source)
    save(
        database,
        trip,
        run,
        mentions=[mention],
        evidence=[evidence],
        candidates=[candidate],
    )
    bad = '{"unexpected_private_field":"do-not-return"}'
    with database.connection(write=True) as connection:
        connection.execute(
            "UPDATE candidates SET payload_json=? WHERE id=?", (bad, candidate.id)
        )
    with pytest.raises(ValidationError):
        database.list_candidates(trip["id"])
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT payload_json FROM candidates WHERE id=?", (candidate.id,)
            ).fetchone()[0]
            == bad
        )
    assert database.get_trip(trip["id"])["name"] == trip["name"]
