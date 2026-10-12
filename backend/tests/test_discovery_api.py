from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easytrip.api.discovery import make_router
from easytrip.domain.discovery import (
    CandidateDraft,
    Evidence,
    PlaceMention,
    SourceComment,
    SourceNote,
)
from easytrip.domain.models import CreateTrip, TripPatch
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


@pytest.fixture
def saved(tmp_path):
    database = Database(tmp_path / "discovery.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    run = database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1))
    run = database.claim_run()
    note = database.save_source(trip["id"], run["id"], run["generation"], SourceNote(external_id="real-note", title="杭州", text="西湖夜景好看"))
    comment = database.save_source(trip["id"], run["id"], run["generation"], SourceComment(external_id="real-comment", parent_source_id=note["id"], text="西湖夜景好看，但排队太久"))
    mention = PlaceMention(source_id=note["id"], name="西湖", city="杭州", category="play", quote="西湖夜景好看")
    evidence = Evidence(source_id=comment["id"], mention_id=mention.id, object_text="西湖", dimension="夜景", stance="positive", nature="experience", quote="西湖夜景好看", link_status="linked")
    candidate = CandidateDraft(name="西湖", category="play", city="杭州", mention_ids=[mention.id], evidence_ids=[evidence.id])
    database.save_discovery_batch(trip["id"], run["id"], run["generation"], mentions=[mention], activities=[], evidence=[evidence], candidates=[candidate], gaps=[])
    app = FastAPI()
    app.state.database = database
    app.include_router(make_router())
    with TestClient(app) as client:
        yield client, database, trip, other, run, note, comment, candidate


def test_candidates_sources_and_evidence_are_owned_and_have_no_coordinates(saved):
    client, _, trip, other, run, note, comment, candidate = saved
    response = client.get(f"/api/trips/{trip['id']}/candidates", params={"run_id": run["id"], "category": "play", "limit": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1 and payload["items"][0]["match_status"] == "unmatched"
    assert not {"coordinates", "poi_id", "location"}.intersection(payload["items"][0])
    assert client.get(f"/api/trips/{other['id']}/candidates", params={"run_id": run["id"]}).status_code == 404
    cited = client.get(f"/api/trips/{trip['id']}/candidates/{candidate.id}/evidence")
    assert cited.status_code == 200
    assert [row["id"] for row in cited.json()["sources"]] == [note["id"]]
    assert "author_key" not in cited.json()["sources"][0]
    assert client.get(f"/api/trips/{other['id']}/candidates/{candidate.id}/evidence").status_code == 404
    source = client.get(f"/api/trips/{trip['id']}/sources/{comment['id']}", params={"run_id": run["id"]})
    assert source.status_code == 200 and source.json()["parent_source_id"] == note["id"]
    assert "author_key" not in source.json()
    assert client.get(f"/api/trips/{other['id']}/sources/{note['id']}", params={"run_id": run["id"]}).status_code == 404
    assert client.get(f"/api/trips/{trip['id']}/sources", params={"run_id": run["id"]}).status_code == 200


def test_default_results_exclude_older_conditions_but_explicit_history_remains(saved):
    client, database, trip, _, run, *_ = saved
    database.update_trip(trip["id"], TripPatch(expected_version=1, name="新条件"))
    assert client.get(f"/api/trips/{trip['id']}/candidates").json()["total"] == 0
    assert client.get(f"/api/trips/{trip['id']}/candidates", params={"run_id": run["id"]}).json()["total"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"category": "anything"}, {"run_id": "not-uuid"}])
def test_query_boundaries_are_validated(saved, params):
    client, _, trip, *_ = saved
    assert client.get(f"/api/trips/{trip['id']}/candidates", params=params).status_code == 422
