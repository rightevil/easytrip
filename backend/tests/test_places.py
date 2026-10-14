import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from easytrip.api import places as places_api
from easytrip.api.places import make_router
from easytrip.api.trips import make_router as trips_router
from easytrip.config import Settings
from easytrip.domain.discovery import (
    Activity,
    CandidateDraft,
    Evidence,
    PlaceMention,
    SourceComment,
    SourceNote,
)
from easytrip.domain.models import (
    CreateTrip,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)
from easytrip.domain.places import ConfirmOrigin, ConfirmPoi, MapAction, MatchPlaces
from easytrip.domain.tasks import CreateRun
from easytrip.services.places import normalize_poi
from easytrip.storage.database import (
    Database,
    NotFound,
    OperationConflict,
    VersionConflict,
    now,
)


def raw_poi(identifier="poi-1", name="西湖", **changes):
    return {
        "id": identifier,
        "name": name,
        "location": "120.15,30.25",
        "cityname": "杭州市",
        "address": "杭州西湖区",
        "adcode": "330106",
        "type": "风景名胜;公园",
        **changes,
    }


def unique(poi=None):
    poi = poi or normalize_poi(raw_poi(), now())
    return {"status": "unique", "poi": poi, "options": [poi], "reason": "明确地点"}


def seed(database, trip=None, names=("西湖",)):
    trip = trip or database.create_trip(
        CreateTrip(
            operation_id=uuid4(),
            request=TripRequest(destination="杭州", origin="杭州东站"),
        )
    )
    database.enqueue_run(
        trip["id"],
        CreateRun(operation_id=uuid4(), expected_version=trip["request_version"]),
    )
    run = database.claim_run()
    note = database.save_source(
        trip["id"], run["id"], run["generation"], SourceNote(text="，".join(names))
    )
    mentions = [
        PlaceMention(
            source_id=note["id"], name=name, city="杭州", category="play", quote=name
        )
        for name in names
    ]
    candidates = [
        CandidateDraft(
            name=name, city="杭州", category="play", mention_ids=[mention.id]
        )
        for name, mention in zip(names, mentions)
    ]
    activities = [
        Activity(
            source_id=note["id"],
            mention_id=mention.id,
            quote=mention.name,
            description="散步",
        )
        for mention in mentions
    ]
    database.save_discovery_batch(
        trip["id"],
        run["id"],
        run["generation"],
        mentions=mentions,
        candidates=candidates,
        activities=activities,
    )
    # P3 may read a completed discovery; it does not reopen or mutate its checkpoint.
    with database.connection(write=True) as connection:
        connection.execute(
            "UPDATE runs SET status='completed' WHERE id=?", (run["id"],)
        )
    return trip, run, candidates, note, activities


@pytest.fixture
def saved(tmp_path):
    database = Database(tmp_path / "places.sqlite3")
    database.initialize()
    trip, run, candidates, note, activities = seed(database)
    return database, trip, run, candidates[0], note, activities[0]


def match_saved(database, trip, candidate, result=None, **changes):
    current = database.get_trip(trip["id"])
    body = MatchPlaces(
        expected_version=current["request_version"],
        expected_workspace_version=current["workspace_version"],
        **changes,
    )
    session = database.begin_place_match(trip["id"], body)
    return database.finish_place_match(
        trip["id"], body, session, {candidate.id: result or unique()}
    )


def map_action(database, trip, action, **changes):
    current = database.get_trip(trip["id"])
    return database.apply_map_action(
        trip["id"],
        MapAction(
            expected_version=current["request_version"],
            expected_workspace_version=current["workspace_version"],
            action=action,
            **changes,
        ),
    )


