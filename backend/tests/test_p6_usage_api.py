import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from easytrip.agent.discovery import DiscoveryRunner
from easytrip.config import Settings
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.main import create_app
from easytrip.storage.database import Database


@pytest.mark.parametrize("frozen_model", [None, "previous-model"])
def test_model_ledger_freezes_actual_model_without_overwriting_extraction_metadata(
    tmp_path, frozen_model
):
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover")
    )
    run = database.claim_run()
    runner = DiscoveryRunner(database, Settings(model_id="current-model"))
    _, before, _ = asyncio.run(runner.model_hooks(run, "understanding"))
    info = {"max_tokens": 16384}
    if frozen_model:
        info["model_id"] = frozen_model
    assert asyncio.run(before(info))
    assert database.list_actions(run["id"])[0]["params"]["model_id"] == (
        frozen_model or "current-model"
    )


def test_usage_endpoint_is_trip_owned_and_recovery_does_not_replay(tmp_path):
    path = tmp_path / "app.sqlite3"
    database = Database(path)
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    action = database.begin_api_action(trip["id"], 1, "rail", "get-tickets")
    app = create_app(Settings(), database_path=path)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/api/trips/{trip['id']}/usage")
        assert response.status_code == 200
        result = response.json()
        assert result["external_calls"] == 1
        assert result["cost_cny"] is None
        assert client.get(f"/api/trips/{uuid4()}/usage").status_code == 404
        assert (
            client.get(
                f"/api/trips/{trip['id']}/usage",
                headers={"origin": "https://hostile.example"},
            ).status_code
            == 403
        )
    with database.connection() as connection:
        row = connection.execute(
            "SELECT status FROM api_actions WHERE id=?", (action,)
        ).fetchone()
        assert row["status"] == "unknown"


def test_usage_report_redacts_configured_secrets_even_in_legacy_model_metadata(
    tmp_path,
):
    path = tmp_path / "app.sqlite3"
    database = Database(path)
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1, kind="discover")
    )
    run = database.claim_run()
    database.begin_action(
        run["id"],
        run["generation"],
        "test:model:1",
        "model",
        {"max_tokens": 16384, "model_id": "private-ledger-key"},
    )
    app = create_app(Settings(secrets={"private-ledger-key"}), database_path=path)
    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(f"/api/trips/{trip['id']}/usage")
        assert response.status_code == 200
        assert "private-ledger-key" not in response.text
        assert response.json()["actions"][0]["model_id"] == "[REDACTED]"
