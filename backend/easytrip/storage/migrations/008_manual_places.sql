CREATE TABLE manual_place_searches (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    request_version INTEGER NOT NULL,
    query TEXT NOT NULL,
    city TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    read_at TEXT NOT NULL
);

CREATE TABLE manual_candidates (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    request_version INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    operation_json TEXT NOT NULL CHECK(json_valid(operation_json)),
    category TEXT NOT NULL CHECK(category IN ('eat','play','stay')),
    poi_json TEXT NOT NULL CHECK(json_valid(poi_json)),
    created_at TEXT NOT NULL,
    UNIQUE(trip_id, operation_id)
);
CREATE INDEX manual_candidates_owner ON manual_candidates(trip_id);
