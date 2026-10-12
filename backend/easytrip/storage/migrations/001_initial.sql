CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    create_operation_id TEXT NOT NULL UNIQUE,
    create_payload_json TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'archived')),
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    request_version INTEGER NOT NULL DEFAULT 1 CHECK (request_version >= 1),
    workspace_json TEXT NOT NULL CHECK (json_valid(workspace_json)),
    workspace_version INTEGER NOT NULL DEFAULT 1 CHECK (workspace_version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role = 'user'),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status = 'pending'),
    created_at TEXT NOT NULL,
    UNIQUE (trip_id, operation_id)
);
CREATE INDEX IF NOT EXISTS messages_trip ON messages(trip_id, created_at, id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    request_version INTEGER NOT NULL CHECK (request_version >= 1),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    params_json TEXT NOT NULL CHECK (json_valid(params_json)),
    budget_json TEXT NOT NULL CHECK (json_valid(budget_json)),
    checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(checkpoint_json)),
    usage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(usage_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    error_code TEXT,
    stop_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (trip_id, operation_id)
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, created_at, id);
CREATE INDEX IF NOT EXISTS runs_trip ON runs(trip_id, created_at, id);
