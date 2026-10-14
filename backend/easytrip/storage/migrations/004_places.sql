CREATE TABLE place_match_sessions (
    run_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    token TEXT NOT NULL,
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE
);

CREATE TABLE candidate_matches (
    candidate_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id, run_id) REFERENCES candidates(id, run_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE
);
CREATE INDEX candidate_matches_owner ON candidate_matches(trip_id, request_version, run_id);

CREATE TABLE poi_search_cache (
    query TEXT NOT NULL,
    city TEXT NOT NULL,
    api_version TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    read_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(query, city, api_version)
);

CREATE TABLE origin_searches (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    request_version INTEGER NOT NULL,
    request_origin TEXT,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    read_at TEXT NOT NULL
);
