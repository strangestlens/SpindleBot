-- Retire an older overwrite-capable COPY when a later restoration approval
-- protects the same destination path. Preserve the original plan and approval.
-- The timestamp remains authoritative if the replacement's history is purged.
ALTER TABLE pending_action ADD COLUMN superseded_utc INTEGER;
ALTER TABLE pending_action ADD COLUMN superseded_by_action_id INTEGER
    REFERENCES pending_action(id) ON DELETE SET NULL;
-- A run's timestamp can predate the actual acknowledgement. Allocate this
-- ordinal atomically at acknowledgement; NULL means legacy/unknown ordering.
ALTER TABLE pending_action ADD COLUMN approval_order INTEGER;
ALTER TABLE location ADD COLUMN path_case_sensitive INTEGER
    CHECK (path_case_sensitive IN (0, 1));
ALTER TABLE pending_action ADD COLUMN dest_path_key TEXT;
UPDATE pending_action SET dest_path_key = rel_path;
-- Existing approvals precede all new events; their relative order is unknown,
-- so they share an explicit legacy cohort and stricter safety wins ties.
UPDATE pending_action SET approval_order = 0 WHERE acknowledged = 1;

CREATE INDEX idx_action_approval_order ON pending_action(approval_order);
CREATE INDEX idx_action_destination_key ON pending_action(dest_location_id, dest_path_key);

CREATE INDEX idx_action_restoration_approval
    ON pending_action(dest_location_id, dest_path_key, approval_order, id)
    WHERE acknowledged = 1 AND dest_must_be_absent = 1;

-- Derived keys must not survive a root or filesystem-policy change. The raw
-- approved paths remain frozen; current code rebuilds only invalidated keys.
CREATE TRIGGER invalidate_location_root_keys AFTER UPDATE OF root_path ON location
WHEN OLD.root_path IS NOT NEW.root_path
BEGIN
    UPDATE location SET path_case_sensitive = NULL WHERE id = NEW.id;
    UPDATE pending_action SET dest_path_key = NULL WHERE dest_location_id = NEW.id;
END;
CREATE TRIGGER invalidate_location_case_keys AFTER UPDATE OF path_case_sensitive ON location
WHEN OLD.path_case_sensitive IS NOT NEW.path_case_sensitive
BEGIN
    UPDATE pending_action SET dest_path_key = NULL WHERE dest_location_id = NEW.id;
END;
