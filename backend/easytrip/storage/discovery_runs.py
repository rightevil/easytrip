"""Discovery run transactions, including atomic message start and explicit adoption."""

import json
import math
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..domain.discovery import CandidateDraft
from ..domain.discovery_tasks import (
    DISCOVERY_STAGES,
    DiscoveryCheckpoint,
    DiscoverySnapshot,
    DiscoveryUsage,
)
from ..domain.models import TripRequest, Workspace
from ..domain.tasks import CreateRun


class DiscoveryRunStore:
    def enqueue_discovery(self, trip_id, body):
        with self.connection(write=True) as connection:
            return self._enqueue_discovery(connection, trip_id, body)

    def _enqueue_discovery(self, connection, trip_id, body):
        from .database import OperationConflict, encode, now

        trip = self._row(connection, trip_id)
        self._editable(trip)
        operation = body.model_dump(mode="json", exclude={"operation_id"}, exclude_none=True)
        previous = connection.execute("SELECT * FROM runs WHERE trip_id=? AND operation_id=?", (trip_id, str(body.operation_id))).fetchone()
        if previous is not None:
            if json.loads(previous["params_json"])["operation"] != operation:
                raise OperationConflict("操作ID已用于不同的任务")
            return self._run(previous)
        self._version(trip, body.expected_version)
        if body.parent_run_id:
            parent = self._run_row(connection, str(body.parent_run_id), trip_id)
            if parent["kind"] != "discover":
                raise OperationConflict("只能继续攻略发现任务")
        messages = connection.execute("SELECT role,content FROM messages WHERE trip_id=? ORDER BY rowid DESC LIMIT 100", (trip_id,)).fetchall()
        # Bound model context while retaining the latest user input in full.
        selected, characters = [], 0
        for row in messages:
            added = len(row["content"]) + bool(selected)
            if characters + added > 16000:
                break
            selected.append(dict(row))
            characters += added
        snapshot = DiscoverySnapshot(
            request=TripRequest.model_validate_json(trip["request_json"]),
            workspace=Workspace.model_validate_json(trip["workspace_json"]),
            workspace_version=trip["workspace_version"],
            messages_count=connection.execute("SELECT count(*) FROM messages WHERE trip_id=?", (trip_id,)).fetchone()[0],
            messages=list(reversed(selected)), operation=operation,
            parent_run_id=str(body.parent_run_id) if body.parent_run_id else None,
        )
        run_id, timestamp = str(uuid4()), now()
        connection.execute(
            "INSERT INTO runs (id,trip_id,operation_id,request_version,kind,status,stage,params_json,budget_json,checkpoint_json,usage_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, trip_id, str(body.operation_id), body.expected_version, "discover", "pending", DISCOVERY_STAGES[0], encode(snapshot.model_dump(mode="json")), encode(body.budget.model_dump()), encode({"completed_stages": []}), encode(DiscoveryUsage().model_dump()), timestamp, timestamp),
        )
        return self._run(self._run_row(connection, run_id))

    def add_discovery_message(self, trip_id, body):
        from .database import OperationConflict

        with self.connection(write=True) as connection:
            message = self._add_message(connection, trip_id, body)
            operation_id = uuid5(NAMESPACE_URL, f"easytrip:{trip_id}:message:{body.operation_id}")
            run = self._enqueue_discovery(connection, trip_id, CreateRun(operation_id=operation_id, expected_version=body.expected_version, kind="discover", budget=body.budget))
            previous = connection.execute("SELECT discovery_run_id FROM messages WHERE id=?", (message["id"],)).fetchone()[0]
            if previous and previous != run["id"]:
                raise OperationConflict("消息已关联其它任务")
            connection.execute("UPDATE messages SET discovery_run_id=? WHERE id=?", (run["id"], message["id"]))
            return {**message, "discovery_run_id": run["id"], "discovery_run": run}

    def commit_discovery_stage(self, run_id, generation, stage, output, elapsed_seconds):
        from .database import NotFound, OperationConflict, encode, now

        if stage not in DISCOVERY_STAGES or not isinstance(output, dict) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError("无效探索阶段输出")
        encode(output)
        with self.connection(write=True) as connection:
            try:
                row = self._run_row(connection, run_id)
            except NotFound:
                return False
            if not self._run_current(connection, row, generation):
                return False
            checkpoint = DiscoveryCheckpoint.model_validate_json(row["checkpoint_json"])
            if row["stage"] != stage or stage != DISCOVERY_STAGES[len(checkpoint.completed_stages)]:
                raise OperationConflict("任务阶段已更新")
            usage = json.loads(row["usage_json"])
            usage["elapsed_seconds"] += elapsed_seconds
            budget = json.loads(row["budget_json"])
            if usage["elapsed_seconds"] > budget["max_seconds"]:
                connection.execute("UPDATE runs SET status='partial',stop_reason='time_budget_exhausted',usage_json=?,stage_started_at=NULL,updated_at=? WHERE id=?", (encode(usage), now(), run_id))
                return False
            payload = checkpoint.model_dump(exclude_none=True)
            payload[stage] = output
            payload["completed_stages"].append(stage)
            DiscoveryCheckpoint.model_validate(payload)
            done = len(payload["completed_stages"]) == len(DISCOVERY_STAGES)
            partial = done and bool(output.get("partial_reasons"))
            timestamp = now()
            connection.execute(
                "UPDATE runs SET checkpoint_json=?,usage_json=?,status=?,stage=?,result_json=?,error_code=NULL,stop_reason=?,stage_started_at=?,updated_at=? WHERE id=?",
                (encode(payload), encode(usage), "partial" if partial else "completed" if done else "running", "done" if done else DISCOVERY_STAGES[len(payload["completed_stages"])], encode(output) if done else None, output["partial_reasons"][0] if partial else None, None if done else timestamp, timestamp, run_id),
            )
            return True

    def set_discovery_progress(self, run_id, generation, updates):
        from .database import encode, now

        allowed = {"notes_obtained", "notes_failed", "comments_obtained", "comments_selected", "comments_analyzed", "comments_judged", "notes_analyzed", "images_obtained", "images_analyzed_unique", "images_unanalyzed", "evidence_count", "judgement_rows"}
        if set(updates) - allowed or any(type(value) is not int or value < 0 for value in updates.values()):
            raise ValueError("无效采集覆盖计数")
        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id)
            if not self._run_current(connection, row, generation):
                return False
            usage = json.loads(row["usage_json"])
            usage.update(updates)
            DiscoveryUsage.model_validate(usage)
            connection.execute("UPDATE runs SET usage_json=?,updated_at=? WHERE id=?", (encode(usage), now(), run_id))
            return True

    def adopt_direction(self, trip_id, candidate_id, body):
        from .database import NotFound, OperationConflict, encode, now

        payload = encode({"candidate_id": candidate_id, **body.model_dump(mode="json", exclude={"operation_id"})})
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            previous = connection.execute("SELECT * FROM direction_adoptions WHERE trip_id=? AND operation_id=?", (trip_id, str(body.operation_id))).fetchone()
            if previous:
                if previous["payload_json"] != payload:
                    raise OperationConflict("采用操作ID已用于其它方向")
                return json.loads(previous["result_json"])
            self._version(trip, body.expected_version)
            run = self._run_row(connection, str(body.run_id), trip_id)
            if run["request_version"] != trip["request_version"]:
                raise OperationConflict("方向来自旧条件")
            row = connection.execute("SELECT * FROM candidates WHERE id=? AND trip_id=? AND run_id=?", (candidate_id, trip_id, str(body.run_id))).fetchone()
            if row is None:
                raise NotFound()
            candidate = CandidateDraft.model_validate_json(row["payload_json"])
            if candidate.kind != "direction" or not candidate.mention_ids or not candidate.city:
                raise OperationConflict("只能采用有来源的方向")
            request = TripRequest.model_validate_json(trip["request_json"])
            request.destination = candidate.city
            connection.execute("UPDATE trips SET request_json=?,request_version=request_version+1,updated_at=? WHERE id=?", (encode(request.model_dump(mode="json")), now(), trip_id))
            result = self._detail(connection, self._row(connection, trip_id))
            connection.execute("INSERT INTO direction_adoptions(trip_id,operation_id,payload_json,result_json) VALUES(?,?,?,?)", (trip_id, str(body.operation_id), payload, encode(result)))
            return result
