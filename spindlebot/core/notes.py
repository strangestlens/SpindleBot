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
where the library says "Old 97's"), so artist and track keys fold punctuation
and diacritics through `text_key`.

**But not as rich as the collection matcher.** `normalize_artist` also strips a
leading article, and that heuristic cannot come near an identity key: see
`text_key`.

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
from spindlebot.core.collection_match import normalize_track_title
from spindlebot.core.enums import NoteSubjectKind


def text_key(value: str | None) -> str:
    """Exact-equality key for a display string — an artist, album or track name.

    Folds what a human varies without meaning to: case, Latin diacritics,
    punctuation, `&`/`and`, and whitespace. So "Old 97's" and "Old 97s" are one
    subject, as are "Björk"/"Bjork" and "Belle & Sebastian"/"Belle and
    Sebastian".

    Two deliberate non-uses:

    **Not `normalize_artist`.** It strips a leading article, which is a
    *fuzzy-matching* heuristic and wrong for identity: "The Band" and "Band"
    both reduce to `band`, as do "The The"/"The" and "The Sound"/"Sound". The
    collection matcher can absorb that because it scores candidates afterwards;
    a uuid has no downstream, so the two artists would merge permanently and
    silently. Article-insensitivity belongs in resolution, which can then key
    off the library's own spelling.

    **The separators are removed, not collapsed.** `normalize_track_title`
    replaces punctuation with a SPACE, so "Old 97's" becomes `old 97 s` and
    "Old 97s" becomes `old 97s` — equal under fuzzy comparison, unequal under
    `==`.
    """
    return "".join(normalize_track_title(value).split())


def artist_key(name: str | None, mb_artistid: str | None = None) -> str:
    """Deterministic subject key for an artist.

    Prefers the MusicBrainz artist id when present; otherwise `text_key`, which
    folds "Old 97s"/"Old 97's" and "Björk"/"Bjork" onto one subject but keeps a
    leading article — "The Band" is not "Band". Typing the article-less form
    still reaches the right subject, because resolution matches it against the
    library and keys off the library's own spelling.
    """
    if mb_artistid and mb_artistid.strip():
        basis = f"mb:{mb_artistid.strip().lower()}"
    else:
        basis = f"n:{text_key(name)}"
    return str(uuid5(NAMESPACE_URL, f"spindlebot:note-artist:{basis}"))


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
    basis = f"{parent_album_key}\x00{text_key(title)}"
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
            mbid=mb_albumid,
        )

    @property
    def alt_keys(self) -> tuple[str, ...]:
        """Keys this same work could already be filed under.

        `album_key` prefers a MusicBrainz id, so the key for a record CHANGES the
        day you rip it and learn its MBID. A note written before then — "look for
        this album" — would fork into a second subject, and its history and
        filters would stop following the work.

        So an MBID-backed ref reports the name-derived key it would have had
        beforehand, and the service adopts that subject instead of creating a new
        one. Nothing is reported in the other direction: a name-keyed ref cannot
        guess an MBID it has never seen.
        """
        if not self.mbid:
            return ()
        if self.kind is NoteSubjectKind.ARTIST:
            return (artist_key(self.artist_name),)
        nameless_album = album_key(self.artist_name, self.album_title or "", None)
        if self.kind is NoteSubjectKind.ALBUM:
            return (nameless_album,)
        return (track_key(nameless_album, self.track_title),)

    @property
    def label(self) -> str:
        """Human-readable subject, e.g. `Old 97's — Fight Songs — Murder (Or A Heart Attack)`."""
        parts = [p for p in (self.artist_name, self.album_title, self.track_title) if p]
        return " — ".join(parts) if parts else self.subject_key
