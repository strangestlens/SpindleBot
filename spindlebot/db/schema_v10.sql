-- Tie a positively confirmed missing path to the exact inventory that checked
-- it. observed_utc alone cannot distinguish two scans within the same second.
-- Legacy rows remain unknown; no absence is inferred during migration.
ALTER TABLE audio_presence ADD COLUMN absence_scan_id INTEGER
    REFERENCES location_scan(id) ON DELETE SET NULL;
ALTER TABLE sidecar_presence ADD COLUMN absence_scan_id INTEGER
    REFERENCES location_scan(id) ON DELETE SET NULL;

-- A restoration uses the recorded missing TARGET path, which may differ from
-- its source. The no-clobber requirement stays frozen even if scans are purged.
ALTER TABLE pending_action ADD COLUMN source_rel_path TEXT;
ALTER TABLE pending_action ADD COLUMN dest_must_be_absent INTEGER NOT NULL DEFAULT 0;
