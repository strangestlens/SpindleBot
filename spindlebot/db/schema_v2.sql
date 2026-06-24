-- SpindleBot schema — version 2 (Phase 1): location resolution + scan log.
--
-- root_path is the location's last-known content root (the directory we scan and
-- where its marker file lives). For a library it is the local Pending area; for
-- a drive/DAP it is the mounted path. Resolution verifies the marker before
-- trusting it (see services/volumes.py).

ALTER TABLE location ADD COLUMN root_path TEXT;

-- One row per inventory/reconcile pass over a location, so "last scanned" and
-- interrupted-scan recovery are answerable.
CREATE TABLE location_scan (
    id            INTEGER PRIMARY KEY,
    location_id   INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
    started_utc   INTEGER NOT NULL,
    finished_utc  INTEGER,
    files_seen    INTEGER,
    status        TEXT NOT NULL DEFAULT 'running'   -- 'running'|'ok'|'interrupted'|'error'
);
CREATE INDEX idx_location_scan_loc ON location_scan(location_id);
