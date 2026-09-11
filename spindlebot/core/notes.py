"""Pure identity and text canonicalization for listening notes.

No DB, no I/O, no print.

A note attaches to a musical **work** — an artist, an album, a track — never to
bytes. `audio_content.id` is a decoded-audio MD5: it identifies one rip of one
pressing, so a note keyed to it would die on a re-rip and silently fork the day
a remaster is imported. The keys here survive both, and they carry no foreign
key into the library, so a note can be written about an album that was never
ripped at all.

Two normalization decisions, both deliberate:

**Richer than `core/albums.py:album_key`.** That key folds with a bare
`strip().lower()`, which is enough because it is only ever computed from values
the library already holds — it has to be self-consistent, nothing more. A note
key may instead be computed from what a human typed at a prompt ("Old 97s"
where the library says "Old 97's"), so artist and track keys fold punctuation,
diacritics and a leading article through the same normalizers the collection
matcher uses.

**Album subjects reuse `album_key()` unchanged.** The payoff is a direct join:
`note_subject.subject_key` for an album IS `album.album_key`, which is UNIQUE,
so note → library album needs no extra machinery. Typing variance is absorbed
one layer up, by resolution, which matches free text against the library and
then computes the key from the values it resolved to.
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from spindlebot.core.albums import album_key
from spindlebot.core.collection_match import normalize_artist, normalize_track_title
from spindlebot.core.enums import NoteSubjectKind


def _key_basis(normalized: str) -> str:
    """Collapse a normalized name to an exact-equality basis.

    `normalize_artist` / `normalize_track_title` replace punctuation with a
    SPACE, so "Old 97's" folds to `old 97 s` while "Old 97s" folds to `old 97s`.
    The collection matcher never notices, because it compares those with jaccard
    and sequence similarity — token splitting is absorbed downstream. An
    identity key has no downstream: it is exact equality or nothing. Removing
    separators entirely is what makes the two spellings one subject.
    """
    return "".join(normalized.split())


def artist_key(name: str | None, mb_artistid: str | None = None) -> str:
    """Deterministic subject key for an artist.

    Prefers the MusicBrainz artist id when present; otherwise a normalized
    name, which folds "Old 97s" and "Old 97's" — and "The Beatles" and
    "Beatles" — onto one subject.
    """
    if mb_artistid and mb_artistid.strip():
        basis = f"mb:{mb_artistid.strip().lower()}"
    else:
        basis = f"n:{_key_basis(normalize_artist(name))}"
    return str(uuid5(NAMESPACE_URL, f"spindlebot:note-artist:{basis}"))


def title_key(value: str | None) -> str:
    """Exact-equality key for an album or track title.

    The title half of `track_key`, exposed because filtering needs the same
    folding that identity uses: `note list --album "fight songs"` has to reach
    notes filed under "Fight Songs". Keeping one function means a filter can
    never disagree with the key it is filtering on.
    """
    return _key_basis(normalize_track_title(value))


def track_key(parent_album_key: str, title: str | None) -> str:
    """Deterministic subject key for a track, scoped to its album.

    Position is deliberately NOT part of the key. Disc and track numbers are
    demonstrably unstable in this system — `runner._fix_multidisc()` exists
    precisely because MusicBrainz reports `disctotal=2` for single-disc albums
    and the numbering is patched *after* import — so a position-keyed note
    would detach from its track during an ordinary repair. The title is what a
    human note is actually about.

    The cost is that two tracks sharing a title on one album (a reprise, a
    hidden track) collapse onto one subject. That is rare, and far cheaper than
    silently orphaning notes.
    """
    basis = f"{parent_album_key}\x00{title_key(title)}"
    return str(uuid5(NAMESPACE_URL, f"spindlebot:note-track:{basis}"))


def canonicalize_body(text: str) -> str:
    """Normalize note text so that a no-op edit is detectably a no-op.

    Opening a note in `$EDITOR` and closing it unchanged must not append a
    revision, and an editor that rewrites line endings or adds a trailing
    newline must not look like an edit. Interior blank lines are preserved —
    they are the author's paragraph breaks, not noise.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def body_sha256(body: str) -> str:
    """Content hash of a canonicalized note body.

    The dedupe key for revisions today, and the lineage key when notes
    eventually sync across devices — the same primitive `services/lyrics_sync.py`
    already reasons over.
    """
    return hashlib.sha256(canonicalize_body(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NoteSubjectRef:
    """A note's subject, self-describing even when the library knows nothing of it.

    The display fields are a snapshot, not a lookup: a note about an album that
    was never ripped still has to render as something a human recognizes.
    """
    kind: NoteSubjectKind
    subject_key: str
    artist_name: str | None = None
    album_title: str | None = None
    track_title: str | None = None
    mbid: str | None = None

    @staticmethod
    def for_artist(name: str, mb_artistid: str | None = None) -> "NoteSubjectRef":
        return NoteSubjectRef(
            kind=NoteSubjectKind.ARTIST,
            subject_key=artist_key(name, mb_artistid),
            artist_name=name,
            mbid=mb_artistid,
        )

    @staticmethod
    def for_album(
        artist: str | None, album: str, mb_albumid: str | None = None
    ) -> "NoteSubjectRef":
        return NoteSubjectRef(
            kind=NoteSubjectKind.ALBUM,
            subject_key=album_key(artist, album, mb_albumid),
            artist_name=artist,
            album_title=album,
            mbid=mb_albumid,
        )

    @staticmethod
    def for_track(
        artist: str | None,
        album: str,
        title: str,
        mb_albumid: str | None = None,
    ) -> "NoteSubjectRef":
        return NoteSubjectRef(
            kind=NoteSubjectKind.TRACK,
            subject_key=track_key(album_key(artist, album, mb_albumid), title),
            artist_name=artist,
            album_title=album,
            track_title=title,
        )

    @property
    def label(self) -> str:
        """Human-readable subject, e.g. `Old 97's — Fight Songs — Murder (Or A Heart Attack)`."""
        parts = [p for p in (self.artist_name, self.album_title, self.track_title) if p]
        return " — ".join(parts) if parts else self.subject_key
