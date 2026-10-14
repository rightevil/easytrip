import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from easytrip.domain.routes import RouteLeg
from easytrip.integrations.amap import Amap, AmapBudgetExceeded, AmapError
from easytrip.services.routes import RouteProvider, normalize_route
from easytrip.storage.database import Database
from easytrip.storage.routes import RouteStore

DEPARTURE = datetime(2030, 10, 15, 9, 30, tzinfo=timezone(timedelta(hours=8)))
ORIGIN = [120.1, 30.2]
DESTINATION = [120.2, 30.3]
LINE = "120.1,30.2;120.2,30.3"


class RouteDatabase(Database, RouteStore):
    pass


@pytest.fixture
def database(tmp_path):
    database = RouteDatabase(tmp_path / "routes.sqlite3")
    database.initialize()
    return database


def walking_payload(**fields):
    return {
        "status": "1",
        "infocode": "10000",
        "route": {
            "paths": [
                {
                    "distance": "1500",
                    "cost": {"duration": "600"},
                    "steps": [{"polyline": LINE}],
                    **fields,
                }
            ]
        },
    }


def transit_payload():
    return {
        "status": "1",
        "route": {
            "transits": [
                {
                    "distance": "6500",
                    "cost": {"duration": "1200"},
                    "segments": [
                        {
                            "walking": {
                                "distance": "300",
                                "duration": "400",
                                "steps": [{"polyline": LINE}],
                            },
                            "bus": {
                                "buslines": [
                                    {
                                        "duration": "900",
                                        "polyline": "120.2,30.3;120.4,30.5",
                                    },
                                    {
                                        "duration": "800",
                                        "polyline": "120.2,30.3;120.5,30.6",
                                    },
                                ]
                            },
                            "cost": {"transit_fee": "2.50"},
                        }
                    ],
                }
            ]
        },
    }


def district_payload(name="杭州市", code="0571", level="city"):
    return {
        "status": "1",
        "districts": [
            {"name": name, "citycode": code, "level": level, "adcode": "330100"}
        ],
    }


def test_route_cache_preserves_real_facts_and_ignores_arbitrary_provider_fields(
    database,
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                **walking_payload(),
                "key": "web-secret",
                "provider_debug": "private",
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ) as client:
            provider = RouteProvider(Amap(client, "web-secret"), database)
            first = await provider.route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            second = await provider.route(
                "walking", ORIGIN, DESTINATION, "杭州市", DEPARTURE.replace(second=20)
            )
            assert {
                **first,
                "departure_at": DEPARTURE.replace(second=20).isoformat(),
            } == second
            assert len(requests) == 1
            assert first["status"] == "available"
            assert first["duration_seconds"] == 600
            assert first["distance_meters"] == first["walking_distance_meters"] == 1500
            assert first["geometry"] == [[ORIGIN, DESTINATION]]
            assert first["geometry_complete"] is True
            assert first["cost_cny"] is None and first["cost_source"] == "unknown"
            assert "web-secret" not in json.dumps(first)
            assert "private" not in json.dumps(first)
            assert datetime.fromisoformat(
                first["valid_until"]
            ) - datetime.fromisoformat(first["read_at"]) == timedelta(minutes=30)

    asyncio.run(run())
    with database.connection() as connection:
        payload = connection.execute("SELECT payload_json FROM route_cache").fetchone()[
            0
        ]
    assert "web-secret" not in payload and "provider_debug" not in payload


