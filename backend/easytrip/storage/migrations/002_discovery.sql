CREATE UNIQUE INDEX IF NOT EXISTS runs_owner_version ON runs(id, trip_id, request_version);

CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'comment')),
    external_id TEXT,
    dedupe_key TEXT NOT NULL,
    dedupe_confidence TEXT NOT NULL CHECK (dedupe_confidence IN ('high', 'low')),
    parent_source_id TEXT,
    latest_snapshot_json TEXT NOT NULL CHECK (json_valid(latest_snapshot_json)),
    first_read_at TEXT NOT NULL,
    last_read_at TEXT NOT NULL,
    UNIQUE(trip_id, platform, kind, dedupe_key),
    UNIQUE(id, trip_id),
    FOREIGN KEY(parent_source_id, trip_id) REFERENCES sources(id, trip_id) ON DELETE CASCADE
);

CREATE TABLE source_snapshots (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    read_at TEXT NOT NULL,
    PRIMARY KEY(run_id, source_id),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE,
    FOREIGN KEY(source_id, trip_id) REFERENCES sources(id, trip_id) ON DELETE CASCADE
);

CREATE TABLE run_sources (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    query TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('discovery', 'main', 'meal_gap', 'lodging_gap')),
    PRIMARY KEY(run_id, source_id, query, purpose),
    FOREIGN KEY(run_id, source_id) REFERENCES source_snapshots(run_id, source_id) ON DELETE CASCADE
);

CREATE TABLE image_observations (
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    image_index INTEGER NOT NULL CHECK (image_index >= 1),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY(run_id, source_id, image_index),
    FOREIGN KEY(run_id, source_id) REFERENCES source_snapshots(run_id, source_id) ON DELETE CASCADE
);

CREATE TABLE place_mentions (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY(run_id, id),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE,
    FOREIGN KEY(run_id, source_id) REFERENCES source_snapshots(run_id, source_id) ON DELETE CASCADE
);

CREATE TABLE activities (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    mention_id TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY(run_id, id),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE,
    FOREIGN KEY(run_id, source_id) REFERENCES source_snapshots(run_id, source_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id, mention_id) REFERENCES place_mentions(run_id, id) ON DELETE CASCADE
);

CREATE TABLE evidence (
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    mention_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY(run_id, id),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE,
    FOREIGN KEY(run_id, source_id) REFERENCES source_snapshots(run_id, source_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id, mention_id) REFERENCES place_mentions(run_id, id) ON DELETE CASCADE
);

CREATE TABLE candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('eat', 'play', 'stay')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    UNIQUE(id, run_id),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE
);
CREATE INDEX candidates_owner ON candidates(trip_id, request_version, run_id, category);

CREATE TABLE candidate_evidence (
    candidate_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY(candidate_id, evidence_id),
    FOREIGN KEY(candidate_id, run_id) REFERENCES candidates(id, run_id) ON DELETE CASCADE,
    FOREIGN KEY(run_id, evidence_id) REFERENCES evidence(run_id, id) ON DELETE CASCADE
);

CREATE TABLE gaps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    request_version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('meal', 'lodging')),
    status TEXT NOT NULL CHECK (status IN ('open', 'undetermined', 'filled', 'unfilled')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    FOREIGN KEY(run_id, trip_id, request_version) REFERENCES runs(id, trip_id, request_version) ON DELETE CASCADE
);
CREATE INDEX gaps_owner ON gaps(trip_id, request_version, run_id);
