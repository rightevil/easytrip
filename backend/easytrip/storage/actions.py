"""Private call ledger: count before sending and retain uncertain interrupted calls."""

import json
import math
import re
from datetime import datetime
from uuid import uuid4


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _seconds(value):
    return (
        value
        if type(value) in {int, float} and math.isfinite(value) and value >= 0
        else None
    )


def _identifier(value):
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}", value)
        else None
    )


def _metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {
        key: item
        for key in ("api_version", "server_version")
        if (item := _identifier(value.get(key))) is not None
    }
    fingerprint = value.get("schema_fingerprint")
    if isinstance(fingerprint, str) and re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
        result["schema_fingerprint"] = fingerprint
    return result


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return value if datetime.fromisoformat(value).tzinfo is not None else None
    except ValueError:
        return None


def _totals():
    return dict.fromkeys(
        (
            "external_calls",
            "model_tokens",
            "prompt_tokens",
            "completion_tokens",
            "images",
            "unknown_cost_calls",
            "cache_hits",
            "failed_calls",
            "unknown_result_calls",
            "started_calls",
        ),
        0,
    ) | {"elapsed_seconds": 0.0, "cost_cny": None}


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
                if usage.get("amap_calls", 0) >= budget["max_routes"] or usage.get(
                    "unknown_result_calls", 0
                ):
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
            if updated_usage.get("amap_kind") in {
                "route",
                "city",
                "poi",
            } and not previous_usage.get("amap_kind"):
                counter = {
                    "route": "route_calls",
                    "city": "city_calls",
                    "poi": "poi_calls",
                }[updated_usage["amap_kind"]]
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

    def begin_api_action(
        self, trip_id, expected_version, provider, operation, *, cached=False
    ):
        from .database import now

        operations = {
            "amap": {"poi_search", "poi_detail"},
            "rail": {"get-station-code-by-names", "get-tickets"},
        }
        if operation not in operations.get(provider, set()) or type(cached) is not bool:
            raise ValueError("无效外部调用")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("无效行程版本")
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            self._version(trip, expected_version)
            identifier, timestamp = str(uuid4()), now()
            connection.execute(
                "INSERT INTO api_actions(id,trip_id,request_version,provider,operation,cached,status,created_at,updated_at) VALUES(?,?,?,?,?,?,'started',?,?)",
                (
                    identifier,
                    trip_id,
                    expected_version,
                    provider,
                    operation,
                    int(cached),
                    timestamp,
                    timestamp,
                ),
            )
            return identifier

    def finish_api_action(self, action_id, *, status, elapsed_seconds, metadata=None):
        from .database import encode, now

        if (
            status not in {"completed", "failed", "unknown"}
            or _seconds(elapsed_seconds) is None
        ):
            raise ValueError("无效调用结果或耗时")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("调用元数据必须为对象")
        with self.connection(write=True) as connection:
            row = connection.execute(
                "SELECT status FROM api_actions WHERE id=?", (action_id,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] != "started":
                return row["status"] == status
            connection.execute(
                "UPDATE api_actions SET status=?,elapsed_seconds=?,metadata_json=?,updated_at=? WHERE id=?",
                (
                    status,
                    elapsed_seconds,
                    encode(_metadata(metadata)),
                    now(),
                    action_id,
                ),
            )
            return True

    def recover_api_actions(self):
        from .database import now

        with self.connection(write=True) as connection:
            return connection.execute(
                "UPDATE api_actions SET status='unknown',updated_at=? WHERE status='started'",
                (now(),),
            ).rowcount

    def _trip_usage(self, connection, trip_id, runs):
        total = _totals()
        for row in runs:
            usage = self._elapsed_usage(row)
            for key in total:
                if key not in {
                    "cost_cny",
                    "elapsed_seconds",
                    "failed_calls",
                    "started_calls",
                }:
                    total[key] += _count(usage.get(key))
            total["elapsed_seconds"] += _seconds(usage.get("elapsed_seconds")) or 0
        states = connection.execute(
            "SELECT a.status,count(*) AS n FROM run_actions a JOIN runs r ON r.id=a.run_id WHERE r.trip_id=? GROUP BY a.status",
            (trip_id,),
        )
        for row in states:
            if row["status"] in {"failed", "started"}:
                total[
                    {"failed": "failed_calls", "started": "started_calls"}[
                        row["status"]
                    ]
                ] += row["n"]
        api = connection.execute(
            "SELECT coalesce(sum(1-cached),0) AS external_calls,coalesce(sum(cached),0) AS cache_hits,coalesce(sum(status='failed'),0) AS failed_calls,coalesce(sum(status='unknown'),0) AS unknown_result_calls,coalesce(sum(status='started'),0) AS started_calls,coalesce(sum(elapsed_seconds),0) AS elapsed_seconds FROM api_actions WHERE trip_id=?",
            (trip_id,),
        ).fetchone()
        for key in api.keys():
            total[key] += api[key]
        return total

    def trip_usage(self, trip_id):
        with self.connection() as connection:
            connection.execute("BEGIN")
            self._row(connection, trip_id)
            runs = connection.execute(
                "SELECT status,stage_started_at,updated_at,usage_json FROM runs WHERE trip_id=?",
                (trip_id,),
            ).fetchall()
            return self._trip_usage(connection, trip_id, runs)

    def usage_report(self, trip_id):
        with self.connection() as connection:
            connection.execute("BEGIN")
            self._row(connection, trip_id)
            rows = connection.execute(
                "SELECT * FROM runs WHERE trip_id=? ORDER BY created_at,id", (trip_id,)
            ).fetchall()
            total = self._trip_usage(connection, trip_id, rows)
            actions, runs = [], []
            for row in rows:
                usage = self._elapsed_usage(row)
                first = connection.execute(
                    "SELECT payload_json FROM run_items WHERE run_id=? AND item_key='first_candidates_at'",
                    (row["id"],),
                ).fetchone()
                report = {
                    key: row[key]
                    for key in (
                        "id",
                        "kind",
                        "status",
                        "request_version",
                        "created_at",
                        "updated_at",
                    )
                }
                report.update(
                    {
                        key: (_seconds(usage.get(key)) or 0)
                        if key == "elapsed_seconds"
                        else _count(usage.get(key))
                        for key in _totals()
                        if key not in {"failed_calls", "started_calls", "cost_cny"}
                    }
                )
                report.update(
                    cost_cny=None,
                    first_candidates_at=_timestamp(json.loads(first[0]))
                    if first
                    else None,
                    interrupted_stage_time_unknown=usage.get(
                        "interrupted_stage_time_unknown"
                    )
                    is True,
                )
                capability = connection.execute(
                    "SELECT payload_json FROM run_items WHERE run_id=? AND item_key='source:capability'",
                    (row["id"],),
                ).fetchone()
                capability = json.loads(capability[0]) if capability else {}
                if not isinstance(capability, dict):
                    capability = {}
                server = capability.get("server_info", {})
                report["source_metadata"] = _metadata(
                    {
                        "api_version": capability.get("protocol_version"),
                        "server_version": server.get("version")
                        if isinstance(server, dict)
                        else None,
                        "schema_fingerprint": capability.get("schema_fingerprint"),
                    }
                )
                run_actions = connection.execute(
                    "SELECT * FROM run_actions WHERE run_id=? ORDER BY created_at,id",
                    (row["id"],),
                ).fetchall()
                report.update(
                    failed_calls=sum(
                        action["status"] == "failed" for action in run_actions
                    ),
                    started_calls=sum(
                        action["status"] == "started" for action in run_actions
                    ),
                )
                for action in run_actions:
                    params, call_usage = (
                        json.loads(action["params_json"]),
                        json.loads(action["usage_json"]),
                    )
                    counts = call_usage.get("usage", {})
                    if not isinstance(counts, dict):
                        counts = {}
                    provider = (
                        "model"
                        if action["kind"] == "model"
                        else "amap"
                        if params.get("service") == "amap"
                        else "xhs"
                    )
                    operation = action["kind"]
                    if provider == "model":
                        operation = (
                            params.get("task_type")
                            if params.get("task_type")
                            in {
                                "understanding",
                                "evidence_text",
                                "evidence_multimodal",
                                "text_extraction",
                                "multimodal_extraction",
                                "output_repair",
                            }
                            else "model"
                        )
                    elif provider == "xhs":
                        operation = (
                            params.get("tool")
                            if params.get("tool")
                            in {"check_login_status", "search_feeds", "get_feed_detail"}
                            else action["kind"]
                        )
                    elif call_usage.get("amap_kind") in {"route", "city", "poi"}:
                        operation = call_usage["amap_kind"]
                    actions.append(
                        {
                            **{
                                key: action[key]
                                for key in (
                                    "id",
                                    "run_id",
                                    "status",
                                    "created_at",
                                    "updated_at",
                                )
                            },
                            "provider": provider,
                            "operation": operation,
                            "cached": False,
                            "elapsed_seconds": _seconds(
                                call_usage.get("elapsed_seconds")
                            ),
                            "model_id": _identifier(params.get("model_id"))
                            if provider == "model"
                            else None,
                            "usage_complete": self._model_usage_complete(call_usage)
                            if provider == "model"
                            else None,
                            **{
                                key: counts.get(key)
                                if type(counts.get(key)) is int and counts[key] >= 0
                                else None
                                for key in (
                                    "prompt_tokens",
                                    "completion_tokens",
                                    "total_tokens",
                                )
                            },
                            "images_submitted": _count(params.get("images_submitted"))
                            if provider == "model"
                            else 0,
                            "metadata": _metadata(call_usage),
                        }
                    )
                runs.append(report)
            api = _totals()
            for row in connection.execute(
                "SELECT * FROM api_actions WHERE trip_id=? ORDER BY created_at,id",
                (trip_id,),
            ):
                actions.append(
                    {
                        **{
                            key: row[key]
                            for key in (
                                "id",
                                "status",
                                "created_at",
                                "updated_at",
                                "provider",
                                "operation",
                                "elapsed_seconds",
                            )
                        },
                        "run_id": None,
                        "cached": bool(row["cached"]),
                        "model_id": None,
                        "usage_complete": None,
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                        "images_submitted": 0,
                        "metadata": _metadata(json.loads(row["metadata_json"])),
                    }
                )
                api["external_calls"] += 1 - row["cached"]
                api["cache_hits"] += row["cached"]
                api["elapsed_seconds"] += row["elapsed_seconds"] or 0
                for status, key in (
                    ("failed", "failed_calls"),
                    ("unknown", "unknown_result_calls"),
                    ("started", "started_calls"),
                ):
                    api[key] += row["status"] == status
            providers = {
                key: {
                    **_totals(),
                    "known_action_elapsed_seconds": 0.0,
                    "elapsed_unknown_calls": 0,
                }
                for key in ("model", "xhs", "amap", "rail", "unknown")
            }
            for action in actions:
                provider = providers[action["provider"]]
                provider["external_calls"] += not action["cached"]
                provider["cache_hits"] += action["cached"]
                provider["images"] += action["images_submitted"]
                for token, key in (
                    ("total_tokens", "model_tokens"),
                    ("prompt_tokens", "prompt_tokens"),
                    ("completion_tokens", "completion_tokens"),
                ):
                    provider[key] += action[token] or 0
                provider["unknown_cost_calls"] += (
                    action["provider"] == "model"
                    and action["usage_complete"] is False
                    and action["status"] != "started"
                )
                for status, key in (
                    ("failed", "failed_calls"),
                    ("unknown", "unknown_result_calls"),
                    ("started", "started_calls"),
                ):
                    provider[key] += action["status"] == status
                if action["elapsed_seconds"] is None:
                    provider["elapsed_unknown_calls"] += 1
                else:
                    provider["known_action_elapsed_seconds"] += action[
                        "elapsed_seconds"
                    ]
            for run in runs:
                providers["amap" if run["kind"] == "plan" else "unknown"][
                    "cache_hits"
                ] += _count(run.get("cache_hits"))
            for key in _totals():
                if key not in {"cost_cny", "elapsed_seconds"}:
                    classified = sum(item[key] for item in providers.values())
                    providers["unknown"][key] += max(0, total[key] - classified)
            for provider in providers.values():
                # Task elapsed time cannot be attributed to an individual provider.
                provider["elapsed_seconds"] = None
            return {
                **total,
                "scope": "recorded_application_calls",
                "account_total_known": False,
                "pricing_status": "unverified",
                "runs": runs,
                "api": api,
                "providers": providers,
                "actions": sorted(
                    actions, key=lambda action: (action["created_at"], action["id"])
                ),
            }
