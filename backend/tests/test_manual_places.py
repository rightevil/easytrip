import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_places import map_action, match_saved, raw_poi, seed
from test_planning import ready, saved_plan

from easytrip.api import places as places_api
from easytrip.api.places import make_router
from easytrip.api.trips import make_router as trips_router
from easytrip.config import Settings
from easytrip.domain.models import (
    ArchiveTrip,
    CreateTrip,
    TripPatch,
    TripRequest,
    Workspace,
    WorkspacePatch,
)
from easytrip.domain.places import ConfirmManualPoi
from easytrip.domain.planning import AdoptPlan, ReplacePlanningCandidate
from easytrip.services.places import normalize_poi
from easytrip.storage.database import Database, OperationConflict, VersionConflict, now


@pytest.fixture
def api(tmp_path, monkeypatch):
    database = Database(tmp_path / "manual.sqlite3")
    database.initialize()
    trip = database.create_trip(CreateTrip(operation_id=uuid4()))
    mode, calls = {}, []

    async def provider(request):
        calls.append(request)
        if request.url.path.endswith("detail"):
            await asyncio.sleep(0.005)
            if mode.get("on_detail"):
                mode["on_detail"]()
            poi = raw_poi(**mode.get("detail", {}))
            return httpx.Response(200, json={"status": "1", "pois": [poi]})
        if mode.get("on_search"):
            mode["on_search"]()
        pois = mode.get("search", [raw_poi()])
        return httpx.Response(
            200, json={"status": "1", "count": str(len(pois)), "pois": pois}
        )

    async def no_gate(application):
        pass

    monkeypatch.setattr(places_api, "_before_amap_request", no_gate)
    app = FastAPI()
    app.state.database = database
    app.state.settings = Settings(amap_web_key="mock-secret", secrets={"mock-secret"})
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    app.include_router(make_router())
    app.include_router(trips_router())
    with TestClient(app) as client:
        yield client, database, trip, mode, calls
        client.portal.call(app.state.http.aclose)


def search(client, trip, **changes):
    return client.get(
        f"/api/trips/{trip['id']}/places/manual/search",
        params={
            "query": "西湖",
            "city": "杭州",
            "expected_version": trip["request_version"],
            **changes,
        },
    )


def body(trip, search_id, **changes):
    return {
        "operation_id": str(uuid4()),
        "expected_version": trip["request_version"],
        "expected_workspace_version": trip["workspace_version"],
        "search_id": search_id,
        "poi_id": "poi-1",
        "category": "play",
        **changes,
    }


def confirm(client, trip, payload):
    return client.post(f"/api/trips/{trip['id']}/places/manual/confirm", json=payload)


def saved_manual(database, trip, identifier="poi-1", name="西湖"):
    poi = normalize_poi(raw_poi(identifier, name), now())
    result = database.save_manual_search(
        trip["id"], trip["request_version"], name, "杭州", [poi]
    )
    payload = ConfirmManualPoi(**body(trip, result["search_id"], poi_id=identifier))
    return database.confirm_manual_poi(trip["id"], payload, poi)[
        "candidate_id"
    ], payload


