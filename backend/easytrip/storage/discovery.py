"""Transactional P2 storage with immutable per-run source snapshots."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from ..domain.discovery import (
    Activity,
    CandidateDraft,
    Evidence,
    Gap,
    ImageObservation,
    PlaceMention,
    Source,
    SubmittedImage,
)


def _encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate(model, value):
    return model.model_validate(
        value.model_dump() if hasattr(value, "model_dump") else value
    )


class DiscoveryStore:
    def _discovery_run(self, connection, trip_id, run_id, generation):
        # A deleted run is also a normal late-worker rejection.
        row = connection.execute(
            "SELECT * FROM runs WHERE id=? AND trip_id=?", (run_id, trip_id)
        ).fetchone()
        if row is None or not self._run_current(connection, row, generation):
            return None
        return row

    @staticmethod
    def _source_record(
        row,
        snapshot,
        *,
        run_id=None,
        request_version=None,
        read_at=None,
        include_private=False,
    ):
        result = Source.model_validate_json(snapshot).model_dump(mode="json")
        if not include_private:
            result.pop("author_key", None)
        result.update(
            {
                key: row[key]
                for key in (
                    "id",
                    "trip_id",
                    "dedupe_confidence",
                    "first_read_at",
                    "last_read_at",
                )
            }
        )
        if run_id is not None:
            result.update(
                run_id=run_id, request_version=request_version, read_at=read_at
            )
        return result

    def _source(
        self, connection, trip_id, source_id, run_id=None, *, include_private=False
    ):
        from .database import NotFound

        row = connection.execute(
            "SELECT * FROM sources WHERE id=? AND trip_id=?", (source_id, trip_id)
        ).fetchone()
        if row is None:
            raise NotFound()
        if run_id is None:
            return self._source_record(
                row, row["latest_snapshot_json"], include_private=include_private
            )
        snapshot = connection.execute(
            "SELECT * FROM source_snapshots WHERE run_id=? AND source_id=? AND trip_id=?",
            (run_id, source_id, trip_id),
        ).fetchone()
        if snapshot is None:
            raise NotFound()
        return self._source_record(
            row,
            snapshot["snapshot_json"],
            run_id=run_id,
            request_version=snapshot["request_version"],
            read_at=snapshot["read_at"],
            include_private=include_private,
        )

    def save_source(
        self, trip_id, run_id, generation, source, *, query="", purpose="main"
    ):
        from .database import OperationConflict

        source = _validate(Source, source)
        if (
            not isinstance(query, str)
            or len(query) > 4000
            or purpose not in {"discovery", "main", "meal_gap", "lodging_gap"}
        ):
            raise ValueError("来源查询或用途无效")
        payload = source.model_dump(mode="json")
        encoded = _encode(payload)
        confidence = "high" if source.external_id is not None else "low"
        dedupe_key = (
            "external:" + source.external_id
            if source.external_id is not None
            else "local:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        )
        with self.connection(write=True) as connection:
            run = self._discovery_run(connection, trip_id, run_id, generation)
            if run is None:
                return None
            if source.kind == "comment":
                parent = self._source(
                    connection,
                    trip_id,
                    source.parent_source_id,
                    run_id,
                    include_private=True,
                )
                if parent["kind"] != "note" or parent["platform"] != source.platform:
                    raise ValueError("评论父来源必须是本轮同平台笔记")
                if source.reply_to_source_id is not None:
                    replied = self._source(
                        connection,
                        trip_id,
                        source.reply_to_source_id,
                        run_id,
                        include_private=True,
                    )
                    if (
                        replied["kind"] != "comment"
                        or replied["parent_source_id"] != source.parent_source_id
                        or replied["platform"] != source.platform
                    ):
                        raise ValueError("回复必须关联本轮同笔记评论")
            row = connection.execute(
                "SELECT * FROM sources WHERE trip_id=? AND platform=? AND kind=? AND dedupe_key=?",
                (trip_id, source.platform, source.kind, dedupe_key),
            ).fetchone()
            timestamp = datetime.now(timezone.utc).isoformat()
            source_id = row["id"] if row is not None else str(uuid4())
            if row is not None and row["parent_source_id"] != source.parent_source_id:
                raise OperationConflict("来源身份已关联其它父笔记")
            snapshot = connection.execute(
                "SELECT * FROM source_snapshots WHERE run_id=? AND source_id=?",
                (run_id, source_id),
            ).fetchone()
            if snapshot is not None and snapshot["snapshot_json"] != encoded:
                raise OperationConflict("同轮来源快照不可覆盖")
            if row is None:
                connection.execute(
                    "INSERT INTO sources(id,trip_id,platform,kind,external_id,dedupe_key,dedupe_confidence,parent_source_id,latest_snapshot_json,first_read_at,last_read_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        source_id,
                        trip_id,
                        source.platform,
                        source.kind,
                        source.external_id,
                        dedupe_key,
                        confidence,
                        source.parent_source_id,
                        encoded,
                        timestamp,
                        timestamp,
                    ),
                )
            elif snapshot is None:
                connection.execute(
                    "UPDATE sources SET latest_snapshot_json=?,last_read_at=? WHERE id=?",
                    (encoded, timestamp, source_id),
                )
            if snapshot is None:
                connection.execute(
                    "INSERT INTO source_snapshots(run_id,source_id,trip_id,request_version,snapshot_json,read_at) VALUES(?,?,?,?,?,?)",
                    (
                        run_id,
                        source_id,
                        trip_id,
                        run["request_version"],
                        encoded,
                        timestamp,
                    ),
                )
            connection.execute(
                "INSERT OR IGNORE INTO run_sources(run_id,source_id,query,purpose) VALUES(?,?,?,?)",
                (run_id, source_id, query, purpose),
            )
            return self._source(
                connection, trip_id, source_id, run_id, include_private=True
            )

    @staticmethod
    def _entity(row, model):
        result = model.model_validate_json(row["payload_json"]).model_dump(mode="json")
        result.update(
            {key: row[key] for key in ("id", "run_id", "trip_id", "request_version")}
        )
        return result

    @staticmethod
    def _insert_entity(connection, table, run, item, extra):
        from .database import OperationConflict

        encoded = _encode(item.model_dump(mode="json"))
        existing = connection.execute(
            f"SELECT * FROM {table} WHERE id=? AND run_id=?", (item.id, run["id"])
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != encoded:
                raise OperationConflict("已保存抽取结果不可覆盖")
            return
        if (
            table in {"candidates", "gaps"}
            and connection.execute(
                f"SELECT 1 FROM {table} WHERE id=?", (item.id,)
            ).fetchone()
        ):
            raise OperationConflict("抽取标识已用于其它轮次")
        fields = ["id", "run_id", "trip_id", "request_version", "payload_json", *extra]
        values = [
            item.id,
            run["id"],
            run["trip_id"],
            run["request_version"],
            encoded,
            *extra.values(),
        ]
        connection.execute(
            f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )

    def save_discovery_batch(
        self,
        trip_id,
        run_id,
        generation,
        *,
        mentions=None,
        activities=None,
        evidence=None,
        candidates=None,
        gaps=None,
        image_observations=None,
        submitted_images=None,
    ):
        from .database import OperationConflict

        observations = [
            _validate(ImageObservation, item) for item in image_observations or []
        ]
        submitted = {
            (item.source_id, item.image_index)
            for item in (
                _validate(SubmittedImage, item) for item in submitted_images or []
            )
        }
        groups = [
            (
                "place_mentions",
                [_validate(PlaceMention, item) for item in mentions or []],
            ),
            ("activities", [_validate(Activity, item) for item in activities or []]),
            ("evidence", [_validate(Evidence, item) for item in evidence or []]),
            (
                "candidates",
                [_validate(CandidateDraft, item) for item in candidates or []],
            ),
            ("gaps", [_validate(Gap, item) for item in gaps or []]),
        ]
        with self.connection(write=True) as connection:
            run = self._discovery_run(connection, trip_id, run_id, generation)
            if run is None:
                return None
            sources = {
                row["source_id"]: json.loads(row["snapshot_json"])
                for row in connection.execute(
                    "SELECT source_id,snapshot_json FROM source_snapshots WHERE run_id=? AND trip_id=?",
                    (run_id, trip_id),
                )
            }
            for source_id, image_index in submitted:
                source = sources.get(source_id)
                if (
                    source is None
                    or source["kind"] != "note"
                    or source["read_status"] != "success"
                    or image_index > len(source["images"])
                    or not source["images"][image_index - 1]
                ):
                    raise ValueError("实际提交图片不属于本轮可读取笔记")
            for item in observations:
                if (item.source_id, item.image_index) not in submitted:
                    raise ValueError("图片观察未对应实际提交图片")
                encoded = _encode(item.model_dump(mode="json"))
                existing = connection.execute(
                    "SELECT payload_json FROM image_observations WHERE run_id=? AND source_id=? AND image_index=?",
                    (run_id, item.source_id, item.image_index),
                ).fetchone()
                if existing is not None and existing["payload_json"] != encoded:
                    raise OperationConflict("同轮图片观察不可覆盖")
                connection.execute(
                    "INSERT OR IGNORE INTO image_observations(run_id,source_id,image_index,payload_json) VALUES(?,?,?,?)",
                    (run_id, item.source_id, item.image_index, encoded),
                )
            observed = {
                (row["source_id"], row["image_index"]): json.loads(row["payload_json"])
                for row in connection.execute(
                    "SELECT * FROM image_observations WHERE run_id=?", (run_id,)
                )
            }

            def quote(item):
                source = sources.get(item.source_id)
                if source is None or source["read_status"] != "success":
                    raise ValueError("证据来源不属于本轮可读取来源")
                if item.citation_kind == "image":
                    key = (item.source_id, item.image_index)
                    observation = observed.get(key)
                    if (
                        key not in submitted
                        or observation is None
                        or not item.quote.strip()
                        or item.quote not in observation["text"]
                    ):
                        raise ValueError("图片引用必须来自实际提交图片的待核实观察")
                elif (
                    not item.quote.strip()
                    or item.quote not in source["title"] + "\n" + source["text"]
                ):
                    raise ValueError("证据引用不属于来源原文")

            def reference(table, identifier):
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE run_id=? AND id=?",
                    (run_id, identifier),
                ).fetchone()
                if row is None:
                    raise ValueError("抽取引用不属于本轮")
                return row

            result = {
                "image_observations": [
                    item.model_dump(mode="json") for item in observations
                ]
            }
            for table, items in groups:
                for item in items:
                    extra = {}
                    if table in {"place_mentions", "activities", "evidence"}:
                        quote(item)
                        extra["source_id"] = item.source_id
                    if table in {"activities", "evidence"}:
                        if item.mention_id is not None:
                            reference("place_mentions", item.mention_id)
                        extra["mention_id"] = item.mention_id
                    if table == "candidates":
                        if not item.mention_ids:
                            raise ValueError("候选必须有本轮来源提及")
                        candidate_mentions = [
                            reference("place_mentions", identifier)
                            for identifier in item.mention_ids
                        ]
                        has_image = any(
                            json.loads(mention["payload_json"])["citation_kind"]
                            == "image"
                            for mention in candidate_mentions
                        )
                        for identifier in item.evidence_ids:
                            linked = reference("evidence", identifier)
                            has_image |= (
                                json.loads(linked["payload_json"])["citation_kind"]
                                == "image"
                            )
                            if (
                                linked["mention_id"] is not None
                                and linked["mention_id"] not in item.mention_ids
                            ):
                                raise ValueError("候选证据关联了其它地点提及")
                        if has_image and "图片待核验" not in item.uncertainty:
                            raise ValueError("图片候选必须明确标记图片待核验")
                        extra["category"] = item.category
                    if table == "gaps":
                        for identifier in item.source_ids:
                            if identifier not in sources:
                                raise ValueError("缺口来源不属于本轮")
                        for identifier in item.candidate_ids:
                            reference("candidates", identifier)
                        extra.update(kind=item.kind, status=item.status)
                    self._insert_entity(connection, table, run, item, extra)
                    if table == "candidates":
                        for identifier in item.evidence_ids:
                            connection.execute(
                                "INSERT OR IGNORE INTO candidate_evidence(candidate_id,run_id,evidence_id) VALUES(?,?,?)",
                                (item.id, run_id, identifier),
                            )
                key = "mentions" if table == "place_mentions" else table
                result[key] = [
                    dict(
                        item.model_dump(mode="json"),
                        run_id=run_id,
                        trip_id=trip_id,
                        request_version=run["request_version"],
                    )
                    for item in items
                ]
            return result

    def get_source(self, trip_id, source_id, run_id=None, *, include_private=False):
        with self.connection() as connection:
            self._row(connection, trip_id)
            return self._source(
                connection, trip_id, source_id, run_id, include_private=include_private
            )

    def list_run_sources(self, trip_id, run_id, *, include_private=False):
        with self.connection() as connection:
            self._run_row(connection, run_id, trip_id)
            result = []
            for row in connection.execute(
                "SELECT source_id FROM source_snapshots WHERE run_id=? AND trip_id=? ORDER BY rowid",
                (run_id, trip_id),
            ):
                item = self._source(
                    connection,
                    trip_id,
                    row["source_id"],
                    run_id,
                    include_private=include_private,
                )
                item["queries"] = [
                    dict(link)
                    for link in connection.execute(
                        "SELECT query,purpose FROM run_sources WHERE run_id=? AND source_id=? ORDER BY rowid",
                        (run_id, row["source_id"]),
                    )
                ]
                result.append(item)
            return result

    def _selected_discovery_run(self, connection, trip_id, run_id):
        trip = self._row(connection, trip_id)
        if run_id is not None:
            self._run_row(connection, run_id, trip_id)
            return run_id
        row = connection.execute(
            "SELECT r.id FROM runs r WHERE r.trip_id=? AND r.request_version=? AND (EXISTS(SELECT 1 FROM candidates c WHERE c.run_id=r.id) OR EXISTS(SELECT 1 FROM gaps g WHERE g.run_id=r.id)) ORDER BY r.rowid DESC LIMIT 1",
            (trip_id, trip["request_version"]),
        ).fetchone()
        return row["id"] if row is not None else None

    def list_candidates(
        self, trip_id, run_id=None, category=None, *, limit=50, offset=0
    ):
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
            or category not in {None, "eat", "play", "stay"}
        ):
            raise ValueError("候选分页或类别无效")
        with self.connection() as connection:
            selected = self._selected_discovery_run(connection, trip_id, run_id)
            filters, values = "trip_id=? AND run_id=?", [trip_id, selected]
            if category is not None:
                filters += " AND category=?"
                values.append(category)
            total = connection.execute(
                "SELECT count(*) FROM candidates WHERE " + filters, values
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM candidates WHERE "
                + filters
                + " ORDER BY rowid LIMIT ? OFFSET ?",
                [*values, limit, offset],
            )
            return {
                "items": [self._entity(row, CandidateDraft) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def candidate_sources(self, trip_id, candidate_id):
        from .database import NotFound

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id=? AND trip_id=?",
                (candidate_id, trip_id),
            ).fetchone()
            if row is None:
                raise NotFound()
            candidate = CandidateDraft.model_validate_json(row["payload_json"])
            result, seen = [], set()
            for mention_id in candidate.mention_ids:
                mention_row = connection.execute(
                    "SELECT * FROM place_mentions WHERE run_id=? AND trip_id=? AND id=?",
                    (row["run_id"], trip_id, mention_id),
                ).fetchone()
                if mention_row is None:
                    raise sqlite3.DatabaseError("候选提及引用不属于已保存轮次")
                mention = PlaceMention.model_validate_json(mention_row["payload_json"])
                if mention.source_id not in seen:
                    seen.add(mention.source_id)
                    result.append(
                        self._source(
                            connection, trip_id, mention.source_id, row["run_id"]
                        )
                    )
            return result

    def candidate_evidence(self, trip_id, candidate_id):
        from .database import NotFound

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id=? AND trip_id=?",
                (candidate_id, trip_id),
            ).fetchone()
            if row is None:
                raise NotFound()
            result = []
            for evidence in connection.execute(
                "SELECT e.* FROM candidate_evidence ce JOIN evidence e ON e.id=ce.evidence_id AND e.run_id=ce.run_id WHERE ce.candidate_id=? ORDER BY ce.rowid",
                (candidate_id,),
            ):
                item = self._entity(evidence, Evidence)
                item["source"] = self._source(
                    connection, trip_id, evidence["source_id"], row["run_id"]
                )
                if item["citation_kind"] == "image":
                    observation = connection.execute(
                        "SELECT payload_json FROM image_observations WHERE run_id=? AND source_id=? AND image_index=?",
                        (row["run_id"], evidence["source_id"], item["image_index"]),
                    ).fetchone()
                    item["image_observation"] = (
                        ImageObservation.model_validate_json(
                            observation["payload_json"]
                        ).model_dump(mode="json")
                        if observation
                        else None
                    )
                result.append(item)
            return result

    def list_gaps(self, trip_id, run_id=None):
        with self.connection() as connection:
            selected = self._selected_discovery_run(connection, trip_id, run_id)
            return [
                self._entity(row, Gap)
                for row in connection.execute(
                    "SELECT * FROM gaps WHERE trip_id=? AND run_id=? ORDER BY rowid",
                    (trip_id, selected),
                )
            ]
