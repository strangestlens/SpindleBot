-- SpindleBot schema — version 3 (feat/sidecars): album grouping + sidecars.
--
-- Albums exist so album-level sidecars (cover art, the .nolrc marker) can attach
-- to a stable album identity instead of one arbitrary track. A sidecar belongs
-- to a track's or album's IDENTITY, never to a path: a .lrc follows its track,
-- cover.jpg / .nolrc follow their album. Mirrors the audio identity/presence
-- split — sidecar_content is the canonical thing, sidecar_presence is per-copy.

-- An album groups tracks under a stable key derived from tags that survive
-- pretag/posttag (see core/albums.py). albumartist/album/mb_albumid are
-- advisory snapshots; album_key is the identity.
CREATE TABLE album (
    id              INTEGER PRIMARY KEY,
    album_key       TEXT NOT NULL UNIQUE,
    albumartist     TEXT,
    album           TEXT,
    mb_albumid      TEXT,
    first_seen_utc  INTEGER NOT NULL,
    last_seen_utc   INTEGER NOT NULL
);

-- Which audio_content belongs to which album. A track may appear on more than
-- one album (e.g. a compilation reissue), so the relationship is many-to-many.
CREATE TABLE album_track (
    album_id   INTEGER NOT NULL REFERENCES album(id) ON DELETE CASCADE,
    audio_id   INTEGER NOT NULL REFERENCES audio_content(id) ON DELETE CASCADE,
    PRIMARY KEY (album_id, audio_id)
);
CREATE INDEX idx_album_track_audio ON album_track(audio_id);

-- A sidecar belongs to a parent's IDENTITY: parent_kind selects the table
-- (track -> audio_content.id, album -> album.id), role is what the file is.
-- sha256 is the current content hash of the sidecar bytes. The
-- (parent_kind, parent_id, role) triple is unique — one cover / one lyric /
-- one nolrc per parent. parent_id is polymorphic, so there is intentionally
-- no SQL foreign key on it — which also means deleting an album/track does NOT
-- cascade to its sidecar_content rows; a deleter (the future reconciler) must
-- remove them explicitly.
CREATE TABLE sidecar_content (
    id              INTEGER PRIMARY KEY,
    parent_kind     TEXT NOT NULL,            -- 'track' | 'album'
    parent_id       INTEGER NOT NULL,
    role            TEXT NOT NULL,            -- 'lrc' | 'cover' | 'nolrc'
    sha256          TEXT NOT NULL,            -- content hash of the sidecar bytes
    first_seen_utc  INTEGER NOT NULL,
    last_seen_utc   INTEGER NOT NULL,
    UNIQUE (parent_kind, parent_id, role)
);

-- Presence of a sidecar's bytes at a location; mirrors audio_presence. A copy
-- whose file_sha256 diverges from sidecar_content.sha256 is what the later
-- lyrics-sync phase will detect; this phase only records observed facts.
CREATE TABLE sidecar_presence (
    sidecar_id    INTEGER NOT NULL REFERENCES sidecar_content(id) ON DELETE CASCADE,
    location_id   INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
    present       INTEGER NOT NULL,           -- 1 observed present, 0 observed absent
    rel_path      TEXT,
    file_sha256   TEXT,                       -- integrity hash of THIS copy
    byte_size     INTEGER,
    observed_utc  INTEGER NOT NULL,
    PRIMARY KEY (sidecar_id, location_id)
);
CREATE INDEX idx_sidecar_presence_loc ON sidecar_presence(location_id, present);
