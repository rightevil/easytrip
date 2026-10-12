import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easytrip.api.trips import make_router
from easytrip.config import Settings
from easytrip.storage.database import Database


@pytest.fixture
def client(tmp_path):
    application = FastAPI()
    database = Database(tmp_path / "trips.sqlite3")
    database.initialize()
    application.state.database = database
    application.state.settings = Settings(secrets={"test-private-key"})
    application.include_router(make_router())
    with TestClient(application) as result:
        yield result


def create(client, **changes):
    response = client.post("/api/trips", json={"operation_id": str(uuid4()), **changes})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_default_draft_is_persisted_with_versions(client):
    trip = create(client)
    assert trip["name"] == "未命名行程"
    assert trip["status"] == "draft"
    assert trip["request"]["destination"] is None
    assert trip["request"]["transport"] == "mixed"
    assert trip["request_version"] == trip["workspace_version"] == 1
    assert trip["messages"] == trip["runs"] == []
    assert client.get(f"/api/trips/{trip['id']}").json() == trip
    assert client.get("/api/trips").json()["total"] == 1


def test_create_retry_is_idempotent_but_changed_payload_conflicts(client):
    body = {"operation_id": str(uuid4()), "name": "杭州周末"}
    first = client.post("/api/trips", json=body)
    retry = client.post("/api/trips", json=body)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    conflict = client.post("/api/trips", json={**body, "name": "更改后的内容"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "operation_conflict"
    assert client.get("/api/trips").json()["total"] == 1


def test_pagination_and_archive_filter(client):
    trips = [create(client, name=f"草稿 {number}") for number in range(3)]
    archived = client.post(
        f"/api/trips/{trips[0]['id']}/archive", json={"expected_version": 1, "archived": True}
    )
    assert archived.status_code == 200
    page = client.get("/api/trips?limit=1&offset=1").json()
    assert (page["total"], page["limit"], page["offset"], len(page["items"])) == (2, 1, 1, 1)
    assert page["items"][0]["id"] != trips[0]["id"]
    full = client.get("/api/trips?include_archived=true").json()
    assert full["total"] == len(full["items"]) == 3
    assert any(item["status"] == "archived" for item in full["items"])


def test_patch_request_conflict_keeps_last_committed_state(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"
    updated = client.patch(url, json={
        "expected_version": 1, "name": "周末杭州", "request": {
            "destination": "杭州", "origin": "杭州东站", "transport": "walking",
            "start_at": "2026-10-17T09:00:00+08:00", "duration_minutes": 480,
        },
    })
    assert updated.status_code == 200
    assert updated.json()["request_version"] == 2
    assert updated.json()["workspace_version"] == 1
    assert updated.json()["request"]["destination"] == "杭州"
    conflict = client.patch(url, json={"expected_version": 1, "name": "旧窗口覆盖"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {
        "code": "version_conflict", "message": "版本已更新，请保留编辑并重新加载", "current_version": 2,
    }
    assert client.get(url).json()["name"] == "周末杭州"


def test_workspace_version_is_independent_and_conflict_is_reported(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"
    workspace = {"expected_version": 1, "workspace": {"active_tab": "map", "map_center": [120.15, 30.26]}}
    saved = client.patch(url + "/workspace", json=workspace)
    assert saved.status_code == 200
    assert saved.json()["workspace_version"] == 2
    assert saved.json()["request_version"] == 1
    assert client.patch(url, json={"expected_version": 1, "name": "改名"}).status_code == 200
    conflict = client.patch(url + "/workspace", json=workspace)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_version"] == 2
    assert client.get(url).json()["workspace"]["map_center"] == [120.15, 30.26]


def test_message_preserves_user_url_redacts_known_secret_and_deduplicates(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"
    body = {
        "operation_id": str(uuid4()),
        "content": "参考 https://example.test/guide?day=1，密钥 test-private-key，邮箱 me@example.test",
    }
    first = client.post(url + "/messages", json=body)
    retry = client.post(url + "/messages", json=body)
    assert first.status_code == retry.status_code == 201
    assert first.json() == retry.json()
    message = first.json()
    assert message["role"] == "user"
    assert message["status"] == "pending"
    assert "https://example.test/guide?day=1" in message["content"]
    assert "me@example.test" in message["content"]
    assert "test-private-key" not in message["content"]
    assert "[REDACTED]" in message["content"]
    conflict = client.post(url + "/messages", json={**body, "content": "另一条消息"})
    assert conflict.status_code == 409
    detail = client.get(url).json()
    assert detail["messages"] == [message]
    assert detail["request_version"] == 1
    other = create(client)
    assert client.get(f"/api/trips/{other['id']}").json()["messages"] == []


def test_archived_trip_rejects_edits_and_can_be_restored(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"
    archived = client.post(url + "/archive", json={"expected_version": 1, "archived": True})
    assert archived.json()["request_version"] == 2
    assert client.patch(url, json={"expected_version": 2, "name": "不应保存"}).status_code == 409
    assert client.patch(url + "/workspace", json={"expected_version": 1, "workspace": {}}).status_code == 409
    assert client.post(url + "/messages", json={"operation_id": str(uuid4()), "content": "不应保存"}).status_code == 409
    restored = client.post(url + "/archive", json={"expected_version": 2, "archived": False})
    assert restored.status_code == 200
    assert restored.json()["status"] == "draft"
    assert restored.json()["request_version"] == 3


def test_delete_requires_current_version_and_cascades_messages(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"
    client.post(url + "/messages", json={"operation_id": str(uuid4()), "content": "保存消息"})
    client.patch(url, json={"expected_version": 1, "name": "新版本"})
    assert client.delete(url + "?expected_version=1").status_code == 409
    deleted = client.delete(url + "?expected_version=2")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(url).status_code == 404
    assert client.get("/api/trips").json()["total"] == 0
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("method,suffix,body", [
    ("get", "", None),
    ("patch", "", {"expected_version": 1, "name": "改名"}),
    ("patch", "/workspace", {"expected_version": 1, "workspace": {}}),
    ("post", "/messages", {"operation_id": str(uuid4()), "content": "消息"}),
    ("post", "/archive", {"expected_version": 1, "archived": True}),
    ("delete", "?expected_version=1", None),
])
def test_unknown_trip_is_not_found(client, method, suffix, body):
    response = client.request(method, f"/api/trips/{uuid4()}{suffix}", json=body)
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "not_found", "message": "行程不存在"}


@pytest.mark.parametrize("method,url,body", [
    ("post", "/api/trips", {}),
    ("post", "/api/trips", {"operation_id": "bad-uuid"}),
    ("post", "/api/trips", {"operation_id": str(uuid4()), "extra": True}),
    ("post", "/api/trips", {"operation_id": str(uuid4()), "request": {"start_at": "2026-10-17T09:00:00"}}),
    ("get", "/api/trips?limit=0", None),
    ("get", "/api/trips?limit=101", None),
    ("get", "/api/trips?offset=-1", None),
    ("patch", "/api/trips/id", {"expected_version": True, "name": "改名"}),
    ("patch", "/api/trips/id", {"expected_version": "1", "name": "改名"}),
    ("patch", "/api/trips/id", {"expected_version": 1}),
    ("patch", "/api/trips/id/workspace", {"expected_version": 1, "workspace": {"map_center": [200, 30]}}),
    ("post", "/api/trips/id/archive", {"expected_version": 1, "archived": "false"}),
    ("post", "/api/trips/id/messages", {"operation_id": str(uuid4()), "content": " "}),
    ("delete", "/api/trips/id", None),
])
def test_invalid_inputs_are_rejected_before_storage(client, method, url, body):
    assert client.request(method, url, json=body).status_code == 422
    assert client.get("/api/trips").json()["total"] == 0


def test_missing_database_and_write_failure_have_safe_retryable_error(client, monkeypatch):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"

    def fail(*args):
        raise sqlite3.OperationalError("private-path test-private-key")

    monkeypatch.setattr(client.app.state.database, "update_trip", fail)
    response = client.patch(url, json={"expected_version": 1, "name": "未保存"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_unavailable"
    assert "private-path" not in response.text
    assert "test-private-key" not in response.text
    assert client.get(url).json()["name"] == trip["name"]
    client.app.state.database = None
    assert client.get("/api/trips").status_code == 503


@pytest.mark.parametrize("corrupt_source", ["request", "run"])
def test_corrupt_saved_data_returns_safe_error_without_resetting_database(client, corrupt_source):
    trip = create(client, name="保留原始草稿")
    url = f"/api/trips/{trip['id']}"
    client.post(url + "/messages", json={"operation_id": str(uuid4()), "content": "保留原始消息"})
    database = client.app.state.database
    with database.connection(write=True) as connection:
        if corrupt_source == "request":
            # Valid JSON with an invalid persisted schema raises Pydantic ValidationError.
            connection.execute(
                "UPDATE trips SET request_json=? WHERE id=?",
                ('{"transport":"private-path test-private-key"}', trip["id"]),
            )
        else:
            # Simulate damaged stored bytes despite the normal JSON CHECK constraint.
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "INSERT INTO runs (id, trip_id, operation_id, request_version, kind, status, stage, params_json, budget_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()), trip["id"], str(uuid4()), 1, "prepare_workspace",
                    "pending", "queued", "{private-path:test-private-key}", "{}",
                    trip["created_at"], trip["updated_at"],
                ),
            )
        original_trip = dict(connection.execute("SELECT * FROM trips WHERE id=?", (trip["id"],)).fetchone())
        original_runs = [dict(row) for row in connection.execute("SELECT * FROM runs WHERE trip_id=?", (trip["id"],))]

    for response in (
        client.get(url),
        client.patch(url, json={"expected_version": 1, "name": "不应提交"}),
    ):
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "database_unavailable", "message": "行程存储暂不可用，请保留编辑后重试",
        }
        assert "private-path" not in response.text
        assert "test-private-key" not in response.text
    reopened = Database(database.path)
    reopened.initialize()
    client.app.state.database = reopened
    assert client.get(url).status_code == 503
    with reopened.connection() as connection:
        assert dict(connection.execute("SELECT * FROM trips WHERE id=?", (trip["id"],)).fetchone()) == original_trip
        assert [dict(row) for row in connection.execute("SELECT * FROM runs WHERE trip_id=?", (trip["id"],))] == original_runs
        assert connection.execute("SELECT content FROM messages WHERE trip_id=?", (trip["id"],)).fetchone()[0] == "保留原始消息"


def test_concurrent_patch_only_one_request_can_commit(client):
    trip = create(client)
    url = f"/api/trips/{trip['id']}"

    def update(name):
        return client.patch(url, json={"expected_version": 1, "name": name})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(update, ["窗口甲", "窗口乙"]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert client.get(url).json()["request_version"] == 2


def test_reopened_database_returns_saved_draft_and_conversation(client):
    trip = create(client, name="重开草稿")
    url = f"/api/trips/{trip['id']}"
    client.post(url + "/messages", json={"operation_id": str(uuid4()), "content": "待处理原文"})
    reopened = Database(client.app.state.database.path)
    reopened.initialize()
    client.app.state.database = reopened
    saved = client.get(url).json()
    assert saved["name"] == "重开草稿"
    assert saved["messages"][0]["content"] == "待处理原文"
    assert saved["messages"][0]["status"] == "pending"
