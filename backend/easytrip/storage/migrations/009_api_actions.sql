-- Physical API requests outside task runs, without queries, credentials or results.
CREATE TABLE api_actions (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    request_version INTEGER NOT NULL CHECK (request_version >= 1),
    provider TEXT NOT NULL CHECK (provider IN ('amap','rail')),
    operation TEXT NOT NULL CHECK (operation IN ('poi_search','poi_detail','get-station-code-by-names','get-tickets')),
    cached INTEGER NOT NULL DEFAULT 0 CHECK (cached IN (0,1)),
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed','unknown')),
    elapsed_seconds REAL CHECK (elapsed_seconds IS NULL OR elapsed_seconds >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX api_actions_trip ON api_actions(trip_id, created_at, id);
