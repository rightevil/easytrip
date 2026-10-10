import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.app import config
from backend.app.config import Settings, direct_client, validate_url


def test_agnes_aliases_are_case_insensitive_and_settings_repr_is_safe(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(config.os, "environ", {})
    (tmp_path / ".env").write_text(
        "Agnes_url=https://agnes.example/v1\nagnes_Key=model-secret\nAGNES_MODEL=vision-model\n",
        encoding="utf-8",
    )
    settings = Settings.load()
    assert (settings.model_base_url, settings.model_api_key, settings.model_id) == (
        "https://agnes.example/v1",
        "model-secret",
        "vision-model",
    )
    assert "model-secret" not in repr(settings)
    assert "agnes.example" not in repr(settings)
    assert (
        settings.redact("model-secret https://agnes.example/v1")
        == "[REDACTED] [REDACTED]"
    )


def test_environment_model_settings_override_dotenv_aliases(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(
        config.os,
        "environ",
        {
            "MODEL_BASE_URL": " https://model.example/v1 ",
            "MODEL_API_KEY": " environment-secret ",
            "MODEL_ID": " selected-model ",
        },
    )
    (tmp_path / ".env").write_text(
        "AGNES_KEY=old-secret\nAGNES_MODEL=old-model\n", encoding="utf-8"
    )
    settings = Settings.load()
    assert settings.model_base_url == "https://model.example/v1"
    assert settings.model_api_key == "environment-secret"
    assert settings.model_id == "selected-model"


def test_redact_keeps_usage_and_removes_credentials_urls_and_contact_data():
    settings = Settings(secrets={"web-secret", "model-secret"})
    payload = {
        "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
        "xsecToken": "unregistered-token",
        "Cookie": "session=abc",
        "Authorization": "Bearer abc",
        "nickname": "作者",
        "user_id": "12345",
        "avatar": "https://cdn.example/avatar",
        "samples": [
            {
                "text": "web-secret model-secret https://cdn.example/p?signature=abc 13800138000 a@example.com"
            }
        ],
        "missing": None,
    }
    result = settings.redact(payload)
    assert result["usage"] == payload["usage"]
    assert result["missing"] is None
    assert all(
        result[key] == "[REDACTED]"
        for key in (
            "xsecToken",
            "Cookie",
            "Authorization",
            "nickname",
            "user_id",
            "avatar",
        )
    )
    assert result["samples"][0]["text"] == "[REDACTED] [REDACTED] [URL] [PHONE] [EMAIL]"
    assert "web-secret" not in json.dumps(result)
    assert payload["xsecToken"] == "unregistered-token"


def test_real_http_client_ignores_proxy_environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"direct")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:

        async def run():
            async with direct_client() as client:
                response = await client.get(f"http://127.0.0.1:{server.server_port}/")
                assert response.status_code == 200
                assert response.text == "direct"

        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com",
        "https:///missing-host",
        "localhost:8000",
    ],
)
def test_invalid_service_urls_are_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)
