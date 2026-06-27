-- SpindleBot schema — version 4 (Phase 2): run log + the pending-action queue.
--
-- The reconciler is a PLANNER: it never touches bytes, it only writes
-- pending_action rows describing proposed work (copy / delete / update_presence
-- / resolve_conflict). A human acknowledges them at review time; the Phase-3
-- executor is the only thing that acts, and only on acknowledged rows. Every
-- import/sync/inventory/reconcile pass is recorded as a `run`.

CREATE TABLE run (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,            -- 'import'|'sync'|'inventory'|'reconcile'
    location_id   INTEGER REFERENCES location(id) ON DELETE SET NULL,  -- location concerned, if any
    started_utc   INTEGER NOT NULL,
    finished_utc  INTEGER,
    status        TEXT NOT NULL DEFAULT 'running',  -- 'running'|'ok'|'interrupted'|'error'
    note          TEXT
);
CREATE INDEX idx_run_kind ON run(kind, started_utc);

-- A proposed, not-yet-executed action. content_kind selects the table for the
-- polymorphic content_id (audio_content / sidecar_content), so — like
-- sidecar_content.parent_id — there is intentionally no SQL FK on content_id.
-- Nothing destructive happens until acknowledged=1; executed_utc is set by the
-- Phase-3 executor.
CREATE TABLE pending_action (
    id                  INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    action_kind         TEXT NOT NULL,      -- 'copy'|'delete'|'update_presence'|'resolve_conflict'
    content_kind        TEXT NOT NULL,      -- 'audio'|'sidecar'
    content_id          INTEGER NOT NULL,   -- polymorphic: audio_content.id / sidecar_content.id
    source_location_id  INTEGER REFERENCES location(id) ON DELETE SET NULL,  -- copy source
    dest_location_id    INTEGER REFERENCES location(id) ON DELETE SET NULL,  -- location acted on
    rel_path            TEXT,               -- proposed dest path / path being marked absent
    reason              TEXT,               -- human-readable why
    acknowledged        INTEGER NOT NULL DEFAULT 0,
    acknowledged_utc    INTEGER,
    executed_utc        INTEGER,            -- set by the executor (Phase 3)
    created_utc         INTEGER NOT NULL
);
CREATE INDEX idx_pending_action_run ON pending_action(run_id);
CREATE INDEX idx_pending_action_ack ON pending_action(acknowledged, executed_utc);
