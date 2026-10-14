"""Private call ledger: count before sending and retain uncertain interrupted calls."""

import json
from uuid import uuid4


class ActionStore:
    @staticmethod
    def _model_usage_complete(usage):
        counts = usage.get("usage", {})
        return (
            usage.get("usage_complete") is True
            and isinstance(counts, dict)
            and all(
                type(counts.get(key)) is int and counts[key] >= 0
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
        )

    @staticmethod
    def _interrupt_actions(connection, run_id, usage):
        rows = connection.execute(
            "SELECT kind FROM run_actions WHERE run_id=? AND status='started'",
            (run_id,),
        ).fetchall()
        if rows:
            connection.execute(
                "UPDATE run_actions SET status='unknown' WHERE run_id=? AND status='started'",
                (run_id,),
            )
            usage["unknown_result_calls"] = usage.get("unknown_result_calls", 0) + len(
                rows
            )
            usage["unknown_cost_calls"] = usage.get("unknown_cost_calls", 0) + sum(
                row["kind"] == "model" for row in rows
            )
        return usage

    def begin_action(self, run_id, generation, action_key, kind, params):
        from .database import OperationConflict, encode, now

        if kind not in {"login", "search", "detail", "model"} or not isinstance(
            params, dict
        ):
            raise ValueError("无效外部调用")
        if not isinstance(action_key, str) or not action_key or len(action_key) > 500:
            raise ValueError("无效调用标识")
        if kind == "model" and (
            type(params.get("images_submitted", 0)) is not int
            or not 0 <= params.get("images_submitted", 0) <= 3
        ):
            raise ValueError("无效图片提交数量")
        if kind == "model" and (
            type(params.get("max_tokens")) is not int
            or not 1 <= params["max_tokens"] <= 65536
        ):
            raise ValueError("模型输出token预算应为1到65536的整数")
        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id)
            if row["kind"] not in {"discover", "plan"}:
                return None
            if not self._run_current(connection, row, generation):
                return None
            previous = connection.execute(
                "SELECT * FROM run_actions WHERE run_id=? AND action_key=?",
                (run_id, action_key),
            ).fetchone()
            if previous:
                raise OperationConflict("调用已记录，读取检查点后决定是否继续")
            budget = json.loads(row["budget_json"])
            usage = json.loads(row["usage_json"])
            if row["kind"] == "plan" and params.get("service") == "amap":
                if usage.get("amap_calls", 0) >= budget["max_routes"] or usage.get("unknown_result_calls", 0):
                    return None
                usage["amap_calls"] = usage.get("amap_calls", 0) + 1
            if self._elapsed_usage(row)["elapsed_seconds"] >= budget["max_seconds"]:
                return None
            # Prompt token usage is reported afterwards; overshoot stops further calls.
            if usage.get("model_tokens", 0) >= budget["max_tokens"]:
                return None
            counters = {
                "search": "searches",
                "detail": "detail_calls",
                "model": "model_calls",
            }
            limits = {"search": "max_searches", "model": "max_model_calls"}
            if kind in limits and usage.get(counters[kind], 0) >= budget[limits[kind]]:
                return None
            if usage.get("steps", 0) >= budget["max_steps"]:
                return None
            if kind == "model":
                images = params.get("images_submitted", 0)
                if usage.get("images", 0) + images > budget["max_images"]:
                    return None
                reserved = usage.get("reserved_tokens", 0)
                if (
                    usage.get("model_tokens", 0) + reserved + params["max_tokens"]
                    > budget["max_tokens"]
                ):
                    return None
                usage["images"] = usage.get("images", 0) + images
                usage["reserved_tokens"] = reserved + params["max_tokens"]
            if kind in counters:
                counter = counters[kind]
                usage[counter] = usage.get(counter, 0) + 1
            usage["external_calls"] = usage.get("external_calls", 0) + 1
            usage["steps"] = usage.get("steps", 0) + 1
            identifier, timestamp = str(uuid4()), now()
            connection.execute(
                "INSERT INTO run_actions (id,run_id,action_key,kind,status,params_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    run_id,
                    action_key,
                    kind,
                    "started",
                    encode(params),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE runs SET usage_json=? WHERE id=?", (encode(usage), run_id)
            )
            return identifier

    def finish_action(self, action_id, *, status, result=None, usage=None):
        from .database import encode, now

        if status not in {"completed", "failed", "unknown"}:
            raise ValueError("无效调用状态")
        if usage is not None and not isinstance(usage, dict):
            raise ValueError("调用用量必须为对象")
        usage = usage or {}
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM run_actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                return False
            previous_usage = json.loads(row["usage_json"])
            previously_complete = self._model_usage_complete(previous_usage)
            if row["status"] not in {"started", "unknown"} and (
                row["status"] != status or row["kind"] != "model" or previously_complete
            ):
                return row["status"] == status
            previous_counts = previous_usage.get("usage", {})
            if not isinstance(previous_counts, dict):
                previous_counts = {}
            incoming_counts = usage.get("usage", {})
            if not isinstance(incoming_counts, dict):
                incoming_counts = {}
            counts = {
                key: value
                for key, value in previous_counts.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(value) is int
                and value >= 0
            }
            counts.update(
                {
                    key: value
                    for key, value in incoming_counts.items()
                    if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and type(value) is int
                    and value >= 0
                }
            )
            updated_usage = {**previous_usage, **usage, "usage": counts}
            if previously_complete:
                counts = previous_counts
                updated_usage.update(usage=counts, usage_complete=True)
            complete = self._model_usage_complete(updated_usage)
            if row["kind"] == "model":
                updated_usage["usage_complete"] = complete
            connection.execute(
                "UPDATE run_actions SET status=?,result_json=?,usage_json=?,updated_at=? WHERE id=?",
                (
                    status,
                    encode(result) if result is not None else row["result_json"],
                    encode(updated_usage),
                    now(),
                    action_id,
                ),
            )
            run = self._run_row(connection, row["run_id"])
            total = json.loads(run["usage_json"])
            if updated_usage.get("amap_kind") in {"route", "city"} and not previous_usage.get("amap_kind"):
                counter = "route_calls" if updated_usage["amap_kind"] == "route" else "city_calls"
                total[counter] = total.get(counter, 0) + 1
            total["unknown_result_calls"] = max(
                0,
                total.get("unknown_result_calls", 0)
                + int(status == "unknown")
                - int(row["status"] == "unknown"),
            )
            if row["kind"] == "model":
                for key, counter in (
                    ("total_tokens", "model_tokens"),
                    ("prompt_tokens", "prompt_tokens"),
                    ("completion_tokens", "completion_tokens"),
                ):
                    value = counts.get(key)
                    if type(value) is int and value >= 0:
                        old_value = previous_counts.get(key, 0)
                        if type(old_value) is not int or old_value < 0:
                            old_value = 0
                        total[counter] = total.get(counter, 0) + value - old_value
                if complete and not previously_complete:
                    reservation = json.loads(row["params_json"]).get("max_tokens", 0)
                    total["reserved_tokens"] = max(
                        0, total.get("reserved_tokens", 0) - reservation
                    )
                was_unknown = row["status"] != "started" and not previously_complete
                total["unknown_cost_calls"] = max(
                    0,
                    total.get("unknown_cost_calls", 0)
                    + int(not complete)
                    - int(was_unknown),
                )
            connection.execute(
                "UPDATE runs SET usage_json=? WHERE id=?",
                (encode(total), row["run_id"]),
            )
            return True

    def list_actions(self, run_id):
        with self.connection() as connection:
            self._run_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM run_actions WHERE run_id=? ORDER BY created_at,id",
                (run_id,),
            )
            return [
                {
                    **dict(row),
                    "params": json.loads(row["params_json"]),
                    "result": json.loads(row["result_json"])
                    if row["result_json"]
                    else None,
                    "usage": json.loads(row["usage_json"]),
                }
                for row in rows
            ]

    def save_run_item(self, run_id, generation, key, payload):
        from .database import encode, now

        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id)
            if not self._run_current(connection, row, generation):
                return False
            connection.execute(
                "INSERT INTO run_items (run_id,item_key,payload_json,updated_at) VALUES (?,?,?,?) ON CONFLICT(run_id,item_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                (run_id, key, encode(payload), now()),
            )
            return True

    def get_run_item(self, run_id, key, default=None):
        with self.connection() as connection:
            self._run_row(connection, run_id)
            row = connection.execute(
                "SELECT payload_json FROM run_items WHERE run_id=? AND item_key=?",
                (run_id, key),
            ).fetchone()
            return json.loads(row[0]) if row else default

    def save_source_handle(self, run_id, generation, external_id, handle):
        from .database import encode

        with self.connection(write=True) as connection:
            row = self._run_row(connection, run_id)
            if not self._run_current(connection, row, generation):
                return False
            connection.execute(
                "INSERT INTO source_handles (run_id,external_id,handle_json) VALUES (?,?,?) ON CONFLICT(run_id,external_id) DO UPDATE SET handle_json=excluded.handle_json",
                (run_id, external_id, encode(handle)),
            )
            return True

    def get_source_handle(self, run_id, external_id):
        with self.connection() as connection:
            self._run_row(connection, run_id)
            row = connection.execute(
                "SELECT handle_json FROM source_handles WHERE run_id=? AND external_id=?",
                (run_id, external_id),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def trip_usage(self, trip_id):
        with self.connection() as connection:
            self._row(connection, trip_id)
            total = {
                "external_calls": 0,
                "model_tokens": 0,
                "images": 0,
                "unknown_cost_calls": 0,
            }
            for row in connection.execute(
                "SELECT usage_json FROM runs WHERE trip_id=?", (trip_id,)
            ):
                usage = json.loads(row[0])
                for key in total:
                    total[key] += usage.get(key, 0)
            return {**total, "cost_cny": None}
