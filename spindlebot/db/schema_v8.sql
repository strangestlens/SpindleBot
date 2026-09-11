-- SpindleBot schema — version 8 (listening notes): authored annotations.
--
-- Everything else in this database is OBSERVED and regenerable: drop it, re-run
-- `spindlebot inventory`, and the rows come back. These tables are the first
-- AUTHORED data in the system — a lost note is gone for good. Two structural
-- consequences follow, and neither is decoration:
--
--   * Revisions are append-only. Editing a note appends seq+1; nothing is ever
--     overwritten, so history is free and an accidental edit is recoverable.
--   * Deletes are soft (note.status). Authored text is never dropped outright.
--
-- A note attaches to a musical WORK, never to bytes — see core/notes.py.

-- What a note is about. subject_key is polymorphic by `kind`:
--   artist -> core.notes.artist_key()
--   album  -> core.albums.album_key()   (equal to album.album_key, which is UNIQUE)
--   track  -> core.notes.track_key()    (scoped inside its album)
--
-- There is deliberately NO foreign key into album/audio_content. A note must be
-- writable about an album that was never ripped, an artist with no rows, or a
-- track whose audio has since been pruned. Resolution to a library row is a
-- LOOKUP, never a constraint. The display columns exist for the same reason:
-- a subject the library knows nothing about still has to render for a human.
CREATE TABLE note_subject (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,            -- 'artist' | 'album' | 'track'
    subject_key  TEXT NOT NULL,
    artist_name  TEXT,
    album_title  TEXT,
    track_title  TEXT,
    mbid         TEXT,
    created_utc  INTEGER NOT NULL,
    UNIQUE (kind, subject_key)
);

-- One listening sitting. An evening with a record produces several notes across
-- several subjects; this is what makes them one event rather than an unrelated
-- pile. note.session_id is nullable — a note written outside a sitting is still
-- a first-class note — and one `note import` of a day's writing becomes one
-- session, so provenance survives the import.
CREATE TABLE note_session (
    id           INTEGER PRIMARY KEY,
    uuid         TEXT NOT NULL UNIQUE,
    occurred_utc INTEGER NOT NULL,
    title        TEXT,
    created_utc  INTEGER NOT NULL
);
CREATE INDEX idx_note_session_occurred ON note_session(occurred_utc);

-- A note's identity and lifecycle. The BODY is in note_revision.
-- `uuid` is device-stable: it is the join key when notes eventually sync across
-- machines, where an autoincrement id would collide.
CREATE TABLE note (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT NOT NULL UNIQUE,
    subject_id  INTEGER NOT NULL REFERENCES note_subject(id) ON DELETE CASCADE,
    session_id  INTEGER REFERENCES note_session(id) ON DELETE SET NULL,
    status      TEXT NOT NULL,             -- 'active' | 'deleted' (soft)
    created_utc INTEGER NOT NULL,
    updated_utc INTEGER NOT NULL
);
CREATE INDEX idx_note_subject ON note(subject_id, status);
CREATE INDEX idx_note_session ON note(session_id);

-- One append-only draft of a body. The head is MAX(seq) rather than a pointer
-- column on `note`: a head_revision column would make note <-> note_revision a
-- foreign-key cycle, and connection.open_db enforces foreign_keys=ON
-- immediately, so the first insert of the pair could only go in half-formed.
--
-- UNIQUE(note_id, seq) is the real guard on seq allocation — the writer reads
-- MAX(seq)+1, so the constraint, not the read, is what makes a concurrent
-- append fail loudly instead of silently overwriting a draft.
--
-- sha256 hashes the CANONICALIZED body (core/notes.canonicalize_body), so an
-- editor that rewrites line endings cannot masquerade as an edit. It is the
-- dedupe key today and the lineage key when notes sync, the same primitive
-- services/lyrics_sync.py already reasons over.
CREATE TABLE note_revision (
    id          INTEGER PRIMARY KEY,
    note_id     INTEGER NOT NULL REFERENCES note(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    body        TEXT NOT NULL,
    body_format TEXT NOT NULL,             -- 'markdown'
    sha256      TEXT NOT NULL,
    author      TEXT,                      -- actor id, for multi-device later
    created_utc INTEGER NOT NULL,
    UNIQUE (note_id, seq)
);
CREATE INDEX idx_note_revision_note ON note_revision(note_id, seq);

-- Tags are an OPEN set, deliberately — a table, not a StrEnum. The existing
-- note corpus hand-rolls its own vocabulary ("surprise track:", "Note:"), and
-- freezing that vocabulary before it is known would be the wrong call.
-- `note list --tag todo` is the listening queue that falls out of this.
CREATE TABLE note_tag (
    note_id INTEGER NOT NULL REFERENCES note(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE INDEX idx_note_tag_tag ON note_tag(tag);