def test_manual_search_confirm_and_independent_membership_need_no_discovery(api):
    client, database, trip, _, calls = api
    result = search(client, trip)
    assert result.status_code == 200
    assert result.json()["query"] == "西湖" and result.json()["city"] == "杭州"
    payload = body(trip, result.json()["search_id"])
    result = confirm(client, trip, payload)
    assert result.status_code == 200, result.text
    snapshot = result.json()
    candidate = snapshot["manual_candidates"][0]
    assert candidate["source"] == "user" and candidate["run_id"] is None
    assert (
        candidate["source_ids"]
        == candidate["evidence_ids"]
        == candidate["mention_ids"]
        == []
    )
    assert "stats" not in candidate
    assert (
        snapshot["run_id"] is None
        and snapshot["candidates"] == snapshot["matches"] == []
    )
    assert snapshot["trip"]["workspace_version"] == trip["workspace_version"]
    assert snapshot["trip"]["workspace"]["selected_ids"] == []
    assert snapshot["trip"]["workspace"]["map_visible_ids"] == []
    assert snapshot["points"][0]["candidate_ids"] == [candidate["id"]]
    assert snapshot["points"][0]["source_ids"] == []
    selected = map_action(database, trip, "select", candidate_id=candidate["id"])[
        "trip"
    ]
    assert selected["workspace"]["map_visible_ids"] == []
    mapped = map_action(database, selected, "add", candidate_id=candidate["id"])["trip"]
    assert (
        mapped["workspace"]["selected_ids"]
        == mapped["workspace"]["map_visible_ids"]
        == [candidate["id"]]
    )
    assert len(calls) == 2
    with database.connection() as connection:
        for table in ("runs", "sources", "candidates", "evidence", "place_mentions"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
    assert "mock-secret" not in result.text


def test_successful_retry_skips_provider_and_returns_current_state(api):
    client, database, trip, _, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    first = confirm(client, trip, payload).json()
    changed = map_action(database, trip, "add", candidate_id=first["candidate_id"])[
        "trip"
    ]
    retry = confirm(client, trip, payload)
    assert retry.status_code == 200
    assert retry.json()["candidate_id"] == first["candidate_id"]
    assert retry.json()["trip"] == changed
    assert len(retry.json()["manual_candidates"]) == 1 and len(calls) == 2
    current = database.update_trip(
        trip["id"], TripPatch(expected_version=1, name="较新的行程条件")
    )
    client.app.state.database = Database(database.path)
    client.app.state.settings = Settings()
    restored = confirm(client, trip, payload)
    assert restored.status_code == 200 and restored.json()["trip"] == current
    assert len(calls) == 2
    assert confirm(client, trip, {**payload, "category": "eat"}).status_code == 409
    assert len(calls) == 2


def test_failed_detail_can_be_retried_with_same_operation(api):
    client, database, trip, mode, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    mode["detail"] = {"cityname": "宁波市"}
    assert confirm(client, trip, payload).status_code == 502
    assert database.list_places(trip["id"])["manual_candidates"] == []
    mode.clear()
    result = confirm(client, trip, payload)
    assert result.status_code == 200 and len(result.json()["manual_candidates"]) == 1
    assert len(calls) == 3


def test_concurrent_confirm_retry_calls_detail_only_once(api):
    client, _, trip, _, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(client, trip, payload), range(2)))
    assert all(result.status_code == 200 for result in results)
    assert results[0].json()["candidate_id"] == results[1].json()["candidate_id"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "detail",
    [
        {"identifier": "foreign"},
        {"cityname": "宁波市"},
        {"cityname": []},
        {"location": "181,30"},
        {"location": []},
        {"name": ""},
    ],
)
def test_invalid_details_never_save_a_candidate(api, detail):
    client, database, trip, mode, _ = api
    payload = body(trip, search(client, trip).json()["search_id"])
    mode["detail"] = detail
    response = confirm(client, trip, payload)
    assert response.status_code == 502
    assert database.list_places(trip["id"])["manual_candidates"] == []
    assert database.get_trip(trip["id"])["workspace_version"] == 1


def test_search_filters_wrong_city_missing_id_and_invalid_coordinates(api):
    client, _, trip, mode, _ = api
    mode["search"] = [
        raw_poi(),
        raw_poi("other", cityname="宁波市"),
        raw_poi("", name="无标识"),
        raw_poi("bad", location="181,30"),
    ]
    result = search(client, trip)
    assert result.status_code == 200
    assert [poi["id"] for poi in result.json()["items"]] == ["poi-1"]
    payload = body(trip, result.json()["search_id"], poi_id="other")
    assert confirm(client, trip, payload).status_code == 409


def test_search_ownership_request_version_archive_and_unlisted_poi(api):
    client, database, trip, _, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    other = database.create_trip(CreateTrip(operation_id=uuid4()))
    assert confirm(client, other, payload).status_code == 409
    assert confirm(client, trip, {**payload, "poi_id": "invented"}).status_code == 409
    changed = database.update_trip(
        trip["id"], TripPatch(expected_version=1, name="改条件")
    )
    assert confirm(client, trip, payload).status_code == 409
    assert confirm(client, trip, {**payload, "expected_version": 2}).status_code == 409
    assert search(client, trip).status_code == 409
    archived = database.archive_trip(
        trip["id"], ArchiveTrip(expected_version=2, archived=True)
    )
    assert search(client, archived).status_code == 409
    assert (
        confirm(
            client, trip, {**payload, "expected_version": archived["request_version"]}
        ).status_code
        == 409
    )
    assert changed["request_version"] == 2 and len(calls) == 1


