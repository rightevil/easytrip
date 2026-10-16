import hashlib
import importlib.util
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from easytrip.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "package.py"
spec = importlib.util.spec_from_file_location("easytrip_delivery", SCRIPT)
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def commit(root):
    git(root, "add", "--all")
    git(
        root,
        "-c",
        "user.name=Delivery Test",
        "-c",
        "user.email=delivery@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.hooksPath", str(root / "empty-hooks"))
    (root / "backend").mkdir()
    (root / ".cz.toml").write_text(
        '[tool.commitizen]\nversion="0.1.0"\n', encoding="utf-8"
    )
    (root / "backend/pyproject.toml").write_text(
        '[project]\nname="easytrip"\nversion="0.1.0"\n', encoding="utf-8"
    )
    (root / "README.md").write_text("公开源码\n", encoding="utf-8")
    (root / ".env.example").write_text(
        "MODEL_ID=public-model\nRAIL_MCP=http://127.0.0.1:18061/mcp\n", encoding="utf-8"
    )
    (root / "fixture.bin").write_bytes(b"\x00\xff\r\n\x80")
    commit(root)
    monkeypatch.setattr(delivery, "_configured_secrets", lambda: {})
    return root


def test_archive_uses_fixed_head_blobs_and_manifest_hashes(repository, tmp_path):
    (repository / ".env").write_text("not-tracked-private-key", encoding="utf-8")
    (repository / "untracked.txt").write_text("not-a-release-file", encoding="utf-8")
    destination = tmp_path / "source.zip"
    manifest = delivery.build_package(destination, root=repository)
    assert manifest["revision"] == git(repository, "rev-parse", "HEAD").decode().strip()
    assert manifest["version"] == "0.1.0"
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == set(manifest["files"]) | {"manifest.json"}
        assert not {".env", "untracked.txt"} & set(archive.namelist())
        assert archive.read("fixture.bin") == b"\x00\xff\r\n\x80"
        assert json.loads(archive.read("manifest.json")) == manifest
        for name, expected in manifest["files"].items():
            content = archive.read(name)
            assert content == git(repository, "show", f"HEAD:{name}")
            assert hashlib.sha256(content).hexdigest() == expected["sha256"]
            assert len(content) == expected["size"]
            assert archive.getinfo(name).date_time == (1980, 1, 1, 0, 0, 0)
    second = tmp_path / "same-source.zip"
    delivery.build_package(second, root=repository)
    assert second.read_bytes() == destination.read_bytes()


@pytest.mark.parametrize("staged", [False, True])
def test_uncommitted_tracked_edits_are_refused(repository, tmp_path, staged):
    (repository / "README.md").write_text("unreviewed change", encoding="utf-8")
    if staged:
        git(repository, "add", "README.md")
    destination = tmp_path / "refused.zip"
    with pytest.raises(delivery.DeliveryError, match="tracked_worktree_dirty"):
        delivery.build_package(destination, root=repository)
    assert not destination.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_flags_cannot_bypass_review(repository, tmp_path, flag):
    git(repository, "update-index", flag, "README.md")
    (repository / "README.md").write_text("hidden unreviewed edit", encoding="utf-8")
    with pytest.raises(delivery.DeliveryError, match="hidden_index_flags"):
        delivery.build_package(tmp_path / "refused.zip", root=repository)


@pytest.mark.parametrize(
    "name",
    [
        "data/trip.json",
        "reports/private.txt",
        "frontend/node_modules/lib.js",
        "backend/.venv/config",
        "backend/__pycache__/x.pyc",
        "backend/.env",
        "backend/.env.local",
        "backend/Cookies.JSON",
        "source.sqlite3",
        "source.db-wal",
        "private.pem",
    ],
)
def test_tracked_private_paths_are_refused_without_a_secret_environment(
    repository, tmp_path, name
):
    target = repository / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("private payload", encoding="utf-8")
    commit(repository)
    destination = tmp_path / "refused.zip"
    with pytest.raises(delivery.DeliveryError, match="private_archive_path"):
        delivery.build_package(destination, root=repository)
    assert not destination.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/absolute",
        "C:drive-relative",
        "folder\\escape",
        "a/../b",
        "a//b",
        "a/./b",
        "CON.txt",
        "file.",
        "file ",
        "stream:secret",
        ".git/config",
        "manifest.json",
        "Manifest.JSON",
        "nul\x00truncated",
        "wild*card",
        "line\nbreak",
    ],
)
def test_archive_entry_names_are_portable_and_cannot_escape(name):
    with pytest.raises(delivery.DeliveryError):
        delivery._safe_path(name)


def test_existing_output_is_never_overwritten(repository, tmp_path):
    destination = tmp_path / "existing.zip"
    destination.write_bytes(b"keep-existing")
    with pytest.raises(FileExistsError):
        delivery.build_package(destination, root=repository)
    assert destination.read_bytes() == b"keep-existing"


