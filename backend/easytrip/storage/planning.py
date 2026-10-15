"""Frozen planning inputs and append-only plan versions under trip ownership."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from ..domain.discovery import Activity, Evidence
from ..domain.models import TripRequest, Workspace
from ..domain.places import PoiMatch
from ..domain.planning import PLAN_STAGES, PlanningCheckpoint, PlanningUsage, SavePlan


def planning_hash(value):
    from .database import encode

    content = {
        key: value[key]
        for key in ("trip_id", "request", "origin", "selected_ids", "selected")
    }
    return hashlib.sha256(encode(content).encode("utf-8")).hexdigest()


class PlanningStore:
    def planning_replacements(self, trip_id):
        from .database import NotFound

        with self.connection() as connection:
            trip = self._row(connection, trip_id)
            current = self._planning_input(connection, trip_id)
            cities = {
                item["poi"]["city"].removesuffix("市") for item in current["selected"]
            }
            result = []
            for row in self._place_match_rows(connection, trip_id)[::-1][:100]:
                match = PoiMatch.model_validate_json(row["payload_json"])
                if (
                    row["candidate_id"] in current["selected_ids"]
                    or match.status != "unique"
                ):
                    continue
                if (
                    not match.poi.city
                    or match.poi.city.removesuffix("市") not in cities
                ):
                    continue
                try:
                    candidate, _ = self._candidate_for_places(
                        connection, trip_id, row["candidate_id"]
                    )
                except NotFound:
                    continue
                if candidate["kind"] == "place":
                    result.append(
                        {
                            "candidate_id": candidate["id"],
                            "name": candidate["name"],
                            "category": candidate["category"],
                            **(
                                {"source": "user"}
                                if candidate.get("source") == "user"
                                else {}
                            ),
                            "poi": match.poi.model_dump(mode="json"),
                        }
                    )
            return {
                "trip_id": trip_id,
                "request_version": trip["request_version"],
                "workspace_version": trip["workspace_version"],
                "candidates": result,
            }

    def replace_planning_candidate(self, trip_id, body):
        from .database import OperationConflict, encode

        encoded = encode(body.model_dump(mode="json", exclude={"operation_id"}))
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            receipt = connection.execute(
                "SELECT payload_json FROM planning_replacements WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if receipt is not None:
                if receipt["payload_json"] != encoded:
                    raise OperationConflict("替换操作ID已用于不同内容")
                return {
                    "trip": self._detail(connection, trip),
                    "operation_applied": True,
                }
            self._version(trip, body.expected_version)
            self._version(trip, body.expected_workspace_version, "workspace_version")
            current = self._planning_input(connection, trip_id)
            if current["input_hash"] != body.input_hash:
                raise OperationConflict("规划输入已改变，请重新读取后替换")
            request = TripRequest.model_validate_json(trip["request_json"])
            for item in request.planning.activity_constraints:
                if item.candidate_id == body.candidate_id and (
                    item.required
                    or item.locked
                    or item.state == "completed"
                    or item.fixed_start_at
                ):
                    raise OperationConflict("不能替换已完成、锁定、必去或固定预约项目")
                if item.candidate_id == body.replacement_id and (
                    item.excluded or item.state == "skipped"
                ):
                    raise OperationConflict("替换地点仍被排除或跳过，请先明确条件")
            workspace = Workspace.model_validate_json(trip["workspace_json"])
            if (
                body.candidate_id not in workspace.selected_ids
                or body.replacement_id in workspace.selected_ids
            ):
                raise OperationConflict("已选项目已改变，请重新读取")
            previous = next(
                (
                    item
                    for item in current["selected"]
                    if item["candidate_id"] == body.candidate_id
                ),
                None,
            )
            candidate, _ = self._candidate_for_places(
                connection, trip_id, body.replacement_id
            )
            match = self._place_match(connection, trip_id, body.replacement_id)
            if (
                not previous
                or candidate["kind"] != "place"
                or not match
                or match.status != "unique"
                or not match.poi.city
                or match.poi.city.removesuffix("市")
                != previous["poi"]["city"].removesuffix("市")
            ):
                raise OperationConflict("替换地点必须是本行程已唯一确认且同城的真实POI")
            workspace.selected_ids = [
                body.replacement_id if identifier == body.candidate_id else identifier
                for identifier in workspace.selected_ids
            ]
            self._write_place_workspace(connection, trip_id, workspace)
            connection.execute(
                "INSERT INTO planning_replacements VALUES(?,?,?)",
                (trip_id, str(body.operation_id), encoded),
            )
            return {
                "trip": self._detail(connection, self._row(connection, trip_id)),
                "operation_applied": True,
            }

    def enqueue_planning(self, trip_id, body):
        from .database import OperationConflict, encode, now

        operation = body.model_dump(
            mode="json", exclude={"operation_id"}, exclude_none=True
        )
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            previous = connection.execute(
                "SELECT * FROM runs WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if previous is not None:
                if json.loads(previous["params_json"])["operation"] != operation:
                    raise OperationConflict("操作ID已用于不同的规划任务")
                return self._run(previous)
            self._version(trip, body.expected_version)
            value = self._planning_input(connection, trip_id)
            if body.parent_plan_id:
                self._plan_row(connection, trip_id, str(body.parent_plan_id))
            value.update(
                operation=operation,
                parent_plan_id=str(body.parent_plan_id)
                if body.parent_plan_id
                else None,
            )
            identifier, timestamp = str(uuid4()), now()
            connection.execute(
                "INSERT INTO runs(id,trip_id,operation_id,request_version,kind,status,stage,params_json,budget_json,checkpoint_json,usage_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    trip_id,
                    str(body.operation_id),
                    body.expected_version,
                    "plan",
                    "pending",
                    PLAN_STAGES[0],
                    encode(value),
                    encode(body.budget.model_dump(mode="json")),
                    encode(PlanningCheckpoint().model_dump(mode="json")),
                    encode(PlanningUsage().model_dump(mode="json")),
                    timestamp,
                    timestamp,
                ),
            )
            return self._run(self._run_row(connection, identifier))

    def commit_planning_stage(self, run_id, generation, stage, output, elapsed_seconds):
        from .database import OperationConflict, encode, now

        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id)
            if not self._run_current(connection, row, generation):
                return False
            checkpoint = json.loads(row["checkpoint_json"])
            completed = checkpoint["completed_stages"]
            if (
                stage != row["stage"]
                or completed != list(PLAN_STAGES[: len(completed)])
                or stage != PLAN_STAGES[len(completed)]
            ):
                raise OperationConflict("规划阶段已更新")
            checkpoint[stage] = output
            completed.append(stage)
            PlanningCheckpoint.model_validate(checkpoint)
            usage = json.loads(row["usage_json"])
            if stage == "routing":
                usage["cache_hits"] = max(
                    (item["payload"]["usage"].get("cache_hits", 0) for item in output),
                    default=0,
                )
            usage["steps"] += 1
            usage["elapsed_seconds"] += elapsed_seconds
            done = len(completed) == len(PLAN_STAGES)
            status = (
                ("partial" if output.get("partial") else "completed")
                if done
                else "running"
            )
            connection.execute(
                "UPDATE runs SET checkpoint_json=?,usage_json=?,status=?,stage=?,result_json=?,stop_reason=?,stage_started_at=?,updated_at=? WHERE id=?",
                (
                    encode(checkpoint),
                    encode(usage),
                    status,
                    "done" if done else PLAN_STAGES[len(completed)],
                    encode(output) if done else row["result_json"],
                    "planning_partial" if status == "partial" else None,
                    None if done else now(),
                    now(),
                    run_id,
                ),
            )
            return True

    def _planning_input(self, connection, trip_id):
        from .database import NotFound

        trip = self._row(connection, trip_id)
        request = TripRequest.model_validate_json(trip["request_json"])
        workspace = Workspace.model_validate_json(trip["workspace_json"])
        origin = workspace.confirmed_origin
        if origin is not None and origin.request_origin != request.origin:
            origin = None
        missing, selected = [], []

        def gap(code, message, candidate_id=None):
            missing.append(
                {
                    "code": code,
                    "message": message,
                    **({"candidate_id": candidate_id} if candidate_id else {}),
                }
            )

        if origin is None:
            gap("origin_missing", "请明确确认出发点坐标")
        if request.start_at is None:
            gap("start_missing", "请填写带时区的出发日期与时间")
        if request.duration_minutes is None and request.planning.end_at is None:
            gap("deadline_missing", "请填写可用时间或结束期限")
        if not workspace.selected_ids:
            gap("selection_missing", "请从已确认地点中选择行程项目")
        cities = set()
        for identifier in workspace.selected_ids:
            try:
                candidate, mentions = self._candidate_for_places(
                    connection, trip_id, identifier
                )
            except NotFound:
                gap(
                    "candidate_unavailable",
                    "所选项目不属于当前行程或已不可用",
                    identifier,
                )
                continue
            match = self._place_match(connection, trip_id, identifier)
            if (
                candidate["kind"] != "place"
                or match is None
                or match.status != "unique"
            ):
                gap("poi_unconfirmed", "所选项目尚未确认真实地点", identifier)
                continue
            poi = match.poi.model_dump(mode="json")
            if poi["city"]:
                cities.add(poi["city"].removesuffix("市"))
            else:
                gap(
                    "city_unknown", "所选地点所属城市未知，无法校验单城交通", identifier
                )
            mention_ids = {item["id"] for item in mentions}
            activities = [
                self._entity(item, Activity)
                for item in connection.execute(
                    "SELECT * FROM activities WHERE trip_id=? AND run_id=?",
                    (trip_id, candidate["run_id"]),
                )
                if item["mention_id"] in mention_ids
            ]
            evidence = []
            for evidence_id in candidate["evidence_ids"]:
                item = connection.execute(
                    "SELECT * FROM evidence WHERE trip_id=? AND run_id=? AND id=?",
                    (trip_id, candidate["run_id"], evidence_id),
                ).fetchone()
                if item is not None:
                    evidence.append(self._entity(item, Evidence))
            source_ids = list(
                dict.fromkeys(
                    [item["source_id"] for item in mentions]
                    + [item["source_id"] for item in evidence]
                )
            )
            selected.append(
                {
                    "candidate_id": identifier,
                    "name": candidate["name"],
                    "category": candidate["category"],
                    "kind": candidate["kind"],
                    **({"source": "user"} if candidate.get("source") == "user" else {}),
                    "city": poi["city"],
                    "run_id": candidate["run_id"],
                    "poi": poi,
                    "activities": activities,
                    "mentions": mentions,
                    "sources": [
                        {
                            key: value
                            for key, value in self._source(
                                connection, trip_id, source_id, candidate["run_id"]
                            ).items()
                            if key not in {"first_read_at", "last_read_at"}
                        }
                        for source_id in source_ids
                    ],
                    "evidence": evidence,
                }
            )
        if trip["active_plan_id"]:
            adopted = self._plan(
                self._plan_row(connection, trip_id, trip["active_plan_id"])
            )
            inherited_ids = {
                item.candidate_id for item in request.planning.activity_constraints
            }
            for item in adopted["payload"].get("supplements", []):
                if item["candidate_id"] in inherited_ids and item[
                    "candidate_id"
                ] not in {value["candidate_id"] for value in selected}:
                    selected.append(item)
                    cities.add(item["poi"]["city"].removesuffix("市"))
        if len(cities) > 1:
            gap("cross_city", "P4 只校验单城单日；跨城行程需要后续阶段")
        selected_ids = {item["candidate_id"] for item in selected}
        for constraint in request.planning.activity_constraints:
            if constraint.candidate_id not in selected_ids and (
                constraint.required
                or constraint.locked
                or constraint.state == "completed"
            ):
                gap(
                    "constraint_unavailable",
                    "必去、锁定或已完成项目缺少可核对的地点快照",
                    constraint.candidate_id,
                )
        if (
            request.planning.end_candidate_id
            and request.planning.end_candidate_id not in selected_ids
        ):
            gap("endpoint_unavailable", "指定终点必须是本行程已选的确认地点")
        value = {
            "trip_id": trip_id,
            "request_version": trip["request_version"],
            "request": request.model_dump(mode="json"),
            "planning": request.planning.model_dump(mode="json"),
            "origin": origin.model_dump(mode="json") if origin else None,
            "selected_ids": workspace.selected_ids,
            "selected": selected,
            "missing": missing,
            "active_plan_id": trip["active_plan_id"],
        }
        value["input_hash"] = planning_hash(value)
        return value

    def planning_input(self, trip_id):
        with self.connection() as connection:
            connection.execute("BEGIN")
            return self._planning_input(connection, trip_id)

    @staticmethod
    def _plan(row):
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result.pop("operation_id")
        return result

    @staticmethod
    def _plan_row(connection, trip_id, plan_id):
        from .database import NotFound

        row = connection.execute(
            "SELECT * FROM plans WHERE id=? AND trip_id=?", (plan_id, trip_id)
        ).fetchone()
        if row is None:
            raise NotFound()
        return row

    def list_plans(self, trip_id):
        with self.connection() as connection:
            connection.execute("BEGIN")
            trip = self._row(connection, trip_id)
            return {
                "items": [
                    self._plan(row)
                    for row in connection.execute(
                        "SELECT * FROM plans WHERE trip_id=? ORDER BY version DESC LIMIT 50",
                        (trip_id,),
                    )
                ],
                "active_plan_id": trip["active_plan_id"],
            }

    def get_plan(self, trip_id, plan_id):
        with self.connection() as connection:
            return self._plan(self._plan_row(connection, trip_id, plan_id))

    def save_plan(self, trip_id, body: SavePlan):
        from .database import OperationConflict, encode, now

        if body.input_hash != planning_hash(body.payload.input):
            raise ValueError("方案的冻结输入签名不匹配")
        if body.payload.input.get("trip_id") != trip_id:
            raise ValueError("方案的冻结输入不属于当前行程")
        if body.request_version != body.payload.input["request_version"]:
            raise ValueError("方案的请求版本不匹配")
        if body.status == "validated" and (
            body.payload.input["missing"]
            or not body.payload.itinerary
            or body.payload.unknowns
            or body.payload.conflicts
            or body.payload.valid_until is None
        ):
            raise ValueError("未完整校验的方案不能声明可执行")
        if body.status in {"validated", "conditional"} and body.payload.conflicts:
            raise ValueError("存在硬约束冲突的方案不可标为可行")
        if (
            len(encode(body.payload.model_dump(mode="json")).encode("utf-8"))
            > 2_097_152
        ):
            raise ValueError("方案快照超过2MiB")
        with self.connection(write=True) as connection:
            self._editable(self._row(connection, trip_id))
            previous = connection.execute(
                "SELECT * FROM plans WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            payload = encode(body.payload.model_dump(mode="json"))
            if previous is not None:
                if (
                    previous["input_hash"],
                    previous["request_version"],
                    previous["payload_json"],
                    previous["status"],
                    previous["parent_plan_id"],
                    previous["run_id"],
                ) != (
                    body.input_hash,
                    body.request_version,
                    payload,
                    body.status,
                    str(body.parent_plan_id) if body.parent_plan_id else None,
                    str(body.run_id) if body.run_id else None,
                ):
                    raise OperationConflict("方案操作ID已用于不同内容")
                return self._plan(previous)
            if body.parent_plan_id:
                self._plan_row(connection, trip_id, str(body.parent_plan_id))
            if body.run_id:
                run = self._run_row(connection, str(body.run_id), trip_id)
                if run["request_version"] != body.request_version:
                    raise OperationConflict("任务与方案条件版本不一致")
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM plans WHERE trip_id=?",
                (trip_id,),
            ).fetchone()[0]
            identifier = str(uuid4())
            connection.execute(
                "INSERT INTO plans(id,trip_id,operation_id,version,request_version,input_hash,parent_plan_id,run_id,status,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    trip_id,
                    str(body.operation_id),
                    version,
                    body.request_version,
                    body.input_hash,
                    str(body.parent_plan_id) if body.parent_plan_id else None,
                    str(body.run_id) if body.run_id else None,
                    body.status,
                    payload,
                    now(),
                ),
            )
            return self._plan(self._plan_row(connection, trip_id, identifier))

    def adopt_plan(self, trip_id, plan_id, body):
        from .database import OperationConflict, now

        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            plan = self._plan(self._plan_row(connection, trip_id, plan_id))
            previous = connection.execute(
                "SELECT * FROM plan_adoptions WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if previous is not None:
                if (
                    previous["plan_id"],
                    previous["request_version"],
                    previous["input_hash"],
                ) != (plan_id, body.expected_version, body.input_hash):
                    raise OperationConflict("采用操作ID已用于不同方案")
                return {
                    "trip": self._detail(connection, trip),
                    "plan": plan,
                    "operation_applied": True,
                }
            self._version(trip, body.expected_version)
            current = self._planning_input(connection, trip_id)
            if (
                plan["request_version"] != body.expected_version
                or plan["input_hash"] != body.input_hash
                or current["input_hash"] != body.input_hash
            ):
                raise OperationConflict(
                    "规划条件、起点或已选项目已改变，请重新校验方案"
                )
            if (
                plan["status"] not in {"validated", "conditional"}
                or current["missing"]
                or plan["payload"]["conflicts"]
                or not plan["payload"]["itinerary"]
            ):
                raise OperationConflict("缺输入、部分结果或冲突方案不能采用")
            valid_until = plan["payload"].get("valid_until")
            if not valid_until or datetime.fromisoformat(valid_until) <= datetime.now(
                timezone.utc
            ):
                raise OperationConflict("路线已过期，请重新校验后采用")
            parent = (
                self._plan(self._plan_row(connection, trip_id, plan["parent_plan_id"]))
                if plan["parent_plan_id"]
                else None
            )
            previous_items = parent["payload"]["itinerary"] if parent else []

            def facts(item):
                return {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "historical",
                        "state",
                        "locked",
                        "required",
                        "execution_unknown",
                        "untravelled",
                    }
                }

            historical = [
                item for item in plan["payload"]["itinerary"] if item.get("historical")
            ]
            if historical and (
                parent is None
                or parent["id"] != trip["active_plan_id"]
                or [facts(item) for item in historical]
                != [facts(item) for item in previous_items[: len(historical)]]
            ):
                raise OperationConflict("历史前缀必须原样沿用当前采用方案")
            activities = {
                item.get("candidate_id"): item
                for item in plan["payload"]["itinerary"]
                if item.get("kind") == "activity"
            }
            previous_activities = {
                item.get("candidate_id"): item
                for item in previous_items
                if item.get("kind") == "activity"
            }
            current_constraints = {
                item["candidate_id"]: item
                for item in current["request"]["planning"]["activity_constraints"]
            }
            active = (
                self._plan(self._plan_row(connection, trip_id, trip["active_plan_id"]))
                if trip["active_plan_id"]
                else None
            )
            if active and any(
                item.get("kind") == "activity"
                and item.get("state") == "completed"
                and current_constraints.get(item["candidate_id"], {}).get("state")
                != "completed"
                for item in active["payload"]["itinerary"]
            ):
                raise OperationConflict("不能移除或重置已经采用的完成事实")
            for constraint in current["request"]["planning"]["activity_constraints"]:
                if constraint["state"] != "completed" and not constraint["locked"]:
                    continue
                identifier = constraint["candidate_id"]
                item, previous_item = (
                    activities.get(identifier),
                    previous_activities.get(identifier),
                )
                if (
                    not parent
                    or parent["id"] != trip["active_plan_id"]
                    or item is None
                    or previous_item is None
                    or facts(item) != facts(previous_item)
                    or (
                        constraint["state"] == "completed"
                        and item.get("state") != "completed"
                    )
                    or (constraint["locked"] and not item.get("locked"))
                ):
                    raise OperationConflict(
                        "已完成或锁定项目必须保留当前方案的完整历史"
                    )
            for item in plan["payload"]["itinerary"]:
                if item.get("kind") == "route" and not item.get("historical"):
                    leg = item.get("leg", {})
                    if (
                        leg.get("status") != "available"
                        or not leg.get("valid_until")
                        or datetime.fromisoformat(leg["valid_until"])
                        <= datetime.now(timezone.utc)
                    ):
                        raise OperationConflict(
                            "交通段未取得有效路线或已经过期，请重新校验"
                        )
            if (
                current["request"]["planning"]["activity_constraints"]
                and any(
                    item["state"] == "completed"
                    for item in current["request"]["planning"]["activity_constraints"]
                )
                and plan["parent_plan_id"] != trip["active_plan_id"]
            ):
                raise OperationConflict("已有已完成项目，旧方案须沿用当前历史重新校验")
            previous = connection.execute(
                "SELECT * FROM plan_adoptions WHERE trip_id=? AND operation_id=?",
                (trip_id, str(body.operation_id)),
            ).fetchone()
            if previous is not None and (
                previous["plan_id"],
                previous["request_version"],
                previous["input_hash"],
            ) != (plan_id, body.expected_version, body.input_hash):
                raise OperationConflict("采用操作ID已用于不同方案")
            if previous is None:
                connection.execute(
                    "INSERT INTO plan_adoptions VALUES(?,?,?,?,?,?)",
                    (
                        trip_id,
                        str(body.operation_id),
                        plan_id,
                        body.expected_version,
                        body.input_hash,
                        now(),
                    ),
                )
                connection.execute(
                    "UPDATE trips SET active_plan_id=?,updated_at=? WHERE id=?",
                    (plan_id, now(), trip_id),
                )
            return {
                "trip": self._detail(connection, self._row(connection, trip_id)),
                "plan": plan,
                "operation_applied": True,
            }
