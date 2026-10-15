import json
import socket
import subprocess
import sys

import pytest

from easytrip import diagnostics
from easytrip.config import ROOT_DIR, Settings
from easytrip.storage.database import Database


def built_frontend(tmp_path):
    frontend = tmp_path / "frontend" / "dist"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<html></html>", encoding="utf-8")
    (frontend / "assets" / "main.js").write_text("// built", encoding="utf-8")
    return frontend


def test_readiness_is_local_and_does_not_expose_config_or_records(
    tmp_path, monkeypatch
):
    settings = Settings(
        xhs_mcp="http://localhost:9000/mcp?token=secret-xhs",
        rail_mcp="not-a-url-secret-rail",
        model_base_url="https://model.example/v1?key=secret-model",
        model_api_key="secret-key",
        model_id="secret-id",
        amap_js_key="secret-js",
    )
    monkeypatch.setattr(diagnostics, "version", lambda _: "test-version")
    path = tmp_path / "app.sqlite3"
    Database(path).initialize()
    before = path.read_bytes()
    result = diagnostics.readiness(
        settings, frontend_dir=built_frontend(tmp_path), database_path=path
    )
    assert result["ready_for_local_workspace"] is True
    assert result["database"]["state"] == "ready"
    assert result["services"] == {
        "xhs_mcp": "configured_not_probed",
        "rail_mcp": "invalid",
        "model": "configured_not_probed",
        "amap_web": "unconfigured",
        "amap_js": "incomplete",
    }
    assert "secret" not in json.dumps(result)
    assert path.read_bytes() == before


def test_missing_optional_services_allow_offline_workspace_but_core_failures_do_not(
    tmp_path, monkeypatch
):
    frontend = built_frontend(tmp_path)
    path = tmp_path / "app.sqlite3"
    result = diagnostics.readiness(
        Settings(), frontend_dir=frontend, database_path=path
    )
    assert result["ready_for_local_workspace"] is True
    assert result["database"] == {"state": "not_created"}
    assert not path.exists()
    path.write_bytes(b"broken")
    assert (
        diagnostics.readiness(Settings(), frontend_dir=frontend, database_path=path)[
            "database"
        ]["state"]
        == "invalid"
    )
    path.unlink()
    (frontend / "assets" / "main.js").unlink()
    assert (
        diagnostics.readiness(Settings(), frontend_dir=frontend, database_path=path)[
            "ready_for_local_workspace"
        ]
        is False
    )

    def missing(_):
        raise diagnostics.PackageNotFoundError

    monkeypatch.setattr(diagnostics, "version", missing)
    result = diagnostics.readiness(
        Settings(), frontend_dir=frontend, database_path=path
    )
    assert not any(result["dependencies"].values())
    assert result["ready_for_local_workspace"] is False


def test_cli_safe_failure_and_exit(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostics", "--quiet"])
    monkeypatch.setattr(
        diagnostics, "readiness", lambda: {"ready_for_local_workspace": True}
    )
    assert diagnostics.main() == 0
    assert not capsys.readouterr().out

    def invalid():
        raise ValueError("secret-value")

    monkeypatch.setattr(diagnostics, "readiness", invalid)
    assert diagnostics.main() == 1
    assert "secret-value" not in capsys.readouterr().out


@pytest.mark.skipif(sys.platform != "win32", reason="Windows entry point")
def test_windows_entry_guards_do_not_touch_occupied_process_or_require_uv(tmp_path):
    frontend = built_frontend(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "dev.ps1"
    script.write_bytes((ROOT_DIR / "scripts" / "dev.ps1").read_bytes())
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "serve",
                "-Port",
                str(port),
            ],
            capture_output=True,
            timeout=20,
        )
        assert result.returncode != 0
        assert b"Local port is unavailable" in result.stderr
        assert server.getsockname()[1] == port
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "restore",
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert b"Restore requires" in result.stderr
    assert frontend.exists()
