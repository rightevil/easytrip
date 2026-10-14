"""POI overlays, bounded search cache, and map state under the existing CAS rules."""

import json
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..domain.discovery import Activity, CandidateDraft, Evidence, PlaceMention
from ..domain.models import ConfirmedOrigin, TripRequest, Workspace
from ..domain.places import Poi, PoiMatch

API_VERSION = "v5"
CACHE_TTL = timedelta(hours=24)


def _normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


class PlaceStore:
    def _place_scope(self, connection, trip_id, expected_version, run_id=None):
        from .database import OperationConflict

        trip = self._row(connection, trip_id)
        self._editable(trip)
        self._version(trip, expected_version)
        selected = self._selected_discovery_run(connection, trip_id, run_id)
        if selected is not None:
            run = self._run_row(connection, selected, trip_id)
            if run["request_version"] != expected_version:
                raise OperationConflict("历史候选不能覆盖当前地图")
        return trip, selected

    def _candidate_for_places(self, connection, trip_id, candidate_id):
        from .database import NotFound

        row = connection.execute(
            "SELECT * FROM candidates WHERE id=? AND trip_id=?", (candidate_id, trip_id)
        ).fetchone()
        if row is None:
            raise NotFound()
        candidate = self._entity(row, CandidateDraft)
        mentions = []
        for identifier in candidate["mention_ids"]:
            mention = connection.execute(
                "SELECT * FROM place_mentions WHERE run_id=? AND id=? AND trip_id=?",
                (row["run_id"], identifier, trip_id),
            ).fetchone()
            if mention is None:
                from sqlite3 import DatabaseError

                raise DatabaseError("候选提及引用缺失")
            mentions.append(self._entity(mention, PlaceMention))
        return candidate, mentions

    def begin_place_match(self, trip_id, body):
        from .database import OperationConflict

        with self.connection(write=True) as connection:
            trip, run_id = self._place_scope(
                connection,
                trip_id,
                body.expected_version,
                str(body.run_id) if body.run_id else None,
            )
            self._version(trip, body.expected_workspace_version, "workspace_version")
            rows = connection.execute(
                "SELECT id FROM candidates WHERE trip_id=? AND run_id=? ORDER BY rowid",
                (trip_id, run_id),
            ).fetchall()
            identifiers = [row["id"] for row in rows]
            if body.candidate_ids is not None:
                if not set(body.candidate_ids).issubset(identifiers):
                    raise OperationConflict("只能匹配当前轮次已保存候选")
                identifiers = list(dict.fromkeys(body.candidate_ids))
            total = len(identifiers)
            identifiers = identifiers[:20]
            token = str(uuid4())
            if run_id:
                connection.execute(
                    "INSERT INTO place_match_sessions(run_id,trip_id,request_version,token) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET token=excluded.token",
                    (run_id, trip_id, body.expected_version, token),
                )
            return {
                "token": token,
                "run_id": run_id,
                "total": total,
                "request": TripRequest.model_validate_json(
                    trip["request_json"]
                ).model_dump(mode="json"),
                "items": [
                    {"candidate": candidate, "mentions": mentions}
                    for candidate, mentions in (
                        self._candidate_for_places(connection, trip_id, identifier)
                        for identifier in identifiers
                    )
                ],
            }

    def cached_poi_search(self, query, city, api_version=API_VERSION):
        from .database import now

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM poi_search_cache WHERE query=? AND city=? AND api_version=? AND expires_at>?",
                (_normalized(query), _normalized(city), api_version, now()),
            ).fetchone()
            if row is None:
                return None
            result = json.loads(row["payload_json"])
            result["pois"] = [
                Poi.model_validate(poi).model_dump(mode="json")
                for poi in result["pois"]
            ]
            return result

    def save_poi_search(self, query, city, response, read_at, api_version=API_VERSION):
        from .database import encode

        timestamp = datetime.fromisoformat(read_at)
        if timestamp.utcoffset() is None:
            raise ValueError("缓存读取时间必须带时区")
        # Persist only provider facts; credentials and arbitrary provider response fields never enter the cache.
        payload = {
            "status": "1",
            "search_complete": response.get("search_complete") is True,
            "pois": [
                Poi.model_validate(poi).model_dump(mode="json")
                for poi in response["pois"]
            ],
        }
        with self.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO poi_search_cache(query,city,api_version,payload_json,read_at,expires_at) VALUES(?,?,?,?,?,?) ON CONFLICT(query,city,api_version) DO UPDATE SET payload_json=excluded.payload_json,read_at=excluded.read_at,expires_at=excluded.expires_at",
                (
                    _normalized(query),
                    _normalized(city),
                    api_version,
                    encode(payload),
                    timestamp.astimezone(timezone.utc).isoformat(),
                    (timestamp.astimezone(timezone.utc) + CACHE_TTL).isoformat(),
                ),
            )
        return payload

    def _places_result(self, connection, trip_id, run_id):
        from ..services.places import rank_candidates
        from .database import now

        trip = self._row(connection, trip_id)
        workspace = Workspace.model_validate_json(trip["workspace_json"])
        candidates = [
            self._entity(row, CandidateDraft)
            for row in connection.execute(
                "SELECT * FROM candidates WHERE trip_id=? AND run_id=? ORDER BY rowid",
                (trip_id, run_id),
            )
        ]
        current_ids = {candidate["id"] for candidate in candidates}
        visible = set(workspace.map_visible_ids)
        matches, points, by_candidate = [], {}, {}
        for row in connection.execute(
            "SELECT * FROM candidate_matches WHERE trip_id=? ORDER BY rowid", (trip_id,)
        ):
            if row["candidate_id"] not in current_ids | visible:
                continue
            match = PoiMatch.model_validate_json(row["payload_json"]).model_dump(
                mode="json"
            )
            candidate, mentions = self._candidate_for_places(
                connection, trip_id, row["candidate_id"]
            )
            match.update(
                candidate_id=row["candidate_id"],
                run_id=row["run_id"],
                request_version=row["request_version"],
                confirmed=bool(row["confirmed"]),
                updated_at=row["updated_at"],
            )
            matches.append(match)
            by_candidate[row["candidate_id"]] = match
            if match["status"] != "unique":
                continue
            poi = match["poi"]
            point = points.setdefault(
                poi["id"],
                {
                    **poi,
                    "candidate_ids": [],
                    "source_ids": [],
                    "activity_ids": [],
                    "activity_refs": [],
                    "evidence_ids": [],
                    "evidence_refs": [],
                    "mention_ids": [],
                    "categories": [],
                },
            )
            point["candidate_ids"].append(candidate["id"])
            for field in ("evidence_ids", "mention_ids"):
                point[field] = list(dict.fromkeys([*point[field], *candidate[field]]))
            for identifier in candidate["evidence_ids"]:
                evidence_row = connection.execute(
                    "SELECT source_id FROM evidence WHERE trip_id=? AND run_id=? AND id=?",
                    (trip_id, row["run_id"], identifier),
                ).fetchone()
                if evidence_row is None:
                    from sqlite3 import DatabaseError

                    raise DatabaseError("候选证据引用缺失")
                reference = {"id": identifier, "run_id": row["run_id"]}
                if reference not in point["evidence_refs"]:
                    point["evidence_refs"].append(reference)
                if evidence_row["source_id"] not in point["source_ids"]:
                    point["source_ids"].append(evidence_row["source_id"])
            for mention in mentions:
                if mention["source_id"] not in point["source_ids"]:
                    point["source_ids"].append(mention["source_id"])
                for activity in connection.execute(
                    "SELECT * FROM activities WHERE trip_id=? AND run_id=? AND mention_id=?",
                    (trip_id, row["run_id"], mention["id"]),
                ):
                    item = self._entity(activity, Activity)
                    if item["id"] not in point["activity_ids"]:
                        point["activity_ids"].append(item["id"])
                    reference = {"id": item["id"], "run_id": row["run_id"]}
                    if reference not in point["activity_refs"]:
                        point["activity_refs"].append(reference)
            if candidate["category"] not in point["categories"]:
                point["categories"].append(candidate["category"])
        for candidate in candidates:
            match = by_candidate.get(candidate["id"])
            candidate.update(
                match_status=match["status"] if match else "unmatched",
                poi=match["poi"] if match else None,
            )
            _, mentions = self._candidate_for_places(
                connection, trip_id, candidate["id"]
            )
            candidate["source_ids"] = list(
                dict.fromkeys(mention["source_id"] for mention in mentions)
            )
        sources = [
            self._source(
                connection, trip_id, row["source_id"], run_id, include_private=True
            )
            for row in connection.execute(
                "SELECT source_id FROM source_snapshots WHERE trip_id=? AND run_id=?",
                (trip_id, run_id),
            )
        ]
        evidence = [
            self._entity(row, Evidence)
            for row in connection.execute(
                "SELECT * FROM evidence WHERE trip_id=? AND run_id=?", (trip_id, run_id)
            )
        ]
        activities = [
            self._entity(row, Activity)
            for row in connection.execute(
                "SELECT * FROM activities WHERE trip_id=? AND run_id=?",
                (trip_id, run_id),
            )
        ]
        candidates = rank_candidates(
            candidates,
            TripRequest.model_validate_json(trip["request_json"]).model_dump(
                mode="json"
            ),
            sources=sources,
            evidence=evidence,
            activities=activities,
        )
        return {
            "run_id": run_id,
            "request_version": trip["request_version"],
            "candidates": candidates,
            "matches": matches,
            "points": list(points.values()),
            "trip": self._detail(connection, trip),
            "read_at": now(),
        }

    def list_places(self, trip_id, run_id=None):
        with self.connection() as connection:
            connection.execute("BEGIN")
            selected = self._selected_discovery_run(connection, trip_id, run_id)
            return self._places_result(connection, trip_id, selected)

    def finish_place_match(self, trip_id, body, session, matches):
        from .database import OperationConflict, encode, now

        validated = {
            identifier: PoiMatch.model_validate(value)
            for identifier, value in matches.items()
        }
        with self.connection(write=True) as connection:
            trip, run_id = self._place_scope(
                connection, trip_id, body.expected_version, session["run_id"]
            )
            if run_id is None:
                return self._places_result(connection, trip_id, None)
            current = connection.execute(
                "SELECT token FROM place_match_sessions WHERE run_id=?", (run_id,)
            ).fetchone()
            if current is None or current["token"] != session["token"]:
                raise OperationConflict("已有较新的POI匹配请求")
            allowed = {item["candidate"]["id"] for item in session["items"]}
            if not set(validated).issubset(allowed):
                raise OperationConflict("匹配结果不属于本次候选")
            for identifier, match in validated.items():
                self._candidate_for_places(connection, trip_id, identifier)
                connection.execute(
                    "INSERT INTO candidate_matches(candidate_id,run_id,trip_id,request_version,payload_json,confirmed,updated_at) VALUES(?,?,?,?,?,0,?) ON CONFLICT(candidate_id) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at WHERE candidate_matches.confirmed=0",
                    (
                        identifier,
                        run_id,
                        trip_id,
                        body.expected_version,
                        encode(match.model_dump(mode="json")),
                        now(),
                    ),
                )
            workspace = Workspace.model_validate_json(trip["workspace_json"])
            initialize = not workspace.map_initialized
            update_map = (
                initialize or body.refresh_map
            ) and self._selected_discovery_run(connection, trip_id, None) == run_id
            skipped = (
                update_map
                and trip["workspace_version"] != body.expected_workspace_version
            )
            if update_map and not skipped:
                matched = self._places_result(connection, trip_id, run_id)["candidates"]
                workspace.map_visible_ids = self._top_map_ids(
                    matched, city=workspace.map_city
                )
                if workspace.map_visible_ids or body.refresh_map:
                    workspace.map_initialized = True
                    self._write_place_workspace(connection, trip_id, workspace)
            result = self._places_result(connection, trip_id, run_id)
            result["map_update_skipped"] = skipped
            return result

    @staticmethod
    def _top_map_ids(candidates, *, category=None, city=None):
        selected, seen = [], set()
        for candidate in candidates:
            poi = candidate.get("poi")
            if (
                candidate.get("match_status") != "unique"
                or not poi
                or poi.get("location") is None
            ):
                continue
            if category and candidate["category"] != category:
                continue
            if city and _normalized(poi.get("city") or "").removesuffix(
                "市"
            ) != _normalized(city).removesuffix("市"):
                continue
            if poi["id"] not in seen:
                selected.append(candidate["id"])
                seen.add(poi["id"])
            if len(selected) == 8:
                break
        return selected

    def apply_map_action(self, trip_id, body):
        from .database import OperationConflict

        with self.connection(write=True) as connection:
            trip, run_id = self._place_scope(connection, trip_id, body.expected_version)
            self._version(trip, body.expected_workspace_version, "workspace_version")
            workspace = Workspace.model_validate_json(trip["workspace_json"])
            before = workspace.model_dump(mode="json")
            if body.action in {"initialize", "refresh"}:
                if body.action == "refresh" or not workspace.map_initialized:
                    candidates = self._places_result(connection, trip_id, run_id)[
                        "candidates"
                    ]
                    workspace.map_visible_ids = self._top_map_ids(
                        candidates,
                        category=body.category,
                        city=body.city if body.city is not None else workspace.map_city,
                    )
                    workspace.map_initialized = True
            else:
                candidate, _ = self._candidate_for_places(
                    connection, trip_id, body.candidate_id
                )
                row = connection.execute(
                    "SELECT payload_json FROM candidate_matches WHERE candidate_id=? AND trip_id=?",
                    (body.candidate_id, trip_id),
                ).fetchone()
                match = (
                    PoiMatch.model_validate_json(row["payload_json"]) if row else None
                )
                if (
                    match is None
                    or match.status != "unique"
                    or match.poi.location is None
                ):
                    raise OperationConflict("地图操作必须对应唯一匹配的有效POI")
                if (
                    body.action in {"add", "select"}
                    and candidate["request_version"] != body.expected_version
                ):
                    raise OperationConflict("历史条件候选不能加入当前选择")
                field = (
                    "selected_ids"
                    if body.action in {"select", "unselect"}
                    else "map_visible_ids"
                )
                identifiers = getattr(workspace, field)
                if (
                    body.action in {"add", "select"}
                    and body.candidate_id not in identifiers
                ):
                    if len(identifiers) >= 100:
                        raise OperationConflict("工作区最多保留100个地点")
                    identifiers.append(body.candidate_id)
                elif body.action in {"remove", "unselect"}:
                    remove_ids = {body.candidate_id}
                    if field == "map_visible_ids":
                        for linked in connection.execute(
                            "SELECT candidate_id,payload_json FROM candidate_matches WHERE trip_id=?",
                            (trip_id,),
                        ):
                            linked_match = PoiMatch.model_validate_json(
                                linked["payload_json"]
                            )
                            if linked_match.poi and linked_match.poi.id == match.poi.id:
                                remove_ids.add(linked["candidate_id"])
                    setattr(
                        workspace,
                        field,
                        [
                            identifier
                            for identifier in identifiers
                            if identifier not in remove_ids
                        ],
                    )
                if field == "map_visible_ids":
                    workspace.map_initialized = True
            if body.city is not None:
                workspace.map_city = body.city
            if workspace.model_dump(mode="json") != before:
                self._write_place_workspace(connection, trip_id, workspace)
            return self._places_result(connection, trip_id, run_id)

    @staticmethod
    def _write_place_workspace(connection, trip_id, workspace):
        from .database import encode, now

        connection.execute(
            "UPDATE trips SET workspace_json=?,workspace_version=workspace_version+1,updated_at=? WHERE id=?",
            (encode(workspace.model_dump(mode="json")), now(), trip_id),
        )

    def candidate_confirmation_context(self, trip_id, candidate_id, body):
        from .database import OperationConflict

        with self.connection() as connection:
            candidate, mentions = self._candidate_for_places(
                connection, trip_id, candidate_id
            )
            trip, _ = self._place_scope(
                connection, trip_id, body.expected_version, candidate["run_id"]
            )
            self._version(trip, body.expected_workspace_version, "workspace_version")
            row = connection.execute(
                "SELECT payload_json FROM candidate_matches WHERE candidate_id=? AND trip_id=?",
                (candidate_id, trip_id),
            ).fetchone()
            match = PoiMatch.model_validate_json(row["payload_json"]) if row else None
            chosen = (
                next((poi for poi in match.options if poi.id == body.poi_id), None)
                if match
                else None
            )
            if chosen is None:
                raise OperationConflict("只能确认已保存的有效备选POI")
            return {
                "poi": chosen.model_dump(mode="json"),
                "city": candidate.get("city")
                or next(
                    (mention["city"] for mention in mentions if mention.get("city")),
                    None,
                ),
            }

    def confirm_candidate_poi(self, trip_id, candidate_id, body, verified_poi):
        from .database import OperationConflict, encode, now

        with self.connection(write=True) as connection:
            candidate, _ = self._candidate_for_places(connection, trip_id, candidate_id)
            trip, _ = self._place_scope(
                connection, trip_id, body.expected_version, candidate["run_id"]
            )
            self._version(trip, body.expected_workspace_version, "workspace_version")
            row = connection.execute(
                "SELECT * FROM candidate_matches WHERE candidate_id=? AND trip_id=?",
                (candidate_id, trip_id),
            ).fetchone()
            match = PoiMatch.model_validate_json(row["payload_json"]) if row else None
            chosen = (
                next((poi for poi in match.options if poi.id == body.poi_id), None)
                if match
                else None
            )
            if chosen is None or chosen.location is None:
                raise OperationConflict("只能确认已保存的有效备选POI")
            verified = Poi.model_validate(verified_poi)
            if (
                verified.id != chosen.id
                or verified.location is None
                or not verified.name
            ):
                raise OperationConflict("POI详情未确认有效地点")
            match = PoiMatch(
                status="unique",
                poi=verified,
                options=match.options,
                reason="用户确认备选POI",
            )
            connection.execute(
                "UPDATE candidate_matches SET payload_json=?,confirmed=1,updated_at=? WHERE candidate_id=?",
                (encode(match.model_dump(mode="json")), now(), candidate_id),
            )
            workspace = Workspace.model_validate_json(trip["workspace_json"])
            if (
                not workspace.map_initialized
                and self._selected_discovery_run(connection, trip_id, None)
                == candidate["run_id"]
            ):
                workspace.map_visible_ids = self._top_map_ids(
                    self._places_result(connection, trip_id, candidate["run_id"])[
                        "candidates"
                    ],
                    city=workspace.map_city,
                )
                workspace.map_initialized = True
                self._write_place_workspace(connection, trip_id, workspace)
            return self._places_result(connection, trip_id, candidate["run_id"])

    def save_origin_search(self, trip_id, expected_version, options):
        from .database import encode, now

        valid = [Poi.model_validate(option) for option in options]
        valid = [
            option
            for option in valid
            if option.id and option.name and option.location is not None
        ]
        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            self._version(trip, expected_version)
            origin = TripRequest.model_validate_json(trip["request_json"]).origin
            search_id = str(uuid4())
            items = [option.model_dump(mode="json") for option in valid]
            connection.execute(
                "INSERT INTO origin_searches(id,trip_id,request_version,request_origin,payload_json,read_at) VALUES(?,?,?,?,?,?)",
                (search_id, trip_id, expected_version, origin, encode(items), now()),
            )
            return {
                "search_id": search_id,
                "request_version": expected_version,
                "request_origin": origin,
                "items": items,
            }

    def origin_confirmation_context(self, trip_id, body):
        from .database import OperationConflict

        with self.connection() as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            self._version(trip, body.expected_version)
            self._version(trip, body.expected_workspace_version, "workspace_version")
            search = connection.execute(
                "SELECT * FROM origin_searches WHERE id=? AND trip_id=? AND request_version=?",
                (str(body.search_id), trip_id, body.expected_version),
            ).fetchone()
            if search is None:
                raise OperationConflict("起点搜索不属于当前行程条件")
            chosen = next(
                (
                    Poi.model_validate(poi)
                    for poi in json.loads(search["payload_json"])
                    if poi.get("id") == body.poi_id
                ),
                None,
            )
            if chosen is None or chosen.location is None:
                raise OperationConflict("只能确认已保存搜索的有效POI")
            return {"poi": chosen.model_dump(mode="json"), "city": chosen.city}

    def confirm_origin(self, trip_id, body, verified_poi=None):
        from .database import OperationConflict

        with self.connection(write=True) as connection:
            trip = self._row(connection, trip_id)
            self._editable(trip)
            self._version(trip, body.expected_version)
            self._version(trip, body.expected_workspace_version, "workspace_version")
            request_origin = TripRequest.model_validate_json(
                trip["request_json"]
            ).origin
            if body.source == "amap":
                search = connection.execute(
                    "SELECT * FROM origin_searches WHERE id=? AND trip_id=? AND request_version=?",
                    (str(body.search_id), trip_id, body.expected_version),
                ).fetchone()
                if search is None or search["request_origin"] != request_origin:
                    raise OperationConflict("起点搜索不属于当前行程条件")
                chosen = next(
                    (
                        Poi.model_validate(poi)
                        for poi in json.loads(search["payload_json"])
                        if poi.get("id") == body.poi_id
                    ),
                    None,
                )
                if chosen is None or chosen.location is None or not chosen.name:
                    raise OperationConflict("只能确认已保存搜索的有效POI")
                verified = Poi.model_validate(verified_poi)
                if (
                    verified.id != chosen.id
                    or verified.location is None
                    or not verified.name
                ):
                    raise OperationConflict("POI详情未确认有效起点")
                origin = ConfirmedOrigin(
                    name=verified.name,
                    location=verified.location,
                    poi_id=verified.id,
                    source="amap",
                    request_origin=request_origin,
                    city=verified.city,
                )
            else:
                origin = ConfirmedOrigin(
                    name=body.name,
                    location=body.location,
                    source="map",
                    request_origin=request_origin,
                )
            workspace = Workspace.model_validate_json(trip["workspace_json"])
            workspace.confirmed_origin = origin
            self._write_place_workspace(connection, trip_id, workspace)
            return self._detail(connection, self._row(connection, trip_id))
