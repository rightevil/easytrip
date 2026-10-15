"""Consistent local SQLite copies; never overwrite a database or run migrations."""

import argparse
import json
import math
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic


class MaintenanceError(RuntimeError):
    pass


def _deadline(timeout_seconds):
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("maintenance_timeout_invalid")
    return monotonic() + timeout_seconds


def _migrations():
    paths = sorted((Path(__file__).parent / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    if not paths or [int(path.name[:3]) for path in paths] != list(
        range(1, len(paths) + 1)
    ):
        raise MaintenanceError("database_migrations_invalid")
    return paths


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _inspect(connection, deadline):
    connection.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
    migrations = _migrations()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 1 <= version <= len(migrations):
        raise MaintenanceError("database_version_unsupported")
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise MaintenanceError("database_integrity_failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise MaintenanceError("database_foreign_keys_invalid")
    with closing(sqlite3.connect(":memory:")) as expected:
        for path in migrations[:version]:
            expected.executescript(path.read_text(encoding="utf-8"))
        tables = [
            row[0]
            for row in expected.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        counts = {}
        actual_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in tables:
            table = _quoted(name)
            required = {
                row[1]: (row[2], row[3], row[5])
                for row in expected.execute(f"PRAGMA table_info({table})")
            }
            actual = {
                row[1]: (row[2], row[3], row[5])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if name not in actual_tables or any(
                actual.get(key) != value for key, value in required.items()
            ):
                raise MaintenanceError("database_schema_incompatible")
            foreign_keys = {
                tuple(row[2:])
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            }
            if any(
                tuple(row[2:]) not in foreign_keys
                for row in expected.execute(f"PRAGMA foreign_key_list({table})")
            ):
                raise MaintenanceError("database_schema_incompatible")
            counts[name] = connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
    if monotonic() >= deadline:
        raise MaintenanceError("database_maintenance_timeout")
    return {"schema_version": version, "table_counts": counts}


def _source_connection(source, deadline):
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise MaintenanceError("database_source_not_file")
    connection = sqlite3.connect(
        source.as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        metadata = _inspect(connection, deadline)
    except BaseException:
        connection.close()
        raise
    return connection, metadata


def inspect_database(source: Path, *, timeout_seconds: float = 60) -> dict:
    deadline = _deadline(timeout_seconds)
    try:
        connection, metadata = _source_connection(source, deadline)
        connection.close()
        return metadata
    except sqlite3.Error:
        raise MaintenanceError("database_validation_failed") from None


def _require_new_destination(source, destination):
    destination = Path(destination).absolute()
    if any(
        os.path.lexists(str(destination) + suffix)
        for suffix in ("", "-wal", "-shm", "-journal")
    ):
        raise FileExistsError("database_destination_exists")
    if Path(source).resolve() == destination.resolve():
        raise FileExistsError("database_destination_is_source")
    return destination


def backup_database(
    source: Path, destination: Path, *, timeout_seconds: float = 60
) -> dict:
    deadline = _deadline(timeout_seconds)
    destination = _require_new_destination(source, destination)
    owned = None
    try:
        source_connection, source_metadata = _source_connection(source, deadline)
        with closing(source_connection):
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as output:
                owned = os.fstat(output.fileno())
            # Recheck sidecars after claiming the file, before SQLite can open one.
            if any(
                os.path.lexists(str(destination) + suffix)
                for suffix in ("-wal", "-shm", "-journal")
            ):
                raise FileExistsError("database_destination_sidecar_exists")
            with closing(
                sqlite3.connect(
                    destination.as_uri() + "?mode=rw", uri=True, isolation_level=None
                )
            ) as target:
                target.execute("PRAGMA journal_mode=DELETE")

                def progress(status, remaining, total):
                    if monotonic() >= deadline:
                        raise MaintenanceError("database_maintenance_timeout")

                source_connection.backup(
                    target, pages=256, progress=progress, sleep=0.1
                )
                target.execute("PRAGMA journal_mode=DELETE")
                metadata = _inspect(target, deadline)
                if metadata != source_metadata:
                    raise MaintenanceError("database_backup_verification_failed")
        return metadata
    except BaseException as error:
        if owned is not None:
            try:
                if os.path.samestat(destination.lstat(), owned):
                    destination.unlink()
            except FileNotFoundError:
                pass
        if isinstance(error, sqlite3.Error):
            raise MaintenanceError("database_backup_failed") from None
        raise


def restore_database(
    source: Path, destination: Path, *, timeout_seconds: float = 60
) -> dict:
    return backup_database(source, destination, timeout_seconds=timeout_seconds)


def main(argv=None):
    from ..config import DATA_DIR

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "backup", "restore"))
    parser.add_argument("--source", type=Path, default=DATA_DIR / "easytrip.sqlite3")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if args.action == "restore" and args.destination is None:
        parser.error("restore requires --destination pointing to a new file")
    try:
        if args.action == "check":
            metadata = inspect_database(args.source)
        else:
            destination = args.destination or args.source.parent / "backups" / (
                "easytrip-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                + ".sqlite3"
            )
            operation = backup_database if args.action == "backup" else restore_database
            metadata = operation(args.source, destination)
            metadata = {**metadata, "destination": str(destination.resolve())}
        print(json.dumps(metadata, ensure_ascii=True))
        return 0
    except (MaintenanceError, OSError, sqlite3.Error, ValueError) as error:
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