def test_overlay_initializes_once_and_preserves_p2_and_independent_selection(saved):
    database, trip, run, candidate, note, activity = saved
    original = database.list_candidates(trip["id"])
    result = match_saved(database, trip, candidate)
    assert result["trip"]["workspace"]["map_visible_ids"] == [candidate.id]
    assert result["trip"]["workspace"]["selected_ids"] == []
    assert result["trip"]["workspace"]["map_initialized"]
    assert result["candidates"][0]["match_status"] == "unique"
    assert result["candidates"][0]["ranking_reasons"]
    assert result["points"][0]["source_ids"] == [note["id"]]
    assert result["points"][0]["activity_ids"] == [activity.id]
    assert database.list_candidates(trip["id"]) == original
    result = map_action(database, trip, "select", candidate_id=candidate.id)
    result = map_action(database, trip, "remove", candidate_id=candidate.id)
    assert result["trip"]["workspace"]["selected_ids"] == [candidate.id]
    assert result["trip"]["workspace"]["map_visible_ids"] == []
    result = match_saved(database, trip, candidate)
    assert result["trip"]["workspace"]["map_visible_ids"] == []
    assert (
        Database(database.path).list_places(trip["id"])["matches"] == result["matches"]
    )
    result = map_action(database, trip, "refresh")
    assert result["trip"]["workspace"]["map_visible_ids"] == [candidate.id]
    assert result["trip"]["request_version"] == 1


def test_top8_counts_distinct_pois_and_keeps_all_alias_references(tmp_path):
    database = Database(tmp_path / "distinct.sqlite3")
    database.initialize()
    trip, run, candidates, note, activities = seed(
        database, names=tuple(f"公园{i}" for i in range(11))
    )
    body = MatchPlaces(expected_version=1, expected_workspace_version=1)
    session = database.begin_place_match(trip["id"], body)
    matches = {
        candidate.id: unique(
            normalize_poi(raw_poi(f"poi-{max(0, i - 1)}", candidate.name), now())
        )
        for i, candidate in enumerate(candidates)
    }
    result = database.finish_place_match(trip["id"], body, session, matches)
    assert len(result["trip"]["workspace"]["map_visible_ids"]) == 8
    assert candidates[1].id not in result["trip"]["workspace"]["map_visible_ids"]
    point = next(point for point in result["points"] if point["id"] == "poi-0")
    assert point["candidate_ids"] == [candidates[0].id, candidates[1].id]
    assert point["activity_ids"] == [activities[0].id, activities[1].id]
    result = map_action(database, trip, "remove", candidate_id=candidates[1].id)
    assert candidates[0].id not in result["trip"]["workspace"]["map_visible_ids"]


def test_new_run_and_condition_change_keep_old_map_until_explicit_refresh(saved):
    database, trip, run, candidate, *_ = saved
    match_saved(database, trip, candidate)
    trip = database.update_trip(
        trip["id"],
        TripPatch(
            expected_version=1, request=TripRequest(destination="杭州", origin="另一处")
        ),
    )
    _, newer, candidates, *_ = seed(database, trip, names=("新的公园",))
    result = match_saved(
        database,
        trip,
        candidates[0],
        unique(normalize_poi(raw_poi("new-poi", "新的公园"), now())),
    )
    assert result["trip"]["workspace"]["map_visible_ids"] == [candidate.id]
    assert {point["id"] for point in result["points"]} == {"poi-1", "new-poi"}
    with pytest.raises(OperationConflict):
        map_action(database, trip, "select", candidate_id=candidate.id)
    result = map_action(database, trip, "refresh")
    assert result["trip"]["workspace"]["map_visible_ids"] == [candidates[0].id]


def test_late_match_cannot_overwrite_changed_request_or_newer_match(saved):
    database, trip, _, candidate, *_ = saved
    body = MatchPlaces(expected_version=1, expected_workspace_version=1)
    older = database.begin_place_match(trip["id"], body)
    newer = database.begin_place_match(trip["id"], body)
    with pytest.raises(OperationConflict):
        database.finish_place_match(trip["id"], body, older, {candidate.id: unique()})
    database.update_trip(trip["id"], TripPatch(expected_version=1, name="改条件"))
    with pytest.raises(VersionConflict):
        database.finish_place_match(trip["id"], body, newer, {candidate.id: unique()})
    assert database.list_places(trip["id"], newer["run_id"])["matches"] == []


def test_late_match_preserves_newer_workspace(saved):
    database, trip, _, candidate, *_ = saved
    body = MatchPlaces(expected_version=1, expected_workspace_version=1)
    session = database.begin_place_match(trip["id"], body)
    database.save_workspace(
        trip["id"],
        WorkspacePatch(
            expected_version=1,
            workspace=Workspace(map_center=[121, 31], selected_ids=["user-choice"]),
        ),
    )
    result = database.finish_place_match(
        trip["id"], body, session, {candidate.id: unique()}
    )
    assert result["map_update_skipped"]
    assert result["trip"]["workspace"]["map_center"] == [121, 31]
    assert result["trip"]["workspace"]["map_visible_ids"] == []
    assert result["trip"]["workspace_version"] == 2


