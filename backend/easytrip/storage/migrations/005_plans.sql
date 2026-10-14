ALTER TABLE trips ADD COLUMN active_plan_id TEXT;
CREATE TABLE plans (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    request_version INTEGER NOT NULL CHECK (request_version >= 1),
    input_hash TEXT NOT NULL,
    parent_plan_id TEXT REFERENCES plans(id) ON DELETE SET NULL,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    UNIQUE (trip_id, operation_id),
    UNIQUE (trip_id, version)
);
CREATE INDEX plans_trip ON plans(trip_id, version DESC);
CREATE TABLE plan_adoptions (
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    request_version INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(trip_id,operation_id)
);
