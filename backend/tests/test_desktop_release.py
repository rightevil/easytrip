import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyInstaller", reason="optional desktop build dependency")
spec = importlib.util.spec_from_file_location("desktop_release", Path(__file__).resolve().parents[2] / "scripts/desktop_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def source(tmp_path, monkeypatch):
    (tmp_path / "backend/easytrip/storage/migrations").mkdir(parents=True)
    (tmp_path / "backend/easytrip/storage/migrations/001.sql").write_text("SELECT 1")
    (tmp_path / ".cz.toml").write_text('[tool.commitizen]\nversion="0.3.0"\n')
    (tmp_path / "backend/pyproject.toml").write_text('[project]\nversion="0.3.0"\n')
    (tmp_path / "backend/uv.lock").write_text('[[package]]\nname="easytrip"\nversion="0.3.0"\n')
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/EasyTrip.exe").write_bytes(b"fixture-executable")
    (tmp_path / "CHANGELOG.md").write_text("## v0.3.0 (date)\n\nNew version\n\n## v0.2.0 (date)\nOld version\n")
    names = {"frontend/dist/index.html", "easytrip/storage/migrations/001.sql"}
    monkeypatch.setattr(release, "CArchiveReader", lambda _: SimpleNamespace(toc={name: None for name in names}))
    monkeypatch.delenv("GITHUB_REF", raising=False)
    return tmp_path, names


def test_release_uses_consistent_version_checksums_and_current_changelog_only(source):
    root, _ = source
    asset = release.prepare(root)
    assert asset.name == "EasyTrip-0.3.0-windows-x64.exe"
    assert asset.read_bytes() == b"fixture-executable"
    assert (asset.parent / "SHA256SUMS.txt").read_text().split()[0] == hashlib.sha256(asset.read_bytes()).hexdigest()
    notes = (asset.parent / "release-notes.md").read_text(encoding="utf-8")
    assert "New version" in notes and "Old version" not in notes


@pytest.mark.parametrize("failure", ["version", "tag", "private-file", "frontend", "migration"])
def test_release_rejects_mismatched_version_tag_or_incomplete_private_bundle(source, monkeypatch, failure):
    root, names = source
    if failure == "version":
        (root / "backend/pyproject.toml").write_text('[project]\nversion="0.2.0"\n')
    elif failure == "tag":
        monkeypatch.setenv("GITHUB_REF", "refs/tags/v0.2.0")
    elif failure == "private-file":
        names.add("private/.env")
    elif failure == "frontend":
        names.remove("frontend/dist/index.html")
    else:
        names.remove("easytrip/storage/migrations/001.sql")
    with pytest.raises(ValueError):
        release.prepare(root)
    assert not (root / "dist/release").exists()
