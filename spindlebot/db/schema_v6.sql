-- SpindleBot schema — version 6 (feat/inventory-incremental-rescan): file mtime.
--
-- Adds an mtime column to both presence tables so inventory can skip re-hashing
-- files whose (rel_path, byte_size, mtime) are unchanged since the last scan —
-- reusing the recorded identity and per-copy file_sha256 instead of recomputing
-- them. mtime is stored as the file's st_mtime_ns (nanoseconds since epoch) so
-- the comparison is exact and filesystem-independent of float rounding.
--
-- Nullable with no default: existing rows predate mtime capture, so their mtime
-- is unknown (NULL) and a NULL never matches an observed mtime — meaning any
-- pre-v6 row is treated as changed and re-hashed once, then carries mtime after.

ALTER TABLE audio_presence ADD COLUMN mtime INTEGER;
ALTER TABLE sidecar_presence ADD COLUMN mtime INTEGER;

-- Inventory looks a file up by (location_id, rel_path) before it knows the
-- content id — once per scanned file. Without an index that's a table scan per
-- file, and with ~2000 tracks that overhead undoes the very cost this phase
-- removes. Not unique: (location_id, rel_path) can hold a stale present=0 row
-- alongside the live one, so lookups filter present and take the newest.
CREATE INDEX idx_audio_presence_relpath ON audio_presence(location_id, rel_path);
CREATE INDEX idx_sidecar_presence_relpath ON sidecar_presence(location_id, rel_path);
