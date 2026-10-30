from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easytrip.api.runs import make_router
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.storage.database import Database


@pytest.mark.parametrize("code", [429, 401, 503, "429", True])
def test_stopped_model_diagnostic_exposes_only_valid_http_status_and_checks_owner(tmp_path, code):
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    run = database.enqueue_run(trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover"))
    run = database.claim_run()
    action = database.begin_action(run["id"], run["generation"], "understanding:model:1", "model", {"max_tokens": 16384})
    database.finish_action(action, status="unknown", result={"error": "private-error"}, usage={"http_status": code, "private_key": "private-value", "result_unknown": True})
    database.stop_run(run["id"], run["generation"], "paused", "model_request_failed")
    before = database.list_actions(run["id"])
    app = FastAPI()
    app.state.database = database
    app.include_router(make_router())
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run['id']}", params={"trip_id": trip["id"]})
        assert response.status_code == 200
        assert response.json().get("model_http_status") == (code if type(code) is int else None)
        assert "private-value" not in response.text and "private-error" not in response.text
        assert client.get(f"/api/runs/{run['id']}", params={"trip_id": other["id"]}).status_code == 404
    assert database.list_actions(run["id"]) == before
