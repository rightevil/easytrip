from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from easytrip.domain.models import (
    ArchiveTrip,
    CreateTrip,
    MessageCreate,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)
from easytrip.storage.database import (
    Database,
    NotFound,
    OperationConflict,
    VersionConflict,
)


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "app.sqlite3")
    database.initialize()
    return database


def create(database, **kwargs):
    return database.create_trip(CreateTrip(operation_id=uuid4(), **kwargs))


def test_migration_and_defaults_survive_reopen(database):
    trip = create(database)
    assert trip["request"]["destination"] is None
    assert trip["request"]["transport"] == "mixed"
    assert trip["request"]["transport_source"] == "default"
    second = Database(database.path)
    second.initialize()
    assert second.get_trip(trip["id"]) == trip
    with second.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8


def test_creation_and_messages_are_idempotent_without_conflating_payloads(database):
    body = CreateTrip(operation_id=uuid4(), name="秋游")
    first = database.create_trip(body)
    assert database.create_trip(body)["id"] == first["id"]
    with pytest.raises(OperationConflict):
        database.create_trip(body.model_copy(update={"name": "另一次出游"}))
    message = MessageCreate(operation_id=uuid4(), content="想找一个安静的地方")
    saved = database.add_message(first["id"], message)
    assert database.add_message(first["id"], message) == saved
    with pytest.raises(OperationConflict):
        database.add_message(
            first["id"], message.model_copy(update={"content": "另外的内容"})
        )
    assert len(database.get_trip(first["id"])["messages"]) == 1
    assert database.list_trips()["total"] == 1


def test_workspace_does_not_invalidate_request_and_changes_are_atomic(database):
    trip = create(database)
    updated = database.update_trip(
        trip["id"],
        TripPatch(expected_version=1, request=TripRequest(destination="杭州")),
    )
    workspace = Workspace(
        active_tab="map", category="play", map_center=[120.1, 30.2], map_zoom=14
    )
    updated = database.save_workspace(
        trip["id"], WorkspacePatch(expected_version=1, workspace=workspace)
    )
    assert updated["request_version"] == updated["workspace_version"] == 2
    with pytest.raises(VersionConflict):
        database.update_trip(trip["id"], TripPatch(expected_version=1, name="旧编辑"))
    with pytest.raises(VersionConflict):
        database.save_workspace(
            trip["id"], WorkspacePatch(expected_version=1, workspace=Workspace())
        )
    persisted = Database(database.path).get_trip(trip["id"])
    assert persisted == updated
    assert persisted["request"]["destination"] == "杭州"


def test_two_concurrent_edits_cannot_overwrite_each_other(database):
    trip = create(database)

    def edit(name):
        try:
            return database.update_trip(
                trip["id"], TripPatch(expected_version=1, name=name)
            )
        except VersionConflict as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, ["版本A", "版本B"]))
    assert sum(isinstance(item, VersionConflict) for item in results) == 1
    assert database.get_trip(trip["id"])["request_version"] == 2


def test_failed_transaction_rolls_back_and_delete_cascades(database):
    trip = create(database)
    database.add_message(
        trip["id"], MessageCreate(operation_id=uuid4(), content="出发条件")
    )
    with pytest.raises(RuntimeError):
        with database.connection(write=True) as connection:
            connection.execute(
                "UPDATE trips SET name='不应保存' WHERE id=?", (trip["id"],)
            )
            raise RuntimeError("模拟提交失败")
    assert database.get_trip(trip["id"])["name"] == trip["name"]
    with pytest.raises(VersionConflict):
        database.delete_trip(trip["id"], 2)
    database.delete_trip(trip["id"], 1)
    with pytest.raises(NotFound):
        database.get_trip(trip["id"])
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_archive_preserves_history_and_requires_restore_before_edit(database):
    trip = create(database)
    archived = database.archive_trip(
        trip["id"], ArchiveTrip(expected_version=1, archived=True)
    )
    assert archived["status"] == "archived" and archived["request_version"] == 2
    assert database.list_trips()["total"] == 0
    assert database.list_trips(include_archived=True)["total"] == 1
    with pytest.raises(OperationConflict):
        database.add_message(
            trip["id"], MessageCreate(operation_id=uuid4(), content="新增消息")
        )
    restored = database.archive_trip(
        trip["id"], ArchiveTrip(expected_version=2, archived=False)
    )
    assert restored["status"] == "draft"


@pytest.mark.parametrize(
    "data",
    [
        {"participants": True},
        {"duration_minutes": -1},
        {"budget_cny": float("inf")},
        {"start_at": "2026-10-12T10:00:00"},
        {"start_at": 1234},
        {"transport": "unknown"},
        {"destination": "x" * 201},
        {"unexpected": "field"},
    ],
)
def test_request_boundary_rejects_invalid_values(data):
    with pytest.raises(ValidationError):
        TripRequest.model_validate(data)


@pytest.mark.parametrize("center", [[181, 30], [120, 91], [120], [float("nan"), 30]])
def test_workspace_rejects_invalid_coordinates(center):
    with pytest.raises(ValidationError):
        Workspace(map_center=center)


def test_future_database_version_is_rejected_without_reset(database):
    trip = create(database)
    with database.connection() as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError):
        database.initialize()
    assert database.get_trip(trip["id"])["id"] == trip["id"]


def test_explicit_transport_and_timezone_are_preserved():
    request = TripRequest(transport="transit", start_at="2026-10-12T09:00:00+08:00")
    assert request.transport_source == "user"
    assert request.model_dump(mode="json")["start_at"] == "2026-10-12T09:00:00+08:00"
