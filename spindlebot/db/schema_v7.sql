-- SpindleBot schema — version 7 (Phase 4.0): lyric lineage presence.
--
-- Records which lyric_version each location currently holds for a doc. This is
-- the causal memory the lineage service needs: without it, two differing .lrc
-- files always look concurrent. With a per-(doc, location) "last-known version",
-- a new observed sha can be classified against the version that location held on
-- the PRIOR scan (its "base"):
--   base == head        -> linear edit (fast-forward the head)   — not a conflict
--   base older/None      -> concurrent edit                       — a real conflict
-- and a location whose sha still matches an old, head-dominated version is BEHIND
-- (a propagation target), never mistaken for an edit.
--
-- One row per (doc, location). Read before it is updated within a scan so the
-- "base" is genuinely the prior-scan version, not this scan's result.

CREATE TABLE lyric_version_presence (
    doc_id       INTEGER NOT NULL REFERENCES lyric_doc(id) ON DELETE CASCADE,
    location_id  INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
    version_id   INTEGER NOT NULL REFERENCES lyric_version(id) ON DELETE CASCADE,
    observed_utc INTEGER NOT NULL,
    PRIMARY KEY (doc_id, location_id)
);
CREATE INDEX idx_lyric_version_presence_doc ON lyric_version_presence(doc_id);
