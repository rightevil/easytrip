"""Small, direct AMap Web API client for P0 probes."""

import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx


class AmapError(RuntimeError):
    def __init__(self, infocode="UNKNOWN"):
        self.infocode = (
            str(infocode) if re.fullmatch(r"\d{5}", str(infocode)) else "UNKNOWN"
        )
        super().__init__(f"高德服务调用失败（infocode={self.infocode}）")


class AmapBudgetExceeded(ValueError):
    pass


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
    def __init__(
        self,
        client: httpx.AsyncClient,
        key: str,
        *,
        max_calls: int | None = None,
        before_request=None,
        after_request=None,
    ):
        if max_calls is not None and (type(max_calls) is not int or max_calls < 1):
            raise ValueError("高德调用预算必须为正整数")
        self.client = client
        self.key = key
        self.max_calls = max_calls
        self.before_request = before_request
        self.after_request = after_request
        self.calls: list[str] = []

    async def _get(self, path: str, params: dict) -> dict:
        if not self.key:
            raise ValueError("高德 Web 服务 Key 未配置")
        if self.max_calls is not None and len(self.calls) >= self.max_calls:
            raise AmapBudgetExceeded("高德调用预算已用尽")
        if self.before_request is not None:
            await self.before_request()
            if self.max_calls is not None and len(self.calls) >= self.max_calls:
                raise AmapBudgetExceeded("高德调用预算已用尽")
        self.calls.append(path)
        try:
            try:
                response = await self.client.get(
                    f"https://restapi.amap.com/{path}",
                    params={**params, "key": self.key},
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
        except BaseException as error:
            if self.after_request is not None:
                try:
                    await self.after_request(path, None, error)
                finally:
                    raise error
            raise
        if self.after_request is not None:
            await self.after_request(path, payload, None)
        return payload

    async def search(
        self, keyword: str, city: str = "杭州", *, page_size: int = 5, page_num: int = 1
    ) -> dict:
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 80:
            raise ValueError("POI 关键词需为 1–80 个字符")
        if not isinstance(city, str) or not city.strip() or len(city) > 200:
            raise ValueError("POI 查询必须指定城市")
        if type(page_size) is not int or not 1 <= page_size <= 25:
            raise ValueError("POI page_size 必须为 1–25")
        if type(page_num) is not int or not 1 <= page_num <= 200:
            raise ValueError("POI page_num 必须为 1–200")
        return await self._get(
            "v5/place/text",
            {
                "keywords": keyword,
                "region": city,
                "city_limit": "true",
                "page_size": page_size,
                "page_num": page_num,
                "show_fields": "business,navi",
            },
        )

    async def search_candidates(
        self, keyword: str, city: str, *, max_pages: int = 2
    ) -> dict:
        """Bounded P3 search; v5 count describes this page, not a total count."""
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise ValueError("POI 搜索页数必须为 1–10")
        pois, seen = [], set()
        complete = False
        for page in range(1, max_pages + 1):
            response = await self.search(keyword, city, page_size=20, page_num=page)
            rows = response.get("pois")
            if not isinstance(rows, list) or len(rows) > 20:
                raise AmapError()
            valid_page = str(response.get("count")) == str(len(rows))
            for row in rows:
                identifier = row.get("id") if isinstance(row, dict) else None
                if not isinstance(identifier, str) or not identifier.strip():
                    valid_page = False
                    continue
                identifier = identifier.strip()
                if identifier in seen:
                    valid_page = False
                    continue
                seen.add(identifier)
                pois.append(row)
            if not valid_page:
                break
            if len(rows) < 20:
                complete = len(pois) < 200
                break
        return {
            "status": "1",
            "pois": pois,
            "search_complete": complete,
            "api_version": "v5",
        }

    async def detail(self, poi_id: str) -> dict:
        if not isinstance(poi_id, str) or not poi_id.strip():
            raise ValueError("POI ID 不能为空")
        return await self._get(
            "v5/place/detail", {"id": poi_id, "show_fields": "business,navi"}
        )

    async def city_code(self, city: str) -> str:
        if not isinstance(city, str) or not city.strip() or len(city) > 200:
            raise ValueError("路线查询必须指定城市")
        response = await self._get(
            "v3/config/district", {"keywords": city.strip(), "subdistrict": 0}
        )
        rows = response.get("districts")

        def name(value):
            return "".join(unicodedata.normalize("NFKC", value).split()).removesuffix(
                "市"
            )

        if not isinstance(rows, list) or len(rows) != 1:
            raise AmapError()
        row = rows[0]
        code = row.get("citycode") if isinstance(row, dict) else None
        level = row.get("level") if isinstance(row, dict) else None
        city_level = level == "city" or (
            level == "province" and name(city) in {"北京", "天津", "上海", "重庆"}
        )
        if (
            not isinstance(code, str)
            or not re.fullmatch(r"\d{3,4}", code)
            or not city_level
            or not isinstance(row.get("name"), str)
            or name(row["name"]) != name(city)
        ):
            raise AmapError()
        return code

    async def route(
        self,
        mode: str,
        origin: str,
        destination: str,
        city_code: str | None,
        *,
        departure_at: datetime | None = None,
        strategy: int | None = None,
    ) -> dict:
        endpoints = {
            "walking": "walking",
            "transit": "transit/integrated",
            "bicycling": "bicycling",
            "electrobike": "electrobike",
            "driving": "driving",
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
            if departure_at is not None:
                if (
                    not isinstance(departure_at, datetime)
                    or departure_at.utcoffset() is None
                ):
                    raise ValueError("公交出发时间必须带时区")
                local = departure_at.astimezone(timezone(timedelta(hours=8)))
                params.update(
                    date=local.strftime("%Y-%m-%d"), time=local.strftime("%H-%M")
                )
        if strategy is not None:
            if (
                type(strategy) is not int
                or (mode == "transit" and not 0 <= strategy <= 8)
                or (mode == "driving" and strategy not in (0, 1, 2, *range(32, 46)))
                or mode not in ("transit", "driving")
            ):
                raise ValueError("路线策略与交通方式不匹配")
            params["strategy"] = strategy
        return await self._get(f"v5/direction/{endpoints[mode]}", params)
