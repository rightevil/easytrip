import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from easytrip.domain.models import CreateTrip, MessageCreate
from easytrip.storage import maintenance
from easytrip.storage.database import Database


def old_database(path, version=1):
    migrations = sorted(
        (Path(maintenance.__file__).parent / "migrations").glob("*.sql")
    )
    with sqlite3.connect(path) as connection:
        for migration in migrations[:version]:
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute(f"PRAGMA user_version={version}")
    database = Database(path)
    trip = database.create_trip(CreateTrip(operation_id=uuid4(), name="升级前行程"))
    database.add_message(
        trip["id"], MessageCreate(operation_id=uuid4(), content="保留原始想法")
    )
    return database, database.get_trip(trip["id"])


def test_upgrade_backs_up_original_version_data_and_does_not_repeat(tmp_path):
    database, original = old_database(tmp_path / "app.sqlite3")
    database.initialize()
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    assert maintenance.inspect_database(backups[0])["schema_version"] == 1
    assert Database(backups[0]).get_trip(original["id"]) == original
    assert database.get_trip(original["id"]) == original
    database.initialize()
    assert list((tmp_path / "backups").glob("*.sqlite3")) == backups


def test_backup_failure_aborts_before_any_migration_or_journal_change(
    tmp_path, monkeypatch
):
    database, original = old_database(tmp_path / "app.sqlite3")
    before = database.path.read_bytes()

    def fail(*args):
        raise OSError("backup storage unavailable")

    monkeypatch.setattr(maintenance, "backup_database", fail)
    with pytest.raises(OSError, match="backup storage"):
        database.initialize()
    assert database.path.read_bytes() == before
    with database.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert database.get_trip(original["id"]) == original


def test_new_and_future_database_do_not_create_upgrade_backups(tmp_path):
    path = tmp_path / "app.sqlite3"
    database = Database(path)
    database.initialize()
    assert not (tmp_path / "backups").exists()
    with database.connection() as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="高于"):
        database.initialize()
    assert not (tmp_path / "backups").exists()


def test_failed_migration_keeps_recoverable_original_backup(tmp_path, monkeypatch):
    database, original = old_database(tmp_path / "app.sqlite3")
    read_text = Path.read_text

    def broken_migration(path, *args, **kwargs):
        if path.name.startswith("002_"):
            return "THIS IS NOT VALID SQL;"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", broken_migration)
    with pytest.raises(sqlite3.Error):
        database.initialize()
    (backup,) = (tmp_path / "backups").glob("*.sqlite3")
    assert maintenance.inspect_database(backup)["schema_version"] == 1
    assert Database(backup).get_trip(original["id"]) == original
    assert database.get_trip(original["id"]) == original