def test_manual_confirmation_requires_saved_option_and_survives_requery(saved):
    database, trip, _, candidate, *_ = saved
    option = normalize_poi(raw_poi(), now())
    match_saved(
        database,
        trip,
        candidate,
        {"status": "ambiguous", "poi": None, "options": [option], "reason": "需确认"},
    )
    body = ConfirmPoi(
        expected_version=1, expected_workspace_version=1, poi_id="fake-poi"
    )
    with pytest.raises(OperationConflict):
        database.candidate_confirmation_context(trip["id"], candidate.id, body)
    body = body.model_copy(update={"poi_id": "poi-1"})
    result = database.confirm_candidate_poi(trip["id"], candidate.id, body, option)
    assert result["matches"][0]["confirmed"]
    assert result["trip"]["workspace"]["map_initialized"]
    failed = {"status": "unmatched", "poi": None, "options": [], "reason": "服务失败"}
    result = match_saved(database, trip, candidate, failed)
    assert (
        result["matches"][0]["status"] == "unique" and result["matches"][0]["confirmed"]
    )


def test_cache_normalization_ttl_and_projection(saved):
    database, *_ = saved
    poi = normalize_poi(raw_poi(), now())
    database.save_poi_search(
        " ＷＥＳＴ lake ",
        " 杭州 ",
        {"pois": [poi], "search_complete": True, "key": "must-not-save"},
        now(),
    )
    assert database.cached_poi_search("westlake", "杭州")["search_complete"]
    assert database.cached_poi_search("westlake", "杭州", "v4") is None
    with database.connection(write=True) as connection:
        raw = connection.execute(
            "SELECT payload_json FROM poi_search_cache"
        ).fetchone()[0]
        assert "must-not-save" not in raw
        connection.execute(
            "UPDATE poi_search_cache SET expires_at='2000-01-01T00:00:00+00:00'"
        )
    assert database.cached_poi_search("westlake", "杭州") is None


def test_origin_confirmation_is_workspace_only_and_cleared_on_origin_change(saved):
    database, trip, *_ = saved
    search = database.save_origin_search(
        trip["id"], 1, [normalize_poi(raw_poi(), now())]
    )
    body = ConfirmOrigin(
        expected_version=1,
        expected_workspace_version=1,
        source="amap",
        search_id=search["search_id"],
        poi_id="poi-1",
    )
    verified = normalize_poi(raw_poi(location="120.16,30.26"), now())
    result = database.confirm_origin(trip["id"], body, verified)
    assert result["workspace"]["confirmed_origin"]["request_origin"] == "杭州东站"
    assert result["workspace"]["confirmed_origin"]["location"] == [120.16, 30.26]
    assert result["request"]["origin"] == "杭州东站" and result["request_version"] == 1
    result = database.save_workspace(
        trip["id"],
        WorkspacePatch(expected_version=2, workspace=Workspace(active_tab="map")),
    )
    assert result["workspace"]["confirmed_origin"]
    result = database.update_trip(
        trip["id"],
        TripPatch(
            expected_version=1, request=TripRequest(origin="另一站", destination="杭州")
        ),
    )
    assert result["workspace"]["confirmed_origin"] is None
    assert result["workspace_version"] == 4
    with pytest.raises(VersionConflict):
        database.confirm_origin(trip["id"], body, verified)


def test_map_origin_has_no_provider_identity_and_workspace_cannot_forge_confirmed_fields(
    saved,
):
    database, trip, *_ = saved
    body = ConfirmOrigin(
        expected_version=1,
        expected_workspace_version=1,
        source="map",
        name="这里出发",
        location=[120.1, 30.2],
    )
    result = database.confirm_origin(trip["id"], body)
    assert result["workspace"]["confirmed_origin"]["poi_id"] is None
    with pytest.raises(OperationConflict):
        database.save_workspace(
            trip["id"],
            WorkspacePatch(
                expected_version=2, workspace=Workspace(map_initialized=True)
            ),
        )


