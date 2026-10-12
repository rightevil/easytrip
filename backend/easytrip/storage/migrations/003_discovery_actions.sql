CREATE TABLE run_actions (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    action_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('login','search','detail','model')),
    status TEXT NOT NULL CHECK (status IN ('started','completed','failed','unknown')),
    params_json TEXT NOT NULL CHECK (json_valid(params_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    usage_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(usage_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, action_key)
);
CREATE INDEX run_actions_run ON run_actions(run_id, created_at, id);

-- Restricted transport handles never form part of public source text or run params.
CREATE TABLE source_handles (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    handle_json TEXT NOT NULL CHECK (json_valid(handle_json)),
    PRIMARY KEY (run_id, external_id)
);
CREATE TABLE run_items (
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    item_key TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, item_key)
);

ALTER TABLE runs ADD COLUMN stage_started_at TEXT;
ALTER TABLE messages ADD COLUMN discovery_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL;

CREATE TABLE direction_adoptions (
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    PRIMARY KEY (trip_id, operation_id)
);
