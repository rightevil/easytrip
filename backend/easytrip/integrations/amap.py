"""Small, direct AMap Web API client for P0 probes."""

import math
import re

import httpx


class AmapError(RuntimeError):
    def __init__(self, infocode="UNKNOWN"):
        self.infocode = (
            str(infocode) if re.fullmatch(r"\d{5}", str(infocode)) else "UNKNOWN"
        )
        super().__init__(f"高德服务调用失败（infocode={self.infocode}）")


def _location(value: str) -> str:
    try:
        longitude, latitude = map(float, value.split(","))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("坐标必须是经度,纬度") from None
    if not (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    ):
        raise ValueError("坐标超出经纬度范围")
    return f"{longitude:.6f},{latitude:.6f}"


class Amap:
    def __init__(self, client: httpx.AsyncClient, key: str):
        self.client = client
        self.key = key
        self.calls: list[str] = []

    async def _get(self, path: str, params: dict) -> dict:
        if not self.key:
            raise ValueError("高德 Web 服务 Key 未配置")
        self.calls.append(path)
        try:
            response = await self.client.get(
                f"https://restapi.amap.com/{path}", params={**params, "key": self.key}
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise AmapError() from None
        if not isinstance(payload, dict) or payload.get("status") != "1":
            raise AmapError(
                payload.get("infocode", "UNKNOWN")
                if isinstance(payload, dict)
                else "UNKNOWN"
            )
        return payload

    async def search(self, keyword: str, city: str = "杭州") -> dict:
        if not keyword.strip() or len(keyword) > 80:
            raise ValueError("POI 关键词需为 1–80 个字符")
        return await self._get(
            "v5/place/text",
            {
                "keywords": keyword,
                "region": city,
                "city_limit": "true",
                "page_size": 5,
                "show_fields": "business,navi",
            },
        )

    async def detail(self, poi_id: str) -> dict:
        if not poi_id.strip():
            raise ValueError("POI ID 不能为空")
        return await self._get(
            "v5/place/detail", {"id": poi_id, "show_fields": "business,navi"}
        )

    async def route(
        self, mode: str, origin: str, destination: str, city_code: str
    ) -> dict:
        endpoints = {
            "walking": "walking",
            "transit": "transit/integrated",
            "bicycling": "bicycling",
            "electrobike": "electrobike",
        }
        if mode not in endpoints:
            raise ValueError("不支持的交通方式")
        params = {
            "origin": _location(origin),
            "destination": _location(destination),
            "show_fields": "cost,polyline",
        }
        if mode == "transit":
            if not isinstance(city_code, str) or not re.fullmatch(
                r"\d{3,4}", city_code
            ):
                raise ValueError("公交 city_code 必须是高德城市编码，例如 0571")
            params.update(city1=city_code, city2=city_code)
        return await self._get(f"v5/direction/{endpoints[mode]}", params)
