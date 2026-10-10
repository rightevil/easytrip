import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.integrations.amap import Amap, AmapError
from backend.app.main import create_app


def test_web_api_parameters_and_raw_response():
    requests = []
    payload = {
        "status": "1",
        "infocode": "10000",
        "pois": [{"id": "B00001", "location": "120.1,30.2"}],
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ) as client:
            amap = Amap(client, "web-secret")
            assert await amap.search("西湖") == payload
            await amap.detail("B00001")
            for mode in ("walking", "transit", "bicycling", "electrobike"):
                await amap.route(mode, "120.1,30.2", "120.2,30.3", "0571")
            assert len(amap.calls) == 6
            assert amap.calls[0] == "v5/place/text"

    asyncio.run(run())
    assert requests[0].url.params["city_limit"] == "true"
    assert requests[0].url.params["region"] == "杭州"
    assert requests[1].url.params["id"] == "B00001"
    assert requests[3].url.path == "/v5/direction/transit/integrated"
    assert requests[3].url.params["city1"] == requests[3].url.params["city2"] == "0571"
    for request in requests[2:]:
        assert request.url.params["show_fields"] == "cost,polyline"
        assert request.url.params["origin"] == "120.100000,30.200000"
        assert request.url.params["destination"] == "120.200000,30.300000"
    assert all(request.url.params["key"] == "web-secret" for request in requests)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "0", "infocode": "10001", "info": "secret-url"},
        {"status": "0", "infocode": "secret-url"},
        [],
    ],
)
def test_amap_failure_does_not_expose_upstream_data(payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ) as client:
            with pytest.raises(AmapError) as error:
                await Amap(client, "web-secret").search("西湖")
            assert "infocode=" in str(error.value)
            assert "secret" not in str(error.value)
            assert "https://" not in str(error.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "location", ["120", "120,30,0", "181,30", "120,91", "nan,30", "120,inf", "bad,30"]
)
def test_invalid_route_location_never_requests_upstream(location):
    async def run():
        def handler(request):
            pytest.fail("Invalid coordinates must not reach AMap")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await Amap(client, "web-secret").route(
                    "walking", location, "120,30", "0571"
                )

    asyncio.run(run())


def test_map_credentials_and_proxy_are_restricted(tmp_path, monkeypatch):
    from backend.app import main

    settings = Settings(
        amap_web_key="web-secret",
        amap_js_key="public-js-key",
        amap_js_secure_key="secure-secret",
        secrets={"web-secret", "secure-secret"},
    )
    (tmp_path / "map-data.json").write_text(
        json.dumps(
            {
                "origin": {"name": "起点", "location": "120,30", "key": "web-secret"},
                "candidates": [],
                "routes": [],
                "coordinate_system": "GCJ-02",
                "private": "secure-secret",
            }
        ),
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text="secure-secret web-secret", headers={"content-type": "text/plain"}
        )

    monkeypatch.setattr(
        main,
        "direct_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ),
    )
    with TestClient(
        create_app(settings, tmp_path), base_url="http://127.0.0.1:8000"
    ) as client:
        config = client.get("/api/map-config").json()
        assert config == {
            "key": "public-js-key",
            "serviceHost": "http://127.0.0.1:8000/_AMapService",
        }
        response = client.get("/api/map-data")
        assert response.status_code == 200
        assert (
            "web-secret" not in response.text and "secure-secret" not in response.text
        )
        response = client.get("/_AMapService/v4/maps?key=attacker&jscode=attacker")
        assert response.status_code == 200
        assert (
            "secure-secret" not in response.text and "web-secret" not in response.text
        )
        assert requests[-1].url.host == "restapi.amap.com"
        assert requests[-1].url.params["key"] == "public-js-key"
        assert requests[-1].url.params["jscode"] == "secure-secret"
        assert client.get("/_AMapService/https://example.com").status_code == 404
        assert client.get("/_AMapService/v5/place/text").status_code == 404
        assert (
            client.get("/api/map-config", headers={"Host": "evil.example"}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/map-config", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            "access-control-allow-origin" not in client.get("/api/map-config").headers
        )


def test_map_data_reports_missing_or_unknown_coordinates(tmp_path):
    settings = Settings(amap_js_key="public", amap_js_secure_key="secret")
    with TestClient(
        create_app(settings, tmp_path), base_url="http://localhost"
    ) as client:
        assert client.get("/api/map-data").status_code == 404
        (tmp_path / "map-data.json").write_text(
            '{"coordinate_system":"WGS-84"}', encoding="utf-8"
        )
        assert client.get("/api/map-data").status_code == 500