@pytest.mark.parametrize("change", ["direction", "mode", "day", "minute", "city"])
def test_cache_never_reuses_other_direction_mode_date_minute_or_city(database, change):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=walking_payload())
            )
        ) as client:
            amap = Amap(client, "test-only")
            provider = RouteProvider(amap, database)
            await provider.route("walking", ORIGIN, DESTINATION, "杭州", DEPARTURE)
            mode, origin, destination, city, departure = (
                "walking",
                ORIGIN,
                DESTINATION,
                "杭州",
                DEPARTURE,
            )
            if change == "direction":
                origin, destination = destination, origin
            elif change == "mode":
                mode = "driving"
            elif change == "day":
                departure += timedelta(days=1)
            elif change == "minute":
                departure += timedelta(minutes=1)
            else:
                city = "上海"
            await provider.route(mode, origin, destination, city, departure)
            assert len(amap.calls) == 2

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["walking", "transit"])
def test_same_api_minute_reuses_facts_but_binds_the_current_departure_input(
    database, mode
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=district_payload()
            if request.url.path == "/v3/config/district"
            else transit_payload()
            if mode == "transit"
            else walking_payload(),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = RouteProvider(Amap(client, "test-only"), database)
            first = await provider.route(mode, ORIGIN, DESTINATION, "杭州", DEPARTURE)
            calls = len(requests)
            later = DEPARTURE.replace(second=29)
            reused = await provider.route(mode, ORIGIN, DESTINATION, "杭州", later)
            assert len(requests) == calls
            assert datetime.fromisoformat(reused["departure_at"]) == later
            assert {**reused, "departure_at": first["departure_at"]} == first
            with database.connection() as connection:
                saved = json.loads(
                    connection.execute(
                        "SELECT payload_json FROM route_cache"
                    ).fetchone()[0]
                )
            assert saved == first
            next_minute = await provider.route(
                mode, ORIGIN, DESTINATION, "杭州", later + timedelta(minutes=1)
            )
            assert len(requests) == calls + 1
            assert next_minute["id"] != first["id"]

    asyncio.run(run())


def test_transit_citycode_is_verified_cached_and_counted_as_physical_call(database):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=district_payload()
            if request.url.path == "/v3/config/district"
            else transit_payload(),
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only", max_calls=4)
            provider = RouteProvider(amap, database)
            leg = await provider.route(
                "transit", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            assert leg["city_code"] == "0571"
            assert (
                leg["duration_seconds"] == 1200
            )  # Includes transfer walking/wait; do not add 400 + 900.
            assert leg["walking_distance_meters"] == 300
            assert leg["cost_cny"] == 2.5 and leg["cost_source"] == "source"
            assert len(leg["geometry"]) == 2
            assert [120.5, 30.6] not in [
                point for line in leg["geometry"] for point in line
            ]
            await provider.route(
                "transit", ORIGIN, DESTINATION, "杭州", DEPARTURE + timedelta(minutes=1)
            )
            assert len(amap.calls) == 3
            assert requests[0].url.params["subdistrict"] == "0"
            assert (
                requests[1].url.params["city1"]
                == requests[1].url.params["city2"]
                == "0571"
            )
            assert requests[1].url.params["date"] == "2030-10-15"
            assert requests[1].url.params["time"] == "09-30"
            assert requests[1].url.params["strategy"] == "0"

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        district_payload("宁波市"),
        district_payload(level="province"),
        district_payload(code="330100"),
        district_payload(code=[]),
        district_payload(level="district"),
        {"status": "1", "districts": []},
        {"status": "1", "districts": district_payload()["districts"] * 2},
    ],
)
def test_city_code_never_guesses_for_wrong_city_area_missing_or_ambiguous_result(
    payload,
):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            with pytest.raises(AmapError):
                await Amap(client, "test-only").city_code("杭州")

    asyncio.run(run())


def test_municipality_uses_actual_provider_citycode():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json=district_payload("北京市", "010", "province")
                )
            )
        ) as client:
            assert await Amap(client, "test-only").city_code("北京") == "010"

    asyncio.run(run())


def test_driving_endpoint_and_future_condition(database):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=walking_payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            leg = await RouteProvider(Amap(client, "test-only"), database).route(
                "driving", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            assert requests[0].url.path == "/v5/direction/driving"
            assert requests[0].url.params["strategy"] == "32"
            assert (
                "date" not in requests[0].url.params
                and "time" not in requests[0].url.params
            )
            assert leg["conditional"] is True
            assert any(
                condition["code"] == "future_traffic_unverified"
                for condition in leg["conditions"]
            )

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["bicycling", "electrobike"])
def test_shared_vehicle_route_never_claims_inventory_battery_return_or_price(
    database, mode
):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=walking_payload())
            )
        ) as client:
            leg = await RouteProvider(Amap(client, "test-only"), database).route(
                mode, ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            assert leg["conditional"] is True
            assert leg["cost_cny"] is None
            assert any(
                condition["code"] == "shared_vehicle_unverified"
                for condition in leg["conditions"]
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "infocode,status",
    [
        ("10012", "unavailable"),
        ("20802", "unavailable"),
        ("10021", "unknown"),
        ("UNKNOWN", "unknown"),
    ],
)
def test_provider_failures_keep_unknown_quantities_without_retry_or_secret(
    database, infocode, status
):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"status": "0", "infocode": infocode, "info": "web-secret"},
                )
            )
        ) as client:
            amap = Amap(client, "web-secret")
            provider = RouteProvider(amap, database)
            leg = await provider.route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            assert leg["status"] == status
            assert leg["duration_seconds"] is None and leg["distance_meters"] is None
            assert leg["geometry"] == [] and leg["geometry_complete"] is False
            assert "web-secret" not in json.dumps(leg)
            assert (
                await provider.route("walking", ORIGIN, DESTINATION, "杭州", DEPARTURE)
                == leg
            )
            assert len(amap.calls) == 1

    asyncio.run(run())


