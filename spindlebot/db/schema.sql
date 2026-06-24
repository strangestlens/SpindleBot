-- SpindleBot schema — version 1 (Phase 0).
--
-- This is the minimal identity/presence core. Later phases extend it via
-- additional migrations (sidecars, version vectors, conflicts, reconciler
-- run/action tables), NOT by editing this file.
--
-- Design: identity (a content hash) is separate from presence (which locations
-- hold the bytes). Presence is metadata that stays known even when a device is
-- unplugged. Overlays beets via a nullable beets_item_id; never depends on it.

-- A location is a first-class place a file can live: the authoring library
-- (Pending), an off-site drive, a DAP/SD card, an rclone remote. Identified by
-- a SpindleBot-minted uuid, NOT a volume uuid or rsync target string.
CREATE TABLE location (
    id                      INTEGER PRIMARY KEY,
    uuid                    TEXT NOT NULL UNIQUE,
    name                    TEXT NOT NULL,
    kind                    TEXT NOT NULL,            -- 'library' | 'local_drive' | 'rclone'
    is_authoritative_audio  INTEGER NOT NULL DEFAULT 0,  -- exactly one (the authoring library)
    is_retention            INTEGER NOT NULL DEFAULT 0,   -- counts toward min_copies
    enabled                 INTEGER NOT NULL DEFAULT 1,
    last_seen_utc           INTEGER
);

-- The canonical "thing": an audio track, independent of tags or location.
-- identity = decoded-audio MD5 when available (survives re-tagging), else the
-- whole-file sha256; identity_kind records which, so the fallback is explicit.
CREATE TABLE audio_content (
    id              INTEGER PRIMARY KEY,
    identity        TEXT NOT NULL UNIQUE,
    identity_kind   TEXT NOT NULL,                   -- 'audio_md5' | 'file_sha256'
    -- advisory denormalized tags, snapshot at discovery; never identity
    artist          TEXT,
    album           TEXT,
    title           TEXT,
    disc_no         INTEGER,
    track_no        INTEGER,
    duration_s      INTEGER,
    beets_item_id   INTEGER,                         -- nullable overlay into beets items.id
    first_seen_utc  INTEGER NOT NULL,
    last_seen_utc   INTEGER NOT NULL
);
CREATE INDEX idx_audio_content_beets ON audio_content(beets_item_id);

-- Presence of audio bytes at a location. A row with present=0 still records
-- "this location is KNOWN not to hold this content as of observed_utc".
CREATE TABLE audio_presence (
    audio_id        INTEGER NOT NULL REFERENCES audio_content(id) ON DELETE CASCADE,
    location_id     INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
    present         INTEGER NOT NULL,                -- 1 observed present, 0 observed absent
    rel_path        TEXT,                            -- path within the location when present
    file_sha256     TEXT,                            -- integrity hash of THIS copy (not identity)
    byte_size       INTEGER,
    observed_utc    INTEGER NOT NULL,                -- when this fact was last confirmed by a scan
    PRIMARY KEY (audio_id, location_id)
);
CREATE INDEX idx_audio_presence_loc ON audio_presence(location_id, present);
