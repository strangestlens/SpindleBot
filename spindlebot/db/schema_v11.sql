-- Retire an older overwrite-capable COPY when a later restoration approval
-- protects the same destination path. Preserve the original plan and approval.
-- The timestamp remains authoritative if the replacement's history is purged.
ALTER TABLE pending_action ADD COLUMN superseded_utc INTEGER;
ALTER TABLE pending_action ADD COLUMN superseded_by_action_id INTEGER
    REFERENCES pending_action(id) ON DELETE SET NULL;
-- A run's timestamp can predate the actual acknowledgement. Allocate this
-- ordinal atomically at acknowledgement; NULL means legacy/unknown ordering.
ALTER TABLE pending_action ADD COLUMN approval_order INTEGER;
-- Existing approvals precede all new events; their relative order is unknown,
-- so they share an explicit legacy cohort and stricter safety wins ties.
UPDATE pending_action SET approval_order = 0 WHERE acknowledged = 1;

CREATE INDEX idx_action_approval_order ON pending_action(approval_order);

CREATE INDEX idx_action_restoration_approval
    ON pending_action(dest_location_id, rel_path, approval_order, id)
    WHERE acknowledged = 1 AND dest_must_be_absent = 1;
