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
