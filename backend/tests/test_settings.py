from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from easytrip.api import settings as settings_api
from easytrip.config import Settings
from easytrip.main import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        xhs_mcp="http://localhost:18060/mcp?token=private-xhs",
        amap_web_key="private-map", model_id="vision", model_api_key="private-model",
    )
    app = create_app(settings, tmp_path, settings_path=tmp_path / ".env")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def save(client, changes, revision=None):
    return client.put("/api/settings", headers={"X-EasyTrip-Settings": "1"}, json={
        "revision": revision or client.get("/api/settings").json()["revision"], "changes": changes,
    })


def test_masked_settings_save_restart_and_clear(client, tmp_path, monkeypatch):
    before = client.get("/api/settings")
    assert before.headers["cache-control"] == "no-store"
    assert "private" not in before.text and "localhost:18060" not in before.text
    assert before.json()["fields"]["model_id"]["value"] == "vision"
    assert before.json()["fields"]["amap_web_key"] == {"configured": True, "value": ""}
    # Dotenv quoting must preserve dollars, apostrophes, backslashes and Unicode exactly.
    key = r"new'key\with${HOME} # 中文"
    result = save(client, {"amap_web_key": key, "model_id": "new-vision"})
    assert result.status_code == 200 and result.json()["restart_required"]
    assert key not in result.text
    assert client.app.state.settings.amap_web_key == "private-map"
    monkeypatch.setenv("MODEL_API_KEY", "unrelated-environment")
    restored = Settings.load(tmp_path / ".env", isolated=True)
    assert restored.amap_web_key == key
    assert restored.model_api_key == "private-model"
    assert restored.xhs_mcp.endswith("private-xhs")
    # Clearing is explicit and survives reopening instead of falling back to an alias.
    assert save(client, {"model_api_key": ""}).status_code == 200
    assert Settings.load(tmp_path / ".env", isolated=True).model_api_key == ""
    with TestClient(create_app(
        Settings.load(tmp_path / ".env", isolated=True), tmp_path, settings_path=tmp_path / ".env",
    ), base_url="http://127.0.0.1") as reopened:
        assert not reopened.get("/api/settings").json()["restart_required"]


@pytest.mark.parametrize("changes", [
    {"unknown": "secret-value"}, {"model_api_key": ["secret-value"]},
    {"model_api_key": "secret-value\nINJECT=bad"},
    {"xhs_mcp": "file:///secret-value"}, {"model_base_url": "https://u:secret-value@example.com"},
])
def test_invalid_settings_never_echo_credentials_or_write(client, tmp_path, changes):
    response = save(client, changes)
    assert response.status_code == 400 and "secret-value" not in response.text
    assert not (tmp_path / ".env").exists()


