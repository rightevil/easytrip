CREATE TABLE route_cache (
    cache_key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    read_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE city_code_cache (
    city TEXT PRIMARY KEY,
    city_code TEXT NOT NULL,
    read_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
