import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..domain.models import (
    ArchiveTrip,
    CreateTrip,
    MessageCreate,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)


class NotFound(Exception):
    pass


class VersionConflict(Exception):
    def __init__(self, current_version):
        self.current_version = current_version


class OperationConflict(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError("数据库版本高于应用支持版本，请使用匹配的应用")
            connection.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                migration = (
                    Path(__file__).parent / "migrations" / "001_initial.sql"
                ).read_text(encoding="utf-8")
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration
                    + "\nPRAGMA user_version=1;\nCOMMIT;"
                )
            # Fail without resetting user data when an existing database is incompatible.
            connection.execute(
                "SELECT id, request_version, workspace_version FROM trips LIMIT 0"
            )
            connection.execute("SELECT id, trip_id, operation_id FROM messages LIMIT 0")
            connection.execute(
                "SELECT id, generation, checkpoint_json FROM runs LIMIT 0"
            )

    @staticmethod
    def _row(connection, trip_id):
        row = connection.execute(
            "SELECT * FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
        if row is None:
            raise NotFound()
        return row

    @staticmethod
    def _version(row, expected, key="request_version"):
        if row[key] != expected:
            raise VersionConflict(row[key])

    @staticmethod
    def _editable(row):
        if row["status"] == "archived":
            raise OperationConflict("已归档行程需先恢复")

    @staticmethod
    def _trip(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "status": row["status"],
            "request": TripRequest.model_validate_json(row["request_json"]).model_dump(
                mode="json"
            ),
            "request_version": row["request_version"],
            "workspace": Workspace.model_validate_json(
                row["workspace_json"]
            ).model_dump(mode="json"),
            "workspace_version": row["workspace_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _run(row):
        result = dict(row)
        for key in ("params", "budget", "checkpoint", "usage", "result"):
            raw = result.pop(key + "_json")
            result[key] = json.loads(raw) if raw is not None else None
        result.pop("operation_id")
        return result

    def _detail(self, connection, row):
        result = self._trip(row)
        result["messages"] = [
            {
                key: item[key]
                for key in ("id", "trip_id", "role", "content", "status", "created_at")
            }
            for item in connection.execute(
                "SELECT * FROM messages WHERE trip_id=? ORDER BY rowid", (row["id"],)
            )
        ]
        result["runs"] = [
            self._run(item)
            for item in connection.execute(
                "SELECT * FROM runs WHERE trip_id=? ORDER BY created_at DESC, id DESC LIMIT 20",
                (row["id"],),
            )
        ]
        return result

    def create_trip(self, body: CreateTrip):
        payload = encode(body.model_dump(mode="json", exclude={"operation_id"}))
        with self.connection(write=True) as connection:
            previous = connection.execute(
                "SELECT * FROM trips WHERE create_operation_id=?",
                (str(body.operation_id),),
            ).fetchone()
            if previous is not None:
                if previous["create_payload_json"] != payload:
                    raise OperationConflict("操作ID已用于不同的新建请求")
                return self._detail(connection, previous)
            trip_id, timestamp = str(uuid4()), now()
            connection.execute(
                "INSERT INTO trips (id, create_operation_id, create_payload_json, name, request_json, workspace_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    trip_id,
                    str(body.operation_id),
                    payload,
                    body.name,
                    encode(body.request.model_dump(mode="json")),
                    encode(Workspace().model_dump(mode="json")),
                    timestamp,
                    timestamp,
                ),
            )
            return self._detail(connection, self._row(connection, trip_id))

    def get_trip(self, trip_id):
        with self.connection() as connection:
            connection.execute("BEGIN")
            return self._detail(connection, self._row(connection, trip_id))

    def list_trips(self, *, include_archived=False, limit=50, offset=0):
        with self.connection() as connection:
            connection.execute("BEGIN")
            where = "" if include_archived else "WHERE status != 'archived'"
            total = connection.execute(
                f"SELECT count(*) FROM trips {where}"
            ).fetchone()[0]
            items = connection.execute(
                f"SELECT * FROM trips {where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return {
                "items": [self._trip(row) for row in items],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def update_trip(self, trip_id, body: TripPatch):
        with self.connection(write=True) as connection:
            row = self._row(connection, trip_id)
            self._editable(row)
            self._version(row, body.expected_version)
            connection.execute(
                "UPDATE trips SET name=?, request_json=?, request_version=request_version+1, updated_at=? WHERE id=?",
                (
                    body.name if body.name is not None else row["name"],
                    encode(body.request.model_dump(mode="json"))
                    if body.request is not None
                    else row["request_json"],
                    now(),
                    trip_id,
                ),
            )
            return self._detail(connection, self._row(connection, trip_id))

    def save_workspace(self, trip_id, body: WorkspacePatch):
        with self.connection(write=True) as connection:
            row = self._row(connection, trip_id)
            self._editable(row)
            self._version(row, body.expected_version, "workspace_version")
            connection.execute(
                "UPDATE trips SET workspace_json=?, workspace_version=workspace_version+1, updated_at=? WHERE id=?",
                (encode(body.workspace.model_dump(mode="json")), now(), trip_id),
            )
            return self._detail(connection, self._row(connection, trip_id))

    def add_message(self, trip_id, body: MessageCreate):
        with self.connection(write=True) as connection:
            row = self._row(connection, trip_id)
            self._editable(row)
            previous = connection.execute(
                "SELECT * FROM messages WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if previous is not None:
                if previous["content"] != body.content:
                    raise OperationConflict("操作ID已用于不同的消息")
                message = dict(previous)
            else:
                message = {
                    "id": str(uuid4()),
                    "trip_id": trip_id,
                    "role": "user",
                    "content": body.content,
                    "status": "pending",
                    "created_at": now(),
                }
                connection.execute(
                    "INSERT INTO messages (id, trip_id, operation_id, content, created_at) VALUES (?,?,?,?,?)",
                    (
                        message["id"],
                        trip_id,
                        str(body.operation_id),
                        body.content,
                        message["created_at"],
                    ),
                )
                connection.execute(
                    "UPDATE trips SET updated_at=? WHERE id=?",
                    (message["created_at"], trip_id),
                )
            message.pop("operation_id", None)
            return message

    def archive_trip(self, trip_id, body: ArchiveTrip):
        with self.connection(write=True) as connection:
            row = self._row(connection, trip_id)
            self._version(row, body.expected_version)
            status = "archived" if body.archived else "draft"
            if status != row["status"]:
                connection.execute(
                    "UPDATE trips SET status=?, request_version=request_version+1, updated_at=? WHERE id=?",
                    (status, now(), trip_id),
                )
                if body.archived:
                    connection.execute(
                        "UPDATE runs SET status='cancelled', generation=generation+1, stop_reason='trip_archived', updated_at=? WHERE trip_id=? AND status IN ('pending','running','recoverable')",
                        (now(), trip_id),
                    )
            return self._detail(connection, self._row(connection, trip_id))

    def delete_trip(self, trip_id, expected_version):
        with self.connection(write=True) as connection:
            row = self._row(connection, trip_id)
            self._version(row, expected_version)
            connection.execute(
                "UPDATE runs SET status='cancelled', generation=generation+1, stop_reason='trip_deleted', updated_at=? WHERE trip_id=?",
                (now(), trip_id),
            )
            connection.execute("DELETE FROM trips WHERE id=?", (trip_id,))