@pytest.mark.parametrize("phase", ["search", "detail"])
def test_late_provider_response_cannot_overwrite_changed_request(api, phase):
    client, database, trip, mode, _ = api
    payload = body(trip, search(client, trip).json()["search_id"])
    mode[f"on_{phase}"] = lambda: database.update_trip(
        trip["id"], TripPatch(expected_version=1, name="较新的条件")
    )
    result = (
        search(client, trip, query="断桥")
        if phase == "search"
        else confirm(client, trip, payload)
    )
    assert result.status_code == 409
    assert database.list_places(trip["id"])["manual_candidates"] == []


def test_late_detail_workspace_cas_and_archive_are_rechecked(api):
    client, database, trip, mode, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    mode["on_detail"] = lambda: database.save_workspace(
        trip["id"],
        WorkspacePatch(expected_version=1, workspace=Workspace(active_tab="map")),
    )
    assert confirm(client, trip, payload).status_code == 409
    assert database.list_places(trip["id"])["manual_candidates"] == []
    assert len(calls) == 2
    mode["on_detail"] = lambda: database.archive_trip(
        trip["id"], ArchiveTrip(expected_version=1, archived=True)
    )
    assert (
        confirm(client, trip, {**payload, "expected_workspace_version": 2}).status_code
        == 409
    )
    assert database.list_places(trip["id"])["manual_candidates"] == []


def test_time_edits_keep_manual_map_selection_and_frozen_provenance(api):
    _, database, trip, _, _ = api
    identifier, _ = saved_manual(database, trip)
    original_hash = database.planning_input(trip["id"])["input_hash"]
    second, _ = saved_manual(database, trip, "poi-2", "断桥")
    assert database.planning_input(trip["id"])["input_hash"] == original_hash
    trip = map_action(database, trip, "add", candidate_id=identifier)["trip"]
    trip = map_action(database, trip, "select", candidate_id=identifier)["trip"]
    trip = database.update_trip(
        trip["id"],
        TripPatch(
            expected_version=1,
            request=TripRequest(
                start_at="2026-10-16T10:00:00+08:00", duration_minutes=360
            ),
        ),
    )
    trip = map_action(database, trip, "select", candidate_id=second)["trip"]
    value = Database(database.path).planning_input(trip["id"])
    assert len(value["selected"]) == 2 and value["input_hash"] != original_hash
    for selected in value["selected"]:
        assert selected["source"] == "user" and selected["run_id"] is None
        assert (
            selected["sources"]
            == selected["mentions"]
            == selected["evidence"]
            == selected["activities"]
            == []
        )
    assert (
        len(Database(database.path).list_places(trip["id"])["manual_candidates"]) == 2
    )
    assert trip["workspace"]["map_visible_ids"] == [identifier]
    saved = database.save_workspace(
        trip["id"],
        WorkspacePatch(
            expected_version=trip["workspace_version"], workspace=Workspace()
        ),
    )
    assert saved["workspace"]["selected_ids"] == [identifier, second]


def test_manual_and_discovery_same_poi_deduplicate_and_explicit_refresh_keeps_both(api):
    _, database, trip, _, _ = api
    trip, _, candidates, *_ = seed(database, trip)
    matched = match_saved(database, trip, candidates[0])
    manual_id, _ = saved_manual(database, matched["trip"])
    result = map_action(database, matched["trip"], "add", candidate_id=manual_id)
    assert len(result["points"]) == 1
    assert set(result["points"][0]["candidate_ids"]) == {manual_id, candidates[0].id}
    assert result["points"][0]["source_ids"]
    other_id, _ = saved_manual(database, result["trip"], "poi-2", "断桥")
    refreshed = match_saved(database, result["trip"], candidates[0], refresh_map=True)
    assert other_id in refreshed["trip"]["workspace"]["map_visible_ids"]
    removed = map_action(database, refreshed["trip"], "remove", candidate_id=manual_id)
    assert manual_id not in removed["trip"]["workspace"]["map_visible_ids"]
    assert candidates[0].id not in removed["trip"]["workspace"]["map_visible_ids"]
    assert len(removed["manual_candidates"]) == 2


