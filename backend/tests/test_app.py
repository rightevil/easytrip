import time
from uuid import uuid4

from fastapi.testclient import TestClient

from easytrip.config import Settings
from easytrip.domain.models import CreateTrip
from easytrip.domain.tasks import CreateRun
from easytrip.main import create_app
from easytrip.storage.database import Database


def test_actual_application_initializes_and_reopens_trip_data(tmp_path):
    path = tmp_path / "trips.sqlite3"
    settings = Settings()
    with TestClient(
        create_app(settings, tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        created = client.post(
            "/api/trips", json={"operation_id": str(uuid4()), "name": "周末草稿"}
        )
        assert created.status_code == 201
        trip = created.json()
        response = client.post(
            f"/api/trips/{trip['id']}/messages",
            json={"operation_id": str(uuid4()), "content": "想找个安静的地方"},
        )
        assert response.status_code == 201
        assert client.get("/p0/map").status_code == 200
        assert client.get("/api/map-data").status_code == 404
    with TestClient(
        create_app(settings, tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        reopened = client.get(f"/api/trips/{trip['id']}").json()
        assert reopened["name"] == "周末草稿"
        assert reopened["request"]["destination"] is None
        assert reopened["messages"][0]["content"] == "想找个安静的地方"
        assert client.get("/api/trips").json()["total"] == 1


def test_business_api_keeps_local_host_and_origin_restrictions(tmp_path):
    with TestClient(
        create_app(Settings(), tmp_path), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/trips", headers={"Host": "other.example"}).status_code == 400
        assert client.get(
            "/api/trips", headers={"Origin": "https://other.example"}
        ).status_code == 403
        assert client.get(
            "/api/trips", headers={"Origin": "http://127.0.0.1"}
        ).status_code == 200


def test_workspace_build_served_without_changing_p0_route(tmp_path):
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<h1>workspace</h1>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('workspace')", encoding="utf-8")
    with TestClient(
        create_app(Settings(), tmp_path, frontend_dir=frontend),
        base_url="http://127.0.0.1",
    ) as client:
        index = client.get("/")
        assert index.status_code == 200 and "workspace" in index.text
        assert index.headers["Cache-Control"] == "no-store"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/../../.env").status_code == 404
        assert client.get("/p0/map").status_code == 200


def test_missing_frontend_build_is_explicit(tmp_path):
    with TestClient(
        create_app(Settings(), tmp_path, frontend_dir=tmp_path / "missing-build"),
        base_url="http://127.0.0.1",
    ) as client:
        assert client.get("/").status_code == 503
        assert "尚未构建" in client.get("/").text
        assert client.get("/api/trips").status_code == 200


def test_application_restart_waits_for_user_resume_and_executes_saved_run(tmp_path):
    path = tmp_path / "recovery.sqlite3"
    database = Database(path)
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    interrupted = database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1)
    )
    with TestClient(
        create_app(Settings(), tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        route = f"/api/runs/{interrupted['id']}"
        recovered = client.get(route, params={"trip_id": trip["id"]}).json()
        assert recovered["status"] == "recoverable"
        assert recovered["usage"]["steps"] == 0
        assert client.get(route, params={"trip_id": str(uuid4())}).status_code == 404
        resumed = client.post(
            route + "/resume",
            json={"trip_id": trip["id"], "generation": recovered["generation"]},
        )
        assert resumed.status_code == 200
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            finished = client.get(route, params={"trip_id": trip["id"]}).json()
            if finished["status"] == "completed":
                break
            time.sleep(0.01)
        assert finished["status"] == "completed"
        assert finished["usage"]["steps"] == 3
        assert finished["usage"]["external_calls"] == 0
        assert finished["result"]["conditions"]["destination"] is None
    with TestClient(
        create_app(Settings(), tmp_path, database_path=path),
        base_url="http://127.0.0.1",
    ) as client:
        retained = client.get(route, params={"trip_id": trip["id"]}).json()
        assert retained["status"] == "completed"
        assert retained["usage"]["steps"] == 3