def test_concurrent_output_creation_is_not_deleted(repository, tmp_path, monkeypatch):
    destination = tmp_path / "raced.zip"
    original = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == destination and mode == "xb":
            with original(path, "wb") as output:
                output.write(b"another-process")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        delivery.build_package(destination, root=repository)
    assert destination.read_bytes() == b"another-process"


def test_partial_output_cleanup_does_not_delete_other_files(
    repository, tmp_path, monkeypatch
):
    destination = tmp_path / "partial.zip"
    unrelated = tmp_path / "unrelated.zip"
    unrelated.write_bytes(b"keep")

    def fail(*args, **kwargs):
        raise OSError("PRIVATE INTERNAL DETAIL")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail)
    with pytest.raises(OSError):
        delivery.build_package(destination, root=repository)
    assert not destination.exists() and unrelated.read_bytes() == b"keep"


def test_head_change_during_write_refuses_and_removes_only_its_partial(
    repository, tmp_path, monkeypatch
):
    destination = tmp_path / "changed-head.zip"
    original = zipfile.ZipFile.writestr
    changed = False

    def move_head(archive, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            (repository / "README.md").write_text("new revision", encoding="utf-8")
            commit(repository)
        return original(archive, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", move_head)
    with pytest.raises(delivery.DeliveryError, match="revision_changed_during_package"):
        delivery.build_package(destination, root=repository)
    assert not destination.exists()
    assert (repository / "README.md").read_text(encoding="utf-8") == "new revision"


@pytest.mark.parametrize("mode,kind", [("120000", "blob"), ("160000", "commit")])
def test_symlinks_and_submodules_are_refused(
    repository, tmp_path, monkeypatch, mode, kind
):
    original = delivery._git

    def git_entry(root, *arguments):
        if arguments[0] == "ls-tree":
            return f"{mode} {kind} {'a' * 40}\tunsafe-link\0".encode()
        return original(root, *arguments)

    monkeypatch.setattr(delivery, "_git", git_entry)
    with pytest.raises(delivery.DeliveryError, match="unsupported_git_entry"):
        delivery.build_package(tmp_path / "refused.zip", root=repository)


def test_committed_secret_is_refused_without_printing_its_value(
    repository, tmp_path, monkeypatch, capsys
):
    secret = "PRIVATE_KEY_NEVER_PRINT_THIS_VALUE"
    (repository / "fixture.bin").write_bytes(b"\x00" + secret.encode() + b"\xff")
    commit(repository)
    monkeypatch.setattr(
        delivery, "_configured_secrets", lambda: {secret: "model_api_key"}
    )
    original = delivery.build_package
    monkeypatch.setattr(
        delivery, "build_package", lambda target: original(target, root=repository)
    )
    destination = tmp_path / "refused.zip"
    assert delivery.main([str(destination)]) == 1
    captured = capsys.readouterr()
    assert not captured.out and secret not in captured.err
    error = json.loads(captured.err)
    assert error == {
        "error": "configured_secret_detected",
        "hits": [{"field": "model_api_key", "file": "fixture.bin"}],
    }
    assert not destination.exists()


def test_secret_in_filename_is_redacted_in_diagnostics(
    repository, tmp_path, monkeypatch
):
    secret = "PRIVATE_FILENAME_KEY"
    (repository / (secret + ".txt")).write_text("public", encoding="utf-8")
    commit(repository)
    monkeypatch.setattr(
        delivery, "_configured_secrets", lambda: {secret: "amap_web_key"}
    )
    with pytest.raises(delivery.DeliveryError) as caught:
        delivery.build_package(tmp_path / "refused.zip", root=repository)
    assert secret not in json.dumps(caught.value.hits)
    assert caught.value.hits == [{"field": "amap_web_key", "file": "[REDACTED].txt"}]


def test_configured_secret_selection_keeps_auth_url_tokens_but_allows_public_loopback_and_model_id(
    monkeypatch,
):
    bare = "http://127.0.0.1:18061/mcp"
    protected = "http://localhost:18060/mcp?token=PRIVATE_QUERY_TOKEN"
    settings = Settings(
        rail_mcp=bare,
        xhs_mcp=protected,
        model_id="public-model",
        model_api_key="PRIVATE_MODEL_KEY",
        secrets={bare, protected, "PRIVATE_MODEL_KEY", "public-model"},
    )
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    secrets = delivery._configured_secrets()
    assert bare not in secrets and "public-model" not in secrets
    assert secrets[protected] == "xhs_mcp"
    assert secrets["PRIVATE_QUERY_TOKEN"] == "xhs_mcp.credential"
    assert secrets["PRIVATE_MODEL_KEY"] == "model_api_key"


def test_version_mismatch_is_refused(repository, tmp_path):
    (repository / "backend/pyproject.toml").write_text(
        '[project]\nversion="0.2.0"\n', encoding="utf-8"
    )
    commit(repository)
    with pytest.raises(delivery.DeliveryError, match="product_version_mismatch"):
        delivery.build_package(tmp_path / "refused.zip", root=repository)