def test_budget_exhaustion_after_city_lookup_is_not_cached_as_a_route(database):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=district_payload())
            )
        ) as client:
            amap = Amap(client, "test-only", max_calls=1)
            with pytest.raises(AmapBudgetExceeded):
                await RouteProvider(amap, database).route(
                    "transit", ORIGIN, DESTINATION, "杭州", DEPARTURE
                )
            assert len(amap.calls) == 1

    asyncio.run(run())
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM route_cache").fetchone()[0] == 0


@pytest.mark.parametrize(
    "response",
    [{}, {"route": []}, {"route": {"paths": [{}]}}, {"route": {"paths": "bad"}}],
)
def test_malformed_route_is_unknown(response):
    leg = normalize_route("walking", response)
    assert leg["status"] == "unknown"
    assert leg["duration_seconds"] is None


def test_empty_route_is_unavailable():
    assert (
        normalize_route("walking", {"route": {"paths": []}})["status"] == "unavailable"
    )


@pytest.mark.parametrize("value", [True, -1, "nan", "inf", "", None, "1e308"])
def test_bad_distance_and_duration_do_not_become_zero(value):
    leg = normalize_route(
        "walking", walking_payload(distance=value, cost={"duration": value})
    )
    assert leg["status"] == "unknown"
    assert leg["duration_seconds"] is None and leg["distance_meters"] is None


def test_real_zero_values_are_preserved():
    leg = normalize_route(
        "walking", walking_payload(distance="0", cost={"duration": "0"})
    )
    assert leg["status"] == "available"
    assert leg["duration_seconds"] == leg["distance_meters"] == 0


@pytest.mark.parametrize(
    "polyline",
    ["", "120,30", "bad", "181,30;120,30", "120,nan;120,30", "120,30,0;120,30"],
)
def test_bad_geometry_retains_real_time_without_fake_endpoint_line(polyline):
    leg = normalize_route("walking", walking_payload(steps=[{"polyline": polyline}]))
    assert leg["status"] == "available"
    assert leg["duration_seconds"] == 600
    assert leg["geometry"] == [] and leg["geometry_complete"] is False


def test_missing_rail_geometry_is_not_replaced_by_a_line_between_stations():
    payload = transit_payload()
    payload["route"]["transits"][0]["segments"].append(
        {
            "walking": [],
            "railway": {
                "departure_stop": {"location": "120.1,30.1"},
                "arrival_stop": {"location": "120.8,30.8"},
            },
        }
    )
    leg = normalize_route("transit", payload)
    assert leg["geometry_complete"] is False
    assert [120.8, 30.8] not in [point for line in leg["geometry"] for point in line]
    assert leg["cost_cny"] is None


def test_missing_transit_walking_distance_stays_unknown():
    payload = transit_payload()
    del payload["route"]["transits"][0]["segments"][0]["walking"]["distance"]
    assert normalize_route("transit", payload)["walking_distance_meters"] is None


def test_v5_bus_steps_alternative_is_not_merged():
    payload = transit_payload()
    bus = payload["route"]["transits"][0]["segments"][0]["bus"]
    bus["steps"] = bus.pop("buslines")
    leg = normalize_route("transit", payload)
    assert leg["geometry_complete"] is True
    assert [120.5, 30.6] not in [point for line in leg["geometry"] for point in line]


def test_other_returned_paths_do_not_enter_selected_geometry():
    payload = walking_payload()
    payload["route"]["paths"].append(
        {
            "distance": "10",
            "cost": {"duration": "5"},
            "steps": [{"polyline": "121,31;122,32"}],
        }
    )
    leg = normalize_route("walking", payload)
    assert leg["duration_seconds"] == 600
    assert leg["geometry"] == [[ORIGIN, DESTINATION]]


