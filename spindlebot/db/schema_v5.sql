-- SpindleBot schema — version 5 (Phase 2.3): lyric versioning + conflicts.
--
-- Substrate for the Phase-4 masterless lyric sync; nothing writes these yet.
-- A lyric is reconciled by version vector (core/vclock), never by mtime. The
-- plan's separate lyric_version_vector table is represented here as the
-- canonical vclock_json column on lyric_version (vclock.to_json/from_json), and
-- "which version is current" is a head_version_id pointer rather than a per-row
-- is_head flag — same information, fewer moving parts. A real lyric_version_vector
-- table can be normalized out later if relational vclock queries are needed.

-- The logical lyric for one track. head_version_id points at the current winning
-- version; it is a plain integer (not an FK) to avoid a circular reference with
-- lyric_version.doc_id.
CREATE TABLE lyric_doc (
    id              INTEGER PRIMARY KEY,
    audio_id        INTEGER NOT NULL UNIQUE REFERENCES audio_content(id) ON DELETE CASCADE,
    head_version_id INTEGER,            -- -> lyric_version.id (no FK: breaks a cycle)
    created_utc     INTEGER NOT NULL,
    updated_utc     INTEGER NOT NULL
);

-- Immutable log of every materialized version of a doc. sha256 is the version's
-- content hash; vclock_json is its canonical version vector; authored_utc is a
-- TIEBREAKER ONLY, never the basis for who-wins; source records its origin.
CREATE TABLE lyric_version (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES lyric_doc(id) ON DELETE CASCADE,
    sha256        TEXT NOT NULL,
    vclock_json   TEXT NOT NULL,
    source        TEXT,                 -- 'scan' | 'edit' | 'ai-retimer' | ...
    authored_utc  INTEGER,              -- tiebreaker only
    created_utc   INTEGER NOT NULL
);
CREATE INDEX idx_lyric_version_doc ON lyric_version(doc_id);

-- A recorded divergence for a human to adjudicate. Nothing is ever auto-deleted:
-- the loser is preserved (loser_kept_path) and the row stays 'open' until
-- resolved. winner/loser are lyric_version ids (plain integers — the conflict
-- model may cover other content kinds later).
CREATE TABLE conflict (
    id              INTEGER PRIMARY KEY,
    audio_id        INTEGER REFERENCES audio_content(id) ON DELETE CASCADE,
    winner_version  INTEGER,            -- -> lyric_version.id
    loser_version   INTEGER,            -- -> lyric_version.id
    loser_kept_path TEXT,
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'resolved'
    detected_utc    INTEGER NOT NULL,
    resolved_utc    INTEGER
);
CREATE INDEX idx_conflict_status ON conflict(status);
