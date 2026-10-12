from uuid import uuid4

import pytest

from easytrip.domain.discovery import (
    CandidateDraft,
    PlaceMention,
    SourceComment,
    SourceNote,
)
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database, NotFound


@pytest.fixture
def saved(tmp_path):
    database = Database(tmp_path / "reuse.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
    parent = database.claim_run()
    note = database.save_source(trip["id"], parent["id"], parent["generation"], SourceNote(external_id="note", text="杭州西湖夜景"), query="杭州", purpose="main")
    comment = database.save_source(trip["id"], parent["id"], parent["generation"], SourceComment(external_id="comment", parent_source_id=note["id"], text="夜景好看"), query="杭州")
    database.stop_run(parent["id"], parent["generation"], "partial", "test_stop")
    database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover", parent_run_id=parent["id"]))
    child = database.claim_run()
    return database, trip, parent, child, note, comment


def test_reuse_preserves_original_read_time_and_does_not_rewind_latest(saved):
    database, trip, parent, child, note, comment = saved
    database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
    fresh = database.claim_run()
    database.save_source(trip["id"], fresh["id"], fresh["generation"], SourceNote(external_id="note", text="西湖新的内容"))
    copied = database.reuse_note_sources(trip["id"], child["id"], child["generation"], parent["id"], note["id"])
    assert {row["id"] for row in copied} == {note["id"], comment["id"]}
    assert copied == database.reuse_note_sources(trip["id"], child["id"], child["generation"], parent["id"], note["id"])
    assert database.get_source(trip["id"], note["id"], child["id"])["read_at"] == database.get_source(trip["id"], note["id"], parent["id"])["read_at"]
    assert database.get_source(trip["id"], note["id"])["text"] == "西湖新的内容"
    assert database.get_run(child["id"])["usage"]["external_calls"] == 0
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    assert database.reuse_note_sources(other["id"], child["id"], child["generation"], parent["id"], note["id"]) is None
    alien = database.enqueue_run(other["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
    with pytest.raises(NotFound):
        database.reuse_note_sources(trip["id"], child["id"], child["generation"], alien["id"], note["id"])


def test_replacement_rolls_back_and_summary_never_completes_the_task(saved):
    database, trip, parent, child, note, _ = saved
    database.reuse_note_sources(trip["id"], child["id"], child["generation"], parent["id"], note["id"])
    mention = PlaceMention(source_id=note["id"], name="西湖", category="play", quote="杭州西湖夜景")
    original = CandidateDraft(name="西湖", category="play", mention_ids=[mention.id])
    database.save_discovery_batch(trip["id"], child["id"], child["generation"], mentions=[mention], candidates=[original])
    invalid = CandidateDraft(name="无来源", category="play", mention_ids=[str(uuid4())])
    with pytest.raises(ValueError):
        database.save_discovery_batch(trip["id"], child["id"], child["generation"], candidates=[invalid], replace_candidates=True)
    assert database.list_candidates(trip["id"], child["id"])["items"][0]["id"] == original.id
    replacement = original.model_copy(update={"summary": "新增依据"})
    database.save_discovery_batch(trip["id"], child["id"], child["generation"], candidates=[replacement], replace_candidates=True)
    assert database.list_candidates(trip["id"], child["id"])["items"][0]["summary"] == "新增依据"
    assert database.save_discovery_summary(child["id"], child["generation"], {"summary": "部分候选"})
    latest = database.get_run(child["id"])
    assert latest["status"] == "running" and not latest["checkpoint"]["completed_stages"]
    database.cancel_run(child["id"], trip["id"], child["generation"])
    assert not database.save_discovery_summary(child["id"], child["generation"], {"summary": "迟到"})
    assert database.reuse_note_sources(trip["id"], child["id"], child["generation"], parent["id"], note["id"]) is None
    assert database.save_discovery_batch(trip["id"], child["id"], child["generation"], candidates=[], replace_candidates=True) is None