def test_invalid_departure_never_requests_amap(database):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("invalid date reached API")
            )
        ) as client:
            with pytest.raises(ValueError):
                await RouteProvider(Amap(client, "test-only"), database).route(
                    "walking",
                    ORIGIN,
                    DESTINATION,
                    "杭州",
                    datetime(2030, 10, 15, 9, 30),
                )

    asyncio.run(run())


def test_transit_date_time_are_converted_to_china_timezone():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=transit_payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await Amap(client, "test-only").route(
                "transit",
                "120,30",
                "121,31",
                "0571",
                departure_at=datetime(2030, 10, 14, 23, 30, tzinfo=timezone.utc),
            )

    asyncio.run(run())
    assert requests[0].url.params["date"] == "2030-10-15"
    assert requests[0].url.params["time"] == "07-30"


def test_expired_route_cache_is_refetched_and_snapshot_id_changes(
    database, monkeypatch
):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=walking_payload())
            )
        ) as client:
            amap = Amap(client, "test-only")
            provider = RouteProvider(amap, database)
            first = await provider.route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            monkeypatch.setattr(
                "easytrip.storage.database.now",
                lambda: (
                    datetime.now(timezone.utc) + timedelta(minutes=31)
                ).isoformat(),
            )
            second = await provider.route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            assert first["id"] != second["id"]
            assert len(amap.calls) == 2

    asyncio.run(run())


def test_city_cache_expires_after_seven_days(database, monkeypatch):
    database.save_city_code("杭州市", "0571")
    assert database.cached_city_code("杭州") == "0571"
    monkeypatch.setattr(
        "easytrip.storage.database.now",
        lambda: (datetime.now(timezone.utc) + timedelta(days=8)).isoformat(),
    )
    assert database.cached_city_code("杭州") is None


@pytest.mark.parametrize(
    "value", [[True, 30], [181, 30], [120, float("nan")], [120], "120,30"]
)
def test_invalid_endpoint_never_requests_amap(database, value):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("invalid endpoint reached API")
            )
        ) as client:
            with pytest.raises(ValueError):
                await RouteProvider(Amap(client, "test-only"), database).route(
                    "walking", value, DESTINATION, "杭州", DEPARTURE
                )

    asyncio.run(run())


def test_route_cache_rejects_extra_fields(database):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=walking_payload())
            )
        ) as client:
            leg = await RouteProvider(Amap(client, "test-only"), database).route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            with pytest.raises(ValidationError):
                RouteLeg.model_validate({**leg, "api_key": "secret"})

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        {"duration_seconds": None},
        {"distance_meters": None},
        {"geometry": [], "geometry_complete": True},
        {"cost_source": "source", "cost_cny": None},
    ],
)
def test_route_cache_rejects_inconsistent_facts(database, fields):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=walking_payload())
            )
        ) as client:
            leg = await RouteProvider(Amap(client, "test-only"), database).route(
                "walking", ORIGIN, DESTINATION, "杭州", DEPARTURE
            )
            with pytest.raises(ValidationError):
                RouteLeg.model_validate({**leg, **fields})

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "infocode", "http", "cancel"])
def test_amap_after_request_receives_fixed_path_and_preserves_failure(failure):
    events = []

    async def after(path, payload, error):
        events.append((path, payload, error))

    def handler(request):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "http":
            return httpx.Response(500)
        if failure == "infocode":
            return httpx.Response(200, json={"status": "0", "infocode": "10012"})
        return httpx.Response(200, json=walking_payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            amap = Amap(client, "test-only", after_request=after)
            if failure:
                with pytest.raises(
                    asyncio.CancelledError if failure == "cancel" else AmapError
                ):
                    await amap.route("walking", "120.1,30.2", "120.2,30.3", None)
            else:
                await amap.route("walking", "120.1,30.2", "120.2,30.3", None)
            assert len(events) == 1
            assert events[0][0] == "v5/direction/walking"
            assert "test-only" not in events[0][0]
            assert (events[0][1] is not None) == (failure is None)
            assert (events[0][2] is None) == (failure is None)

    asyncio.run(run())


def test_failure_in_cancel_ledger_hook_does_not_swallow_cancellation():
    async def after(path, payload, error):
        raise RuntimeError("ledger write failed")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(asyncio.CancelledError())
            )
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await Amap(client, "test-only", after_request=after).route(
                    "walking", "120,30", "121,31", None
                )

    asyncio.run(run())
