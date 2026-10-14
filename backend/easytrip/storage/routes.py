"""Bounded lifetime caches for public route facts and verified city codes."""

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from ..domain.routes import RouteLeg

ROUTE_TTL = timedelta(minutes=30)
CITY_TTL = timedelta(days=7)


def normalized_city(city):
    if not isinstance(city, str) or not city.strip() or len(city) > 200:
        raise ValueError("路线查询必须指定城市")
    return "".join(unicodedata.normalize("NFKC", city).casefold().split()).removesuffix(
        "市"
    )


def _key(key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("路线缓存键必须是查询摘要")
    return key


class RouteStore:
    def cached_route(self, key):
        from .database import now

        with self.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM route_cache WHERE cache_key=? AND expires_at>?",
                (_key(key), now()),
            ).fetchone()
        if row is None:
            return None
        return RouteLeg.model_validate(json.loads(row["payload_json"])).model_dump(
            mode="json"
        )

    def save_route(self, key, leg):
        from .database import encode

        result = RouteLeg.model_validate(leg)
        timestamp = result.read_at.astimezone(timezone.utc)
        result.valid_until = min(
            result.valid_until.astimezone(timezone.utc), timestamp + ROUTE_TTL
        )
        if result.valid_until <= timestamp:
            raise ValueError("路线缓存有效期必须晚于取得时间")
        payload = result.model_dump(mode="json")
        with self.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO route_cache(cache_key,payload_json,read_at,expires_at) VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json,read_at=excluded.read_at,expires_at=excluded.expires_at",
                (
                    _key(key),
                    encode(payload),
                    timestamp.isoformat(),
                    result.valid_until.isoformat(),
                ),
            )
        return payload

    def cached_city_code(self, city):
        from .database import now

        with self.connection() as connection:
            row = connection.execute(
                "SELECT city_code FROM city_code_cache WHERE city=? AND expires_at>?",
                (normalized_city(city), now()),
            ).fetchone()
        return row["city_code"] if row else None

    def save_city_code(self, city, code):
        if not isinstance(code, str) or not re.fullmatch(r"\d{3,4}", code):
            raise ValueError("公交城市编码无效")
        timestamp = datetime.now(timezone.utc)
        with self.connection(write=True) as connection:
            connection.execute(
                "INSERT INTO city_code_cache(city,city_code,read_at,expires_at) VALUES(?,?,?,?) ON CONFLICT(city) DO UPDATE SET city_code=excluded.city_code,read_at=excluded.read_at,expires_at=excluded.expires_at",
                (
                    normalized_city(city),
                    code,
                    timestamp.isoformat(),
                    (timestamp + CITY_TTL).isoformat(),
                ),
            )
        return code
