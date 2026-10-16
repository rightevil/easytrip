"""Build a credential-checked source ZIP from a clean, fixed Git HEAD."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PARTS = {
    ".git",
    "data",
    "reports",
    "node_modules",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".planning",
}


class DeliveryError(RuntimeError):
    def __init__(self, code, *, hits=None):
        super().__init__(code)
        self.code = code
        self.hits = hits or []


def _git(root, *arguments):
    environment = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(name, None)
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        raise DeliveryError("git_read_failed") from None


def _clean_head(root):
    if (
        Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        != root
    ):
        raise DeliveryError("repository_root_required")
    revision = _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40,64}", revision):
        raise DeliveryError("invalid_revision")
    if _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
        "--ignore-submodules=none",
    ):
        raise DeliveryError("tracked_worktree_dirty")
    # Hidden index flags can conceal modified tracked files from normal status.
    if any(
        row[:1].islower() or row[:1] == b"S"
        for row in _git(root, "ls-files", "-v", "-z").split(b"\0")
        if row
    ):
        raise DeliveryError("hidden_index_flags")
    return revision


def _safe_path(name):
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or PureWindowsPath(name).drive
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise DeliveryError("unsafe_archive_path")
    if any(
        re.search(r'[<>:"|?*\x00-\x1f]', part)
        or part.rstrip(" .") != part
        or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)
        for part in path.parts
    ):
        raise DeliveryError("unsafe_archive_path")
    lowered = [part.casefold() for part in path.parts]
    filename = lowered[-1]
    if (
        any(part in PRIVATE_PARTS for part in lowered)
        or (
            filename != ".env.example"
            and (filename == ".env" or filename.startswith(".env."))
        )
        or re.fullmatch(r"(?:.*[._-])?cookies?(?:[._-].*)?", filename)
        or filename
        in {
            "credentials.json",
            "credentials.toml",
            "credentials.yaml",
            "credentials.yml",
            "id_rsa",
            "id_ed25519",
            ".git-credentials",
            ".netrc",
            ".npmrc",
            ".pypirc",
        }
        or re.search(
            r"\.(?:sqlite3?|db|log|pem|key|p12|pfx)(?:-(?:wal|shm|journal))?$", filename
        )
    ):
        raise DeliveryError("private_archive_path")
    if name.casefold() == "manifest.json":
        raise DeliveryError("reserved_manifest_path")
    return name


def _public_mcp(value):
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"http", "https"}
            and url.hostname in {"localhost", "127.0.0.1", "::1"}
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path == "/mcp"
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        return False


def _configured_secrets():
    sys.path.insert(0, str(ROOT / "backend"))
    try:
        from easytrip.config import Settings

        settings = Settings.load()
    finally:
        sys.path.pop(0)
    secrets = {}
    for value in settings.secrets:
        if not isinstance(value, str) or not value:
            continue
        fields = [
            name
            for name, item in vars(settings).items()
            if name not in {"model_id", "secrets"} and item == value
        ]
        if not fields and value == settings.model_id:
            continue
        if (
            fields
            and all(name in {"xhs_mcp", "rail_mcp", "amap_mcp_url"} for name in fields)
            and _public_mcp(value)
        ):
            continue
        label = ",".join(sorted(fields)) or "configured_secret"
        secrets[value] = label
        try:
            query = urlsplit(value).query
        except ValueError:
            continue
        for key, credential in parse_qsl(query):
            if credential and re.fullmatch(
                r"(?:api[_-]?)?key|(?:access[_-]?)?token|secret|security[_-]?code",
                key,
                re.I,
            ):
                secrets[credential] = label + ".credential"
    return secrets


def _scan(name, payload, secrets):
    hits = []
    for value, field in secrets.items():
        if value.encode("utf-8") in payload or value in name:
            safe_name = name
            for secret in sorted(secrets, key=len, reverse=True):
                safe_name = safe_name.replace(secret, "[REDACTED]")
            hits.append({"field": field, "file": safe_name})
    return hits


def build_package(destination, *, root=ROOT):
    root, destination = Path(root).resolve(), Path(destination).absolute()
    if os.path.lexists(destination):
        raise FileExistsError("delivery_destination_exists")
    revision = _clean_head(root)
    entries, seen = [], set()
    for entry in _git(root, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, identifier = metadata.split()
        try:
            name = _safe_path(raw_name.decode("utf-8"))
        except UnicodeDecodeError:
            raise DeliveryError("non_utf8_archive_path") from None
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise DeliveryError("unsupported_git_entry")
        if name.casefold() in seen:
            raise DeliveryError("duplicate_archive_path")
        seen.add(name.casefold())
        entries.append((name, mode, identifier.decode("ascii")))
    secrets, blobs, hits = _configured_secrets(), {}, []
    for name, mode, identifier in entries:
        payload = _git(root, "show", identifier)
        hits.extend(_scan(name, payload, secrets))
        blobs[name] = (mode, payload)
    if hits:
        raise DeliveryError("configured_secret_detected", hits=hits)
    try:
        version = tomllib.loads(blobs[".cz.toml"][1].decode("utf-8"))["tool"][
            "commitizen"
        ]["version"]
        backend_version = tomllib.loads(
            blobs["backend/pyproject.toml"][1].decode("utf-8")
        )["project"]["version"]
        if not isinstance(version, str) or version != backend_version:
            raise ValueError
    except (KeyError, UnicodeDecodeError, ValueError):
        raise DeliveryError("product_version_mismatch") from None
    manifest = {
        "format_version": 1,
        "revision": revision,
        "version": version,
        "files": {
            name: {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
            for name, (_, payload) in blobs.items()
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    hits = _scan("manifest.json", manifest_bytes, secrets)
    if hits:
        raise DeliveryError("configured_secret_detected", hits=hits)
    owned = None
    try:
        with destination.open("xb") as output:
            owned = os.fstat(output.fileno())
            with zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for name, (mode, payload) in {
                    **blobs,
                    "manifest.json": (b"100644", manifest_bytes),
                }.items():
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system, info.external_attr = 3, int(mode, 8) << 16
                    archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED)
            if _clean_head(root) != revision:
                raise DeliveryError("revision_changed_during_package")
        return manifest
    except BaseException:
        if owned is not None:
            try:
                if os.path.samestat(destination.lstat(), owned):
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", type=Path, help="new ZIP file; parent directory must exist"
    )
    args = parser.parse_args(argv)
    try:
        manifest = build_package(args.destination)
        print(
            json.dumps(
                {
                    "revision": manifest["revision"],
                    "version": manifest["version"],
                    "files": len(manifest["files"]),
                    "sha256": hashlib.sha256(args.destination.read_bytes()).hexdigest(),
                }
            )
        )
        return 0
    except DeliveryError as error:
        print(
            json.dumps({"error": error.code, "hits": error.hits}, ensure_ascii=True),
            file=sys.stderr,
        )
    except (OSError, ValueError, zipfile.BadZipFile):
        print(json.dumps({"error": "delivery_failed"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
