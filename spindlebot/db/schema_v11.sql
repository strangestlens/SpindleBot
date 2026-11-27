-- Retire an older overwrite-capable COPY when a later restoration approval
-- protects the same destination path. Preserve the original plan and approval.
-- The timestamp remains authoritative if the replacement's history is purged.
ALTER TABLE pending_action ADD COLUMN superseded_utc INTEGER;
ALTER TABLE pending_action ADD COLUMN superseded_by_action_id INTEGER
    REFERENCES pending_action(id) ON DELETE SET NULL;

CREATE INDEX idx_action_restoration_approval
    ON pending_action(dest_location_id, rel_path, id)
    WHERE acknowledged = 1 AND dest_must_be_absent = 1;
