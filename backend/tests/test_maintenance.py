import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from easytrip.config import Settings
from easytrip.domain.discovery import SourceComment, SourceNote
from easytrip.domain.models import CreateTrip, MessageCreate
from easytrip.domain.tasks import CreateRun
from easytrip.main import create_app
from easytrip.storage import maintenance
from easytrip.storage.database import Database
from easytrip.storage.maintenance import (
    MaintenanceError,
    backup_database,
    inspect_database,
    restore_database,
)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "source.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4(), name="恢复验收"))
    database.create_trip(CreateTrip(operation_id=uuid4(), name="另一个独立行程"))
    database.add_message(
        trip["id"], MessageCreate(operation_id=uuid4(), content="保留我的原文")
    )
    database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1)
    )
    run = database.claim_run()
    note = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceNote(external_id="note-1", title="公开测试", text="原文和评论都要保留"),
    )
    database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceComment(
            external_id="comment-1", parent_source_id=note["id"], text="评论原文"
        ),
    )
    return database, trip, run


def digest(path):
    with closing(sqlite3.connect(path)) as connection:
        return hashlib.sha256(
            "\n".join(connection.iterdump()).encode("utf-8")
        ).hexdigest()


def test_backup_includes_committed_wal_without_changing_source(database, tmp_path):
    database, trip, _ = database
    destination = tmp_path / "备份 #1.sqlite3"
    with closing(sqlite3.connect(database.path, isolation_level=None)) as writer:
        writer.execute(
            "UPDATE trips SET name='尚在WAL中的更新' WHERE id=?", (trip["id"],)
        )
        wal = Path(str(database.path) + "-wal")
        assert wal.stat().st_size > 0
        original_file, original_wal = database.path.read_bytes(), wal.read_bytes()
        before = digest(database.path)
        metadata = backup_database(database.path, destination)
        assert database.path.read_bytes() == original_file
        assert wal.read_bytes() == original_wal
        assert digest(destination) == before
        assert Database(destination).get_trip(trip["id"])["name"] == "尚在WAL中的更新"
    assert metadata["schema_version"] == 8
    assert metadata["table_counts"]["trips"] == 2
    assert (
        metadata["table_counts"]["sources"]
        == metadata["table_counts"]["source_snapshots"]
        == 2
    )
    assert not Path(str(destination) + "-wal").exists()
    assert not Path(str(destination) + "-shm").exists()


def test_restored_copy_retains_all_rows_and_reopens_actual_application(
    database, tmp_path
):
    database, trip, run = database
    backup, restored = tmp_path / "backup.sqlite3", tmp_path / "restored.sqlite3"
    original = digest(database.path)
    backup_database(database.path, backup)
    metadata = restore_database(backup, restored)
    assert digest(database.path) == digest(backup) == digest(restored) == original
    assert metadata == inspect_database(restored)
    with TestClient(
        create_app(Settings(), tmp_path, database_path=restored),
        base_url="http://127.0.0.1",
    ) as client:
        reopened = client.get(f"/api/trips/{trip['id']}").json()
        assert reopened["name"] == "恢复验收"
        assert reopened["messages"][0]["content"] == "保留我的原文"
        assert reopened["runs"][0]["id"] == run["id"]
        assert reopened["runs"][0]["status"] == "recoverable"
    assert digest(database.path) == digest(backup) == original