def test_manual_candidates_work_with_historical_discovery_and_planning_replacement(api):
    _, database, trip, _, _ = api
    trip, _, candidates, *_ = seed(database, trip)
    trip = match_saved(database, trip, candidates[0])["trip"]
    original, _ = saved_manual(database, trip)
    replacement, _ = saved_manual(database, trip, "poi-2", "断桥")
    trip = database.update_trip(
        trip["id"], TripPatch(expected_version=1, name="时间变更")
    )
    trip = map_action(database, trip, "select", candidate_id=original)["trip"]
    assert replacement in {
        item["candidate_id"]
        for item in database.planning_replacements(trip["id"])["candidates"]
    }
    value = database.planning_input(trip["id"])
    result = database.replace_planning_candidate(
        trip["id"],
        ReplacePlanningCandidate(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            expected_workspace_version=trip["workspace_version"],
            input_hash=value["input_hash"],
            candidate_id=original,
            replacement_id=replacement,
        ),
    )
    assert result["trip"]["workspace"]["selected_ids"] == [replacement]
    assert database.planning_input(trip["id"])["selected"][0]["source"] == "user"


def test_delete_trip_cascades_searches_candidates_and_receipts(api):
    _, database, trip, _, _ = api
    identifier, _ = saved_manual(database, trip)
    assert identifier
    database.delete_trip(trip["id"], 1)
    with database.connection() as connection:
        for table in ("manual_candidates", "manual_place_searches"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )


def test_migration_007_to_008_preserves_existing_trip_workspace_and_adopted_plan(
    tmp_path,
):
    database = Database(tmp_path / "migration.sqlite3")
    database.initialize()
    trip, _ = ready(database)
    plan, _ = saved_plan(database, trip["id"])
    database.adopt_plan(
        trip["id"],
        plan["id"],
        AdoptPlan(
            operation_id=uuid4(),
            expected_version=trip["request_version"],
            input_hash=plan["input_hash"],
        ),
    )
    before_trip = database.get_trip(trip["id"])
    before_plans = database.list_plans(trip["id"])
    # Remove additive tables from later migrations to recreate the 007 schema.
    with database.connection(write=True) as connection:
        connection.execute("DROP TABLE api_actions")
        connection.execute("DROP TABLE manual_candidates")
        connection.execute("DROP TABLE manual_place_searches")
        connection.execute("PRAGMA user_version=7")
    migrated = Database(database.path)
    migrated.initialize()
    assert migrated.get_trip(trip["id"]) == before_trip
    assert migrated.list_plans(trip["id"]) == before_plans
    assert migrated.get_trip(trip["id"])["active_plan_id"] == plan["id"]
    identifier, _ = saved_manual(migrated, before_trip)
    assert migrated.list_places(trip["id"])["manual_candidates"][0]["id"] == identifier
    assert migrated.list_plans(trip["id"]) == before_plans
    with migrated.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9


@pytest.mark.parametrize(
    "changes", [{"query": " "}, {"city": " "}, {"query": "x" * 81}]
)
def test_search_input_validation_never_calls_provider(api, changes):
    client, _, trip, _, calls = api
    assert search(client, trip, **changes).status_code == 422
    assert calls == []


def test_manual_confirmation_rejects_untrusted_fields_and_wrong_versions(api):
    client, database, trip, _, calls = api
    payload = body(trip, search(client, trip).json()["search_id"])
    for changes in (
        {"source": "xhs"},
        {"location": [120, 30]},
        {"category": "shopping"},
        {"operation_id": "not-uuid"},
        {"expected_version": True},
    ):
        assert confirm(client, trip, {**payload, **changes}).status_code == 422
    assert (
        confirm(client, trip, {**payload, "expected_workspace_version": 2}).status_code
        == 409
    )
    assert len(calls) == 1
    model = ConfirmManualPoi(**payload)
    with pytest.raises(VersionConflict):
        database.confirm_manual_poi(
            trip["id"], model.model_copy(update={"expected_version": 2}), None
        )
    with pytest.raises(OperationConflict):
        database.confirm_manual_poi(
            trip["id"], model.model_copy(update={"search_id": uuid4()}), None
        )
