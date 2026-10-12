import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from ..domain.models import (
    ArchiveTrip,
    CreateTrip,
    MessageCreate,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)
from ..domain.tasks import (
    STAGE_MODELS,
    STAGES,
    CreateRun,
    RunBudget,
    RunCheckpoint,
    RunSnapshot,
    RunUsage,
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

    @staticmethod
    def _run_row(connection, run_id, trip_id=None):
        row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or (trip_id is not None and row["trip_id"] != trip_id):
            raise NotFound()
        return row

    def enqueue_run(self, trip_id, body: CreateRun):
        body = CreateRun.model_validate(body)
        operation = body.model_dump(mode="json", exclude={"operation_id"})
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            previous = connection.execute(
                "SELECT * FROM runs WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if previous is not None:
                if json.loads(previous["params_json"])["operation"] != operation:
                    raise OperationConflict("操作ID已用于不同的任务")
                return self._run(previous)
            self._version(trip, body.expected_version)
            snapshot = RunSnapshot(
                request=TripRequest.model_validate_json(trip["request_json"]),
                workspace=Workspace.model_validate_json(trip["workspace_json"]),
                workspace_version=trip["workspace_version"],
                messages_count=connection.execute(
                    "SELECT count(*) FROM messages WHERE trip_id=?", (trip_id,)
                ).fetchone()[0],
                operation=operation,
            )
            run_id, timestamp = str(uuid4()), now()
            usage = {
                "steps": 0, "elapsed_seconds": 0.0, "external_calls": 0,
                "model_tokens": 0, "images": 0,
            }
            connection.execute(
                "INSERT INTO runs (id,trip_id,operation_id,request_version,kind,status,stage,params_json,budget_json,checkpoint_json,usage_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, trip_id, str(body.operation_id), body.expected_version,
                 body.kind, "pending", STAGES[0], encode(snapshot.model_dump(mode="json")),
                 encode(body.budget.model_dump()), encode({"completed_stages": []}),
                 encode(usage), timestamp, timestamp),
            )
            return self._run(self._run_row(connection, run_id))

    def get_run(self, run_id, trip_id=None):
        with self.connection() as connection:
            return self._run(self._run_row(connection, run_id, trip_id))

    @staticmethod
    def _elapsed_usage(row):
        usage = json.loads(row["usage_json"])
        if row["status"] == "running":
            usage["elapsed_seconds"] += max(
                0, (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            )
        return usage

    def recover_runs(self, *, graceful=False):
        with self.connection(write=True) as connection:
            for row in connection.execute("SELECT * FROM runs WHERE status IN ('pending','running')").fetchall():
                usage = self._elapsed_usage(row) if graceful else json.loads(row["usage_json"])
                if not graceful and row["status"] == "running":
                    usage["interrupted_stage_time_unknown"] = True
                connection.execute(
                    "UPDATE runs SET status='recoverable',generation=generation+1,stop_reason='app_restarted',usage_json=?,updated_at=? WHERE id=?",
                    (encode(usage), now(), row["id"]),
                )

    def claim_run(self):
        with self.connection(write=True) as connection:
            while True:
                row = connection.execute(
                    "SELECT * FROM runs WHERE status='pending' ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                try:
                    run = self._run(row)
                    RunSnapshot.model_validate(run["params"])
                    RunBudget.model_validate(run["budget"])
                    checkpoint = RunCheckpoint.model_validate(run["checkpoint"])
                    usage = RunUsage.model_validate(run["usage"])
                    if usage.steps != len(checkpoint.completed_stages) or row["stage"] != STAGES[len(checkpoint.completed_stages)] or row["kind"] != "prepare_workspace":
                        raise ValueError("无效任务阶段")
                except (ValueError, KeyError, TypeError, IndexError, ValidationError):
                    connection.execute(
                        "UPDATE runs SET status='failed',generation=generation+1,error_code='invalid_run_data',stop_reason='invalid_run_data',updated_at=? WHERE id=?",
                        (now(), row["id"]),
                    )
                    continue
                trip = self._row(connection, row["trip_id"])
                if trip["status"] == "archived" or trip["request_version"] != row["request_version"]:
                    status = "cancelled" if trip["status"] == "archived" else "stale"
                    reason = "trip_archived" if status == "cancelled" else "request_changed"
                    connection.execute(
                        "UPDATE runs SET status=?,generation=generation+1,stop_reason=?,updated_at=? WHERE id=?",
                        (status, reason, now(), row["id"]),
                    )
                    continue
                connection.execute(
                    "UPDATE runs SET status='running',generation=generation+1,updated_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                return self._run(self._run_row(connection, row["id"]))

    def _run_current(self, connection, row, generation):
        if row["status"] != "running" or row["generation"] != generation:
            return False
        trip = self._row(connection, row["trip_id"])
        if trip["status"] == "archived" or trip["request_version"] != row["request_version"]:
            status = "cancelled" if trip["status"] == "archived" else "stale"
            reason = "trip_archived" if status == "cancelled" else "request_changed"
            connection.execute(
                "UPDATE runs SET status=?,generation=generation+1,stop_reason=?,updated_at=? WHERE id=?",
                (status, reason, now(), row["id"]),
            )
            return False
        return True

    def run_is_current(self, run_id, generation):
        with self.connection(write=True) as connection:
            try:
                row = self._run_row(connection, run_id)
            except NotFound:
                return False
            return self._run_current(connection, row, generation)

    def commit_run_stage(self, run_id, generation, stage, output, elapsed_seconds):
        output = STAGE_MODELS[stage].model_validate(output).model_dump(mode="json")
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("无效执行时间")
        with self.connection(write=True) as connection:
            try:
                row = self._run_row(connection, run_id)
            except NotFound:
                return False
            if not self._run_current(connection, row, generation):
                return False
            checkpoint, usage = json.loads(row["checkpoint_json"]), json.loads(row["usage_json"])
            completed = checkpoint["completed_stages"]
            if row["stage"] != stage or completed != list(STAGES[:len(completed)]) or stage != STAGES[len(completed)]:
                raise OperationConflict("任务阶段已更新")
            budget = json.loads(row["budget_json"])
            if usage["steps"] >= budget["max_steps"] or usage["elapsed_seconds"] + elapsed_seconds > budget["max_seconds"]:
                usage["elapsed_seconds"] += elapsed_seconds
                connection.execute(
                    "UPDATE runs SET status='partial',stop_reason=?,usage_json=?,updated_at=? WHERE id=?",
                    ("step_budget_exhausted" if usage["steps"] >= budget["max_steps"] else "time_budget_exhausted", encode(usage), now(), run_id),
                )
                return False
            checkpoint[stage] = output
            completed.append(stage)
            usage["steps"] += 1
            usage["elapsed_seconds"] += elapsed_seconds
            done = len(completed) == len(STAGES)
            connection.execute(
                "UPDATE runs SET checkpoint_json=?,usage_json=?,status=?,stage=?,result_json=?,error_code=NULL,stop_reason=NULL,updated_at=? WHERE id=?",
                (encode(checkpoint), encode(usage), "completed" if done else "running",
                 "done" if done else STAGES[len(completed)], encode(output) if done else None,
                 now(), run_id),
            )
            return True

    def stop_run(self, run_id, generation, status, reason, *, elapsed_seconds=0.0):
        if status not in ("partial", "failed") or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("无效停止状态")
        with self.connection(write=True) as connection:
            try:
                row = self._run_row(connection, run_id)
            except NotFound:
                return False
            if not self._run_current(connection, row, generation):
                return False
            usage = json.loads(row["usage_json"])
            usage["elapsed_seconds"] += elapsed_seconds
            connection.execute(
                "UPDATE runs SET status=?,stop_reason=?,error_code=?,usage_json=?,updated_at=? WHERE id=?",
                (status, reason, reason if status == "failed" else None, encode(usage), now(), run_id),
            )
            return True

    def cancel_run(self, run_id, trip_id, generation):
        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id, trip_id)
            if row["generation"] != generation:
                raise OperationConflict("任务已更新，请重新读取")
            if row["status"] not in ("completed", "stale", "cancelled"):
                connection.execute(
                    "UPDATE runs SET status='cancelled',generation=generation+1,stop_reason='user_cancelled',usage_json=?,updated_at=? WHERE id=?",
                    (encode(self._elapsed_usage(row)), now(), run_id),
                )
            return self._run(self._run_row(connection, run_id))

    def resume_run(self, run_id, trip_id, generation):
        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id, trip_id)
            trip = self._row(connection, trip_id)
            self._editable(trip)
            self._version(trip, row["request_version"])
            if row["generation"] != generation or row["status"] not in ("recoverable", "cancelled", "partial", "failed"):
                raise OperationConflict("任务已更新或不能继续")
            if row["error_code"] == "invalid_run_data":
                raise OperationConflict("任务数据不完整，请新建准备任务")
            usage, budget = json.loads(row["usage_json"]), json.loads(row["budget_json"])
            if row["stop_reason"] in ("step_budget_exhausted", "time_budget_exhausted") or usage["steps"] >= budget["max_steps"] or usage["elapsed_seconds"] >= budget["max_seconds"]:
                raise OperationConflict("本轮预算已耗尽，请新建准备任务")
            connection.execute(
                "UPDATE runs SET status='pending',generation=generation+1,error_code=NULL,stop_reason=NULL,updated_at=? WHERE id=?",
                (now(), run_id),
            )
            return self._run(self._run_row(connection, run_id))