def test_trip_ownership_and_deletion_cascade(saved):
    database, trip, _, candidate, *_ = saved
    match_saved(database, trip, candidate)
    database.save_origin_search(trip["id"], 1, [normalize_poi(raw_poi(), now())])
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    with pytest.raises(NotFound):
        map_action(database, other, "add", candidate_id=candidate.id)
    database.delete_trip(trip["id"], 1)
    with database.connection() as connection:
        for table in ("candidate_matches", "place_match_sessions", "origin_searches"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


@pytest.fixture
def api(saved, monkeypatch):
    database, trip, run, candidate, *_ = saved
    calls, mode = [], {"detail": "success", "search": "unique"}

    def provider(request):
        calls.append(request)
        if request.url.path.endswith("detail"):
            if mode["detail"] == "fail":
                return httpx.Response(
                    200,
                    json={
                        "status": "0",
                        "infocode": "10001",
                        "private": "provider-secret",
                    },
                )
            if mode["detail"] == "malformed":
                return httpx.Response(200, json={"status": "1", "pois": ["bad-poi"]})
            if mode["detail"] == "invalid_fields":
                return httpx.Response(
                    200, json={"status": "1", "pois": [raw_poi(name="x" * 2001)]}
                )
            identifier = (
                request.url.params["id"]
                if mode["detail"] in {"success", "wrong_city"}
                else "wrong-id"
            )
            return httpx.Response(
                200,
                json={
                    "status": "1",
                    "pois": [
                        raw_poi(
                            identifier,
                            cityname="上海市"
                            if mode["detail"] == "wrong_city"
                            else "杭州市",
                        )
                    ],
                },
            )
        pois = [raw_poi()]
        if mode["search"] == "ambiguous":
            pois.append(raw_poi("poi-2", "西湖(另一分店)"))
        return httpx.Response(
            200,
            json={
                "status": "1",
                "count": str(len(pois)),
                "pois": pois,
                "key": "provider-secret",
            },
        )

    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(
        amap_web_key="provider-secret", secrets={"provider-secret"}
    )
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def no_gate(application):
        pass

    monkeypatch.setattr(places_api, "_before_amap_request", no_gate)
    app.include_router(make_router())
    app.include_router(trips_router())
    with TestClient(app) as client:
        yield client, database, trip, candidate, calls, mode
        client.portal.call(app.state.http.aclose)


def test_match_api_returns_snapshot_uses_cache_and_keeps_key_out(api):
    client, database, trip, candidate, calls, mode = api
    url = f"/api/trips/{trip['id']}/places"
    response = client.post(
        url + "/match", json={"expected_version": 1, "expected_workspace_version": 1}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["matches"][0]["status"] == "unique"
    assert result["budget"]["calls"] == 1
    assert "provider-secret" not in response.text
    assert calls[0].url.params["key"] == "provider-secret"
    response = client.post(
        url + "/match", json={"expected_version": 1, "expected_workspace_version": 2}
    )
    assert response.json()["budget"]["calls"] == 0
    assert len(calls) == 1
    assert client.get(url).json()["trip"]["workspace"]["map_visible_ids"] == [
        candidate.id
    ]
    with database.connection() as connection:
        assert (
            "provider-secret"
            not in connection.execute(
                "SELECT payload_json FROM poi_search_cache"
            ).fetchone()[0]
        )


def test_confirm_api_details_selected_id_and_failure_preserves_saved_options(api):
    client, database, trip, candidate, calls, mode = api
    mode["search"] = "ambiguous"
    url = f"/api/trips/{trip['id']}/places"
    assert (
        client.post(
            url + "/match",
            json={"expected_version": 1, "expected_workspace_version": 1},
        ).json()["matches"][0]["status"]
        == "ambiguous"
    )
    body = {"expected_version": 1, "expected_workspace_version": 1, "poi_id": "fake"}
    assert (
        client.post(url + f"/candidates/{candidate.id}/confirm", json=body).status_code
        == 409
    )
    assert len(calls) == 1
    body["poi_id"] = "poi-1"
    for failure in ("fail", "wrong", "wrong_city", "malformed", "invalid_fields"):
        mode["detail"] = failure
        response = client.post(url + f"/candidates/{candidate.id}/confirm", json=body)
        assert response.status_code == 502
        assert "provider-secret" not in response.text
        assert client.get(url).json()["matches"][0]["status"] == "ambiguous"
    mode["detail"] = "success"
    response = client.post(url + f"/candidates/{candidate.id}/confirm", json=body)
    assert response.status_code == 200
    assert response.json()["matches"][0]["confirmed"]
    assert calls[-1].url.path.endswith("detail")


def test_origin_api_requires_confirmation_and_validated_detail(api):
    client, database, trip, candidate, calls, mode = api
    url = f"/api/trips/{trip['id']}/places"
    response = client.get(
        url + "/origin/search",
        params={"query": "西湖", "city": "杭州", "expected_version": 1},
    )
    assert response.status_code == 200, response.text
    search = response.json()
    assert search["request_origin"] == "杭州东站"
    assert database.get_trip(trip["id"])["workspace"]["confirmed_origin"] is None
    body = {
        "expected_version": 1,
        "expected_workspace_version": 1,
        "source": "amap",
        "search_id": search["search_id"],
        "poi_id": "poi-1",
    }
    assert (
        client.post(
            url + "/origin/confirm", json={**body, "location": [120, 30]}
        ).status_code
        == 422
    )
    response = client.post(url + "/origin/confirm", json=body)
    assert response.status_code == 200 and response.json()["request_version"] == 1
    assert response.json()["workspace"]["confirmed_origin"]["source"] == "amap"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "suffix,body",
    [
        ("/match", {"expected_version": True, "expected_workspace_version": 1}),
        (
            "/match",
            {
                "expected_version": 1,
                "expected_workspace_version": 1,
                "candidate_ids": [str(i) for i in range(21)],
            },
        ),
        (
            "/map",
            {"expected_version": 1, "expected_workspace_version": 1, "action": "add"},
        ),
        (
            "/origin/confirm",
            {
                "expected_version": 1,
                "expected_workspace_version": 1,
                "source": "map",
                "name": "起点",
                "location": [200, 30],
            },
        ),
    ],
)
def test_places_boundaries(api, suffix, body):
    client, _, trip, *_ = api
    assert (
        client.post(f"/api/trips/{trip['id']}/places" + suffix, json=body).status_code
        == 422
    )


def test_api_bounds_candidates_and_physical_page_calls(tmp_path, monkeypatch):
    database = Database(tmp_path / "budget.sqlite3")
    database.initialize()
    trip, *_ = seed(database, names=tuple(f"公园{i}" for i in range(21)))
    calls = []

    def provider(request):
        calls.append(request)
        page = int(request.url.params["page_num"])
        pois = [
            raw_poi(f"{request.url.params['keywords']}-{page}-{index}")
            for index in range(20)
        ]
        return httpx.Response(200, json={"status": "1", "count": "20", "pois": pois})

    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(amap_web_key="safe-test-key")
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))

    async def no_gate(application):
        pass

    monkeypatch.setattr(places_api, "_before_amap_request", no_gate)
    app.include_router(make_router())
    with TestClient(app) as client:
        response = client.post(
            f"/api/trips/{trip['id']}/places/match",
            json={"expected_version": 1, "expected_workspace_version": 1},
        )
        assert response.status_code == 200, response.text
        assert len(calls) == response.json()["budget"]["calls"] == 40
        assert response.json()["budget"]["processed"] == 20
        assert response.json()["budget"]["limited"]
        client.portal.call(app.state.http.aclose)