@pytest.mark.parametrize("version", range(1, 9))
def test_supported_older_versions_are_checked_and_copied_without_migration(
    tmp_path, version
):
    source, destination = tmp_path / "old.sqlite3", tmp_path / "backup.sqlite3"
    migrations = Path(maintenance.__file__).parent / "migrations"
    with sqlite3.connect(source) as connection:
        for path in sorted(migrations.glob("[0-9][0-9][0-9]_*.sql"))[:version]:
            connection.executescript(path.read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version={version}")
    before = source.read_bytes()
    assert backup_database(source, destination)["schema_version"] == version
    assert inspect_database(destination)["schema_version"] == version
    assert source.read_bytes() == before


@pytest.mark.parametrize("operation", [backup_database, restore_database])
def test_same_path_is_rejected_without_change(database, operation):
    database, _, _ = database
    before = digest(database.path)
    with pytest.raises(FileExistsError):
        operation(database.path, database.path)
    assert digest(database.path) == before


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("operation", [backup_database, restore_database])
def test_existing_destination_or_sidecar_is_never_overwritten(
    database, tmp_path, suffix, operation
):
    database, _, _ = database
    destination = tmp_path / "protected.sqlite3"
    protected = Path(str(destination) + suffix)
    protected.write_bytes(b"someone else's data")
    with pytest.raises(FileExistsError):
        operation(database.path, destination)
    assert protected.read_bytes() == b"someone else's data"
    if suffix:
        assert not destination.exists()


def test_hardlink_to_source_is_not_overwritten(database, tmp_path):
    database, _, _ = database
    destination = tmp_path / "hardlink.sqlite3"
    try:
        destination.hardlink_to(database.path)
    except OSError:
        pytest.skip("filesystem does not support hardlinks")
    before = digest(database.path)
    with pytest.raises(FileExistsError):
        backup_database(database.path, destination)
    assert digest(database.path) == digest(destination) == before


@pytest.mark.parametrize(
    "kind", ["corrupt", "ordinary", "zero", "future", "missing_schema", "foreign_keys"]
)
def test_invalid_databases_are_rejected_before_destination_creation(
    database, tmp_path, kind
):
    database, trip, _ = database
    source, destination = tmp_path / "invalid.sqlite3", tmp_path / "never.sqlite3"
    if kind == "corrupt":
        source.write_bytes(b"not a database: PRIVATE_PAYLOAD")
    elif kind in {"ordinary", "zero"}:
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE unrelated(private TEXT)")
            if kind == "ordinary":
                connection.execute("PRAGMA user_version=1")
    else:
        backup_database(database.path, source)
        with sqlite3.connect(source) as connection:
            if kind == "future":
                connection.execute("PRAGMA user_version=999")
            elif kind == "missing_schema":
                connection.execute("DROP TABLE manual_candidates")
            else:
                connection.execute(
                    "UPDATE messages SET trip_id=? WHERE trip_id=?",
                    ("missing", trip["id"]),
                )
    before = source.read_bytes()
    with pytest.raises(MaintenanceError):
        inspect_database(source)
    with pytest.raises(MaintenanceError):
        restore_database(source, destination)
    assert source.read_bytes() == before
    assert not destination.exists()


def test_missing_source_is_not_created(tmp_path):
    source, destination = tmp_path / "missing.sqlite3", tmp_path / "backup.sqlite3"
    with pytest.raises(FileNotFoundError):
        backup_database(source, destination)
    assert not source.exists() and not destination.exists()


def test_validation_failure_cleans_only_its_new_partial_file(
    database, tmp_path, monkeypatch
):
    database, _, _ = database
    destination = tmp_path / "partial.sqlite3"
    unrelated = tmp_path / "unrelated.sqlite3"
    unrelated.write_bytes(b"keep")
    original = maintenance._inspect
    inspections = 0

    def fail_target(connection, deadline):
        nonlocal inspections
        inspections += 1
        if inspections == 2:
            raise MaintenanceError("PRIVATE_PAYLOAD")
        return original(connection, deadline)

    monkeypatch.setattr(maintenance, "_inspect", fail_target)
    with pytest.raises(MaintenanceError):
        backup_database(database.path, destination)
    assert not destination.exists()
    assert unrelated.read_bytes() == b"keep"


def test_destination_exclusive_creation_does_not_delete_a_concurrent_file(
    database, tmp_path, monkeypatch
):
    database, _, _ = database
    destination = tmp_path / "raced.sqlite3"
    original = Path.open

    def racing_open(path, mode="r", *args, **kwargs):
        if path == destination and mode == "xb":
            with original(path, "wb") as writer:
                writer.write(b"created by another process")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    with pytest.raises(FileExistsError):
        backup_database(database.path, destination)
    assert destination.read_bytes() == b"created by another process"


def test_timeout_does_not_leave_a_destination(database, tmp_path, monkeypatch):
    database, _, _ = database
    destination = tmp_path / "timeout.sqlite3"
    ticks = iter([0, 100])
    monkeypatch.setattr(maintenance, "monotonic", lambda: next(ticks, 100))
    with pytest.raises(MaintenanceError):
        backup_database(database.path, destination, timeout_seconds=1)
    assert not destination.exists()


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected_before_any_file_creation(tmp_path, timeout):
    with pytest.raises(ValueError):
        backup_database(
            tmp_path / "missing", tmp_path / "never", timeout_seconds=timeout
        )
    assert list(tmp_path.iterdir()) == []


def test_cli_outputs_only_counts_and_version_for_check(database, capsys):
    database, _, _ = database
    assert maintenance.main(["check", "--source", str(database.path)]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert set(payload) == {"schema_version", "table_counts"}
    assert payload["table_counts"]["trips"] == 2
    assert "保留我的原文" not in output.out and not output.err


def test_cli_backup_and_restore_require_new_explicit_restore_destination(
    database, tmp_path, capsys
):
    database, _, _ = database
    backup, restored = tmp_path / "cli.sqlite3", tmp_path / "restored.sqlite3"
    assert (
        maintenance.main(
            ["backup", "--source", str(database.path), "--destination", str(backup)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["destination"] == str(backup.resolve())
    with pytest.raises(SystemExit) as error:
        maintenance.main(["restore", "--source", str(backup)])
    assert error.value.code == 2
    capsys.readouterr()
    assert (
        maintenance.main(
            ["restore", "--source", str(backup), "--destination", str(restored)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema_version"] == 8
    assert digest(restored) == digest(backup)


@pytest.mark.parametrize("action", ["check", "backup", "restore"])
def test_cli_errors_do_not_expose_paths_sql_or_private_data(tmp_path, capsys, action):
    source = tmp_path / "PRIVATE_FILE.sqlite3"
    source.write_bytes(b"PRIVATE_PAYLOAD")
    args = [action, "--source", str(source)]
    if action != "check":
        args.extend(["--destination", str(tmp_path / "new.sqlite3")])
    assert maintenance.main(args) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err) == {"error": "MaintenanceError"}
    assert "PRIVATE" not in output.err and "SELECT" not in output.err