def test_settings_write_requires_app_header_same_origin_and_fresh_revision(client):
    revision = client.get("/api/settings").json()["revision"]
    payload = {"revision": revision, "changes": {"model_api_key": "changed"}}
    assert client.put("/api/settings", json=payload).status_code == 403
    assert client.put("/api/settings", json=payload, headers={
        "X-EasyTrip-Settings": "1", "Origin": "https://evil.invalid",
    }).status_code == 403
    assert client.get("/api/settings", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert save(client, payload["changes"], revision).status_code == 200
    assert save(client, {"model_id": "stale"}, revision).status_code == 409


def test_failed_atomic_save_preserves_existing_configuration(client, tmp_path, monkeypatch):
    assert save(client, {"model_id": "saved"}).status_code == 200
    before = (tmp_path / ".env").read_bytes()

    def fail(*args):
        raise PermissionError("private-error")

    monkeypatch.setattr(settings_api.os, "replace", fail)
    response = save(client, {"model_api_key": "replacement"})
    assert response.status_code == 500 and "private-error" not in response.text
    assert (tmp_path / ".env").read_bytes() == before
    assert not list(tmp_path.glob(".settings-*"))


def test_desktop_session_bootstrap_guards_api_and_preserves_trip_data(tmp_path):
    path = tmp_path / "easytrip.sqlite3"
    for index in range(2):
        with TestClient(create_app(
            Settings(), tmp_path, database_path=path, settings_path=tmp_path / ".env",
            desktop_token=f"test-session-{index}",
        ), base_url="http://127.0.0.1") as client:
            assert client.get("/api/settings").status_code == 403
            assert client.get("/desktop/wrong").status_code == 403
            response = client.get(f"/desktop/test-session-{index}", follow_redirects=False)
            assert response.status_code == 303
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=strict" in response.headers["set-cookie"]
            assert client.get("/api/settings").status_code == 200
            if index == 0:
                assert client.post("/api/trips", json={"operation_id": str(uuid4())}).status_code == 201
            assert client.get("/api/trips").json()["total"] == 1


def test_desktop_env_supports_old_aliases_and_explicit_clears(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("AMAP_WEB_KEY", "must-not-inherit")
    path.write_text("agnes_key='alias-key'\nAgnes_MODEL='vision'\n12306_MCP=http://localhost:18061/mcp\n", encoding="utf-8")
    settings = Settings.load(path, isolated=True)
    assert settings.model_api_key == "alias-key" and settings.model_id == "vision"
    assert settings.rail_mcp == "http://localhost:18061/mcp"
    assert not settings.amap_web_key
    path.write_text("AGNES_KEY=old\nMODEL_API_KEY=''\n", encoding="utf-8")
    assert Settings.load(path, isolated=True).model_api_key == ""


def import_env(client, content, *, apply=False, revision=None):
    return client.post("/api/settings/import", headers={"X-EasyTrip-Settings": "1"}, json={
        "revision": revision or client.get("/api/settings").json()["revision"],
        "content": content, "apply": apply,
    })


def test_import_previews_aliases_without_writing_or_exposing_secrets_then_saves(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_ENV", "must-not-expand")
    content = "\ufeff# old configuration\r\nexport Agnes_KEY='literal${PRIVATE_ENV}'\r\nAGNES_MODEL=legacy\r\nMODEL_ID=preferred\r\n12306_MCP=http://localhost:18061/mcp\r\nAMAP_WEB_KEY=\r\nUNRELATED=private-value\r\n"
    preview = import_env(client, content)
    assert preview.status_code == 200
    assert preview.json()["imported_fields"] == ["rail_mcp", "model_api_key", "model_id"]
    assert preview.json()["skipped_empty"] == 1 and preview.json()["ignored_fields"] == 1
    assert all(value not in preview.text for value in ("PRIVATE_ENV", "private-value", "localhost:18061", "preferred"))
    assert not (tmp_path / ".env").exists()
    applied = import_env(client, content, apply=True)
    assert applied.status_code == 200 and applied.json()["settings"]["restart_required"]
    restored = Settings.load(tmp_path / ".env", isolated=True)
    assert restored.model_api_key == "literal${PRIVATE_ENV}"
    assert restored.model_id == "preferred" and restored.amap_web_key == "private-map"
    assert restored.rail_mcp == "http://localhost:18061/mcp"
    assert client.app.state.settings.model_id == "vision"


@pytest.mark.parametrize("content", [
    'MODEL_API_KEY="private-value',
    'MODEL_API_KEY="private-value\nother-line"',
    'MODEL_API_KEY=private-value\nmodel_api_key=different',
    'MODEL_BASE_URL=http://localhost:bad/private-value',
    'XHS_MCP=https://invalid host/private-value',
    'MODEL_API_KEY=' + 'x' * 4097,
    '# comment\nUNKNOWN=private-value\nAMAP_WEB_KEY=',
    'MODEL_API_KEY=private-value\ufffd',
    'MODEL_API_KEY=' + 'x' * 65536,
], ids=["unclosed-quote", "multiline-secret", "duplicate", "bad-port", "bad-host", "long-value", "no-known-values", "invalid-encoding", "large-file"])
def test_invalid_import_is_rejected_atomically_without_echo(client, tmp_path, content):
    response = import_env(client, content, apply=True)
    assert response.status_code == 400
    assert "private-value" not in response.text
    assert not (tmp_path / ".env").exists()


def test_import_rejects_stale_preview_and_failed_write_retains_old_file(client, tmp_path, monkeypatch):
    revision = client.get("/api/settings").json()["revision"]
    assert import_env(client, "AGNES_KEY=new-private").status_code == 200
    assert save(client, {"model_id": "changed-elsewhere"}).status_code == 200
    assert import_env(client, "AGNES_KEY=new-private", apply=True, revision=revision).status_code == 409
    before = (tmp_path / ".env").read_bytes()

    def fail(*args):
        raise OSError("private-path")

    monkeypatch.setattr(settings_api.os, "replace", fail)
    response = import_env(client, "AGNES_KEY=new-private", apply=True)
    assert response.status_code == 500 and "private-path" not in response.text
    assert (tmp_path / ".env").read_bytes() == before


def test_local_check_explains_missing_invalid_and_ready_without_saving_or_network(client, tmp_path, monkeypatch):
    async def no_network(*args, **kwargs):
        pytest.fail("configuration check attempted external request")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_network)
    response = client.post("/api/settings/check", headers={"X-EasyTrip-Settings": "1"}, json={
        "revision": client.get("/api/settings").json()["revision"],
        "changes": {"xhs_mcp": "http://invalid host/private-token", "amap_js_key": "new-private"},
    })
    assert response.status_code == 200 and "private" not in response.text
    checks = response.json()["checks"]
    assert checks["xhs_mcp"]["invalid"] == ["xhs_mcp"]
    assert checks["xhs_mcp"]["state"] == "invalid"
    assert checks["model"]["missing"] == ["model_base_url"]
    assert checks["amap_js"]["state"] == "incomplete"
    assert checks["amap_js"]["missing"] == ["amap_js_secure_key"]
    assert checks["amap_web"]["state"] == "configured_not_probed"
    assert not (tmp_path / ".env").exists()
    assert client.app.state.settings.amap_js_key == ""


@pytest.mark.parametrize("route,payload", [
    ("import", {"revision": "test", "content": "AGNES_KEY=private", "apply": True}),
    ("check", {"revision": "test", "changes": {}}),
    ("setup-complete", {}),
])
def test_new_settings_actions_keep_same_origin_and_custom_header_guards(client, route, payload):
    assert client.post(f"/api/settings/{route}", json=payload).status_code == 403
    assert client.post(f"/api/settings/{route}", json=payload, headers={
        "X-EasyTrip-Settings": "1", "Origin": "https://other.example",
    }).status_code == 403


def test_first_launch_guide_skip_persists_without_saving_fake_credentials(tmp_path):
    for skipped in (False, True):
        with TestClient(create_app(
            Settings(), tmp_path, settings_path=tmp_path / ".env", desktop_token="test-token",
        ), base_url="http://127.0.0.1") as client:
            response = client.get("/desktop/test-token", follow_redirects=False)
            assert response.headers["location"] == ("/" if skipped else "/?setup=1")
            assert client.get("/api/settings").json()["setup_required"] is not skipped
            assert client.post("/api/settings/setup-complete", json={}, headers={"X-EasyTrip-Settings": "1"}).status_code == 200
            assert not (tmp_path / ".env").exists()


def test_existing_configuration_does_not_trigger_first_launch_guide(tmp_path):
    (tmp_path / ".env").write_text("AGNES_MODEL=existing\n", encoding="utf-8")
    with TestClient(create_app(
        Settings(), tmp_path, settings_path=tmp_path / ".env", desktop_token="test-token",
    ), base_url="http://127.0.0.1") as client:
        assert client.get("/desktop/test-token", follow_redirects=False).headers["location"] == "/"


def test_empty_imported_canonical_value_can_use_nonempty_legacy_alias(client, tmp_path):
    result = import_env(client, "MODEL_API_KEY=\nAGNES_KEY=legacy-key", apply=True)
    assert result.status_code == 200 and result.json()["skipped_empty"] == 1
    assert Settings.load(tmp_path / ".env", isolated=True).model_api_key == "legacy-key"