def test_api_timeout_keeps_workspace_and_original_candidates(api, monkeypatch):
    client, database, trip, candidate, calls, mode = api
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        places_api.asyncio,
        "timeout",
        lambda seconds: real_timeout(0.001 if seconds == 60 else seconds),
    )

    async def slow_provider(request):
        await asyncio.sleep(0.02)
        return httpx.Response(
            200, json={"status": "1", "count": "1", "pois": [raw_poi()]}
        )

    old_http = client.app.state.http
    client.app.state.http = httpx.AsyncClient(
        transport=httpx.MockTransport(slow_provider)
    )
    response = client.post(
        f"/api/trips/{trip['id']}/places/match",
        json={"expected_version": 1, "expected_workspace_version": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["budget"]["limited"]
    assert response.json()["matches"] == []
    assert database.get_trip(trip["id"])["workspace_version"] == 1
    assert database.list_candidates(trip["id"])["items"][0]["name"] == "西湖"
    client.portal.call(client.app.state.http.aclose)
    client.app.state.http = old_http


def test_origin_late_response_and_cross_trip_search_cannot_be_confirmed(saved):
    database, trip, *_ = saved
    poi = normalize_poi(raw_poi(), now())
    search = database.save_origin_search(trip["id"], 1, [poi])
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    body = ConfirmOrigin(
        expected_version=1,
        expected_workspace_version=1,
        source="amap",
        search_id=search["search_id"],
        poi_id="poi-1",
    )
    with pytest.raises(OperationConflict):
        database.confirm_origin(other["id"], body, poi)
    database.update_trip(
        trip["id"], TripPatch(expected_version=1, request=TripRequest(origin="另一处"))
    )
    with pytest.raises(VersionConflict):
        database.save_origin_search(trip["id"], 1, [poi])
    with pytest.raises(VersionConflict):
        database.confirm_origin(trip["id"], body, poi)


def test_two_amap_instances_share_application_qps_gate():
    app = FastAPI()
    timestamps = []

    async def run():
        async def request():
            await places_api._before_amap_request(app)
            timestamps.append(asyncio.get_running_loop().time())

        await asyncio.gather(request(), request())

    asyncio.run(run())
    assert len(timestamps) == 2 and timestamps[1] - timestamps[0] >= 1.0


def test_p2_migration_retains_existing_trip_and_legacy_workspace(tmp_path):
    database = Database(tmp_path / "migration.sqlite3")
    with sqlite3.connect(database.path) as connection:
        for migration in sorted(
            (Path(__file__).parents[1] / "easytrip" / "storage" / "migrations").glob(
                "00[1-3]_*.sql"
            )
        ):
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.execute("PRAGMA user_version=3")
    trip = database.create_trip(CreateTrip(operation_id=uuid4(), name="保留旧行程"))
    old_workspace = {
        "active_tab": "map",
        "selected_ids": ["legacy-choice"],
        "map_visible_ids": ["legacy-visible"],
    }
    with database.connection(write=True) as connection:
        connection.execute(
            "UPDATE trips SET workspace_json=? WHERE id=?",
            (json.dumps(old_workspace), trip["id"]),
        )
    database.initialize()
    result = database.get_trip(trip["id"])
    assert result["name"] == "保留旧行程"
    assert result["workspace"]["selected_ids"] == ["legacy-choice"]
    assert result["workspace"]["map_visible_ids"] == ["legacy-visible"]
    assert result["workspace"]["confirmed_origin"] is None
    assert result["workspace"]["map_initialized"] is False
    assert database.list_places(trip["id"])["points"] == []


def test_point_retains_original_comment_evidence_source_and_activity_refs(tmp_path):
    database = Database(tmp_path / "references.sqlite3")
    database.initialize()
    trip = database.create_trip(
        CreateTrip(operation_id=uuid4(), request=TripRequest(destination="杭州"))
    )
    database.enqueue_run(
        trip["id"], CreateRun(operation_id=uuid4(), expected_version=1)
    )
    run = database.claim_run()
    note = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceNote(text="西湖散步", author_key="a" * 64),
    )
    comment = database.save_source(
        trip["id"],
        run["id"],
        run["generation"],
        SourceComment(
            parent_source_id=note["id"], text="西湖散步很舒服", author_key="b" * 64
        ),
    )
    mention = PlaceMention(
        source_id=note["id"],
        name="西湖",
        city="杭州",
        category="play",
        quote="西湖散步",
    )
    evidence = Evidence(
        source_id=comment["id"],
        mention_id=mention.id,
        dimension="散步",
        stance="positive",
        nature="experience",
        quote="西湖散步很舒服",
        link_status="linked",
    )
    activity = Activity(
        source_id=note["id"],
        mention_id=mention.id,
        description="散步",
        quote="西湖散步",
    )
    candidate = CandidateDraft(
        name="西湖",
        city="杭州",
        category="play",
        mention_ids=[mention.id],
        evidence_ids=[evidence.id],
    )
    database.save_discovery_batch(
        trip["id"],
        run["id"],
        run["generation"],
        mentions=[mention],
        evidence=[evidence],
        activities=[activity],
        candidates=[candidate],
    )
    result = match_saved(database, trip, candidate)
    point = result["points"][0]
    assert set(point["source_ids"]) == {note["id"], comment["id"]}
    assert point["evidence_refs"] == [{"id": evidence.id, "run_id": run["id"]}]
    assert point["activity_refs"] == [{"id": activity.id, "run_id": run["id"]}]
    assert any(
        "2位不同作者" in reason for reason in result["candidates"][0]["ranking_reasons"]
    )
    assert "author_key" not in json.dumps(result)


def test_initialized_workspace_patch_ignores_external_cross_trip_and_unmatched_ids(api):
    client, database, trip, candidate, *_ = api
    match_saved(database, trip, candidate)
    map_action(database, trip, "select", candidate_id=candidate.id)
    _, _, other_candidates, *_ = seed(database, names=("其他行程公园",))
    _, _, unmatched_candidates, *_ = seed(
        database, database.get_trip(trip["id"]), names=("未匹配地点",)
    )
    current = database.get_trip(trip["id"])
    forged = ["external-id", other_candidates[0].id, unmatched_candidates[0].id]
    response = client.patch(
        f"/api/trips/{trip['id']}/workspace",
        json={
            "expected_version": current["workspace_version"],
            "workspace": {
                "active_tab": "map",
                "map_center": [121, 31],
                "selected_ids": forged,
                "map_visible_ids": forged,
            },
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["workspace"]["selected_ids"] == [candidate.id]
    assert saved["workspace"]["map_visible_ids"] == [candidate.id]
    assert saved["workspace"]["map_center"] == [121, 31]
    assert saved["workspace_version"] == current["workspace_version"] + 1
    assert saved["request_version"] == current["request_version"]
    assert database.list_places(trip["id"])["points"][0]["candidate_ids"] == [
        candidate.id
    ]


def test_old_workspace_cas_cannot_restore_removed_point_and_new_cas_still_keeps_membership(
    api,
):
    client, database, trip, candidate, *_ = api
    original = match_saved(database, trip, candidate)["trip"]
    removed = map_action(database, trip, "remove", candidate_id=candidate.id)["trip"]
    url = f"/api/trips/{trip['id']}/workspace"
    payload = {
        "workspace": {
            "active_tab": "map",
            "map_center": [121, 31],
            "selected_ids": [candidate.id],
            "map_visible_ids": [candidate.id],
        }
    }
    stale = client.patch(
        url, json={**payload, "expected_version": original["workspace_version"]}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == removed["workspace_version"]
    assert database.get_trip(trip["id"]) == removed
    database.update_trip(trip["id"], TripPatch(expected_version=1, name="新条件"))
    saved = client.patch(
        url, json={**payload, "expected_version": removed["workspace_version"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["workspace"]["selected_ids"] == []
    assert saved.json()["workspace"]["map_visible_ids"] == []
    assert saved.json()["workspace"]["map_center"] == [121, 31]
    assert saved.json()["request_version"] == 2


def test_overlay_locks_id_membership_before_map_initialization_without_dropping_p1_ids(
    saved,
):
    database, trip, _, candidate, *_ = saved
    legacy = database.save_workspace(
        trip["id"],
        WorkspacePatch(
            expected_version=1,
            workspace=Workspace(
                selected_ids=["legacy-selected"], map_visible_ids=["legacy-visible"]
            ),
        ),
    )
    option = normalize_poi(raw_poi(), now())
    ambiguous = {
        "status": "ambiguous",
        "poi": None,
        "options": [option],
        "reason": "需确认",
    }
    result = match_saved(database, trip, candidate, ambiguous)
    assert not result["trip"]["workspace"]["map_initialized"]
    saved = database.save_workspace(
        trip["id"],
        WorkspacePatch(
            expected_version=legacy["workspace_version"],
            workspace=Workspace(
                selected_ids=[candidate.id], map_visible_ids=[candidate.id], map_zoom=15
            ),
        ),
    )
    assert saved["workspace"]["selected_ids"] == ["legacy-selected"]
    assert saved["workspace"]["map_visible_ids"] == ["legacy-visible"]
    assert saved["workspace"]["map_zoom"] == 15
    assert database.list_places(trip["id"])["points"] == []
