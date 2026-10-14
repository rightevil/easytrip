CREATE TABLE planning_replacements (
    trip_id TEXT NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (trip_id, operation_id)
);
