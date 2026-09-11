"""Free text -> a note subject, resolved against the library.

`note add --album "Fight Songs" --artist "Old 97s"` has to land on the same
subject as the album already in the library, whatever spelling was typed. That
is the collection audit's problem exactly, so this reuses `match_items` rather
than growing a second matcher — including its artist-scoping, its containment
rule, and the thresholds calibrated against real data (see gotcha #12: a
whole-string fuzzy score over "artist title" measurably does not work).

**Never guesses.** A resolution is exact, or it comes back AMBIGUOUS with
candidates for a human to pick from, or UNMATCHED. Auto-resolution only ever
comes from the matcher's OWNED verdict or an exact normalized-title hit;
`difflib` appears solely to *suggest* near misses in an error message, never to
decide one.

Nothing here does I/O. The caller loads the library (`services/library_index`)
and passes it in, which is also what makes this testable without beets.
"""
from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.collection_match import (
    MatchStatus,
    match_items,
    normalize_artist,
    normalize_title,
)
from spindlebot.core.notes import NoteSubjectRef, artist_key

MAX_CANDIDATES = 8


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"      # one subject, confidently
    AMBIGUOUS = "ambiguous"    # plausible matches a human must choose between
    UNMATCHED = "unmatched"    # nothing in the library; needs --new to proceed


@dataclass(frozen=True)
class Candidate:
    """Something the library does have, offered when the query missed."""
    label: str
    artist: str
    album: str | None = None
    mb_albumid: str | None = None


@dataclass(frozen=True)
class Resolution:
    status: ResolutionStatus
    subject: NoteSubjectRef | None = None
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED


def _candidate(album: LibraryAlbum) -> Candidate:
    return Candidate(
        label=f"{album.albumartist} — {album.album}",
        artist=album.albumartist,
        album=album.album,
        mb_albumid=album.mb_albumid,
    )


def _albums_by_artist(library: list[LibraryAlbum], artist: str) -> list[LibraryAlbum]:
    """Group by `artist_key`, NOT by `normalize_artist`.

    They are not interchangeable, and using the normalizer here is a real bug
    that this function had: `normalize_artist` replaces punctuation with a
    SPACE, so "Old 97's" folds to `old 97 s` and "Old 97s" to `old 97s`. The
    collection matcher gets away with that because it compares normalized forms
    fuzzily; equality does not.

    Beyond the immediate fix, keying off `artist_key` is what guarantees the
    invariant that matters: if two spellings resolve to the same subject, the
    resolver must already have treated them as the same artist. Anything else
    lets resolution and identity disagree about what one artist is.
    """
    if not artist:
        return []
    key = artist_key(artist)
    return [a for a in library if a.albumartist and artist_key(a.albumartist) == key]


def _near_artists(library: list[LibraryAlbum], artist: str) -> list[str]:
    """Spelling suggestions only. These never resolve anything on their own."""
    names = {a.albumartist for a in library if a.albumartist}
    by_key = {normalize_artist(n): n for n in names}
    close = difflib.get_close_matches(
        normalize_artist(artist), list(by_key), n=MAX_CANDIDATES, cutoff=0.7
    )
    return [by_key[k] for k in close]


def resolve(
    library: list[LibraryAlbum],
    *,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    allow_new: bool = False,
) -> Resolution:
    """Resolve a typed subject against the library.

    `allow_new` is the caller's explicit "yes, this really is something the
    library does not have" — without it an unmatched query is an error rather
    than a silently-created subject, so a typo cannot quietly become a second
    subject alongside the real one.
    """
    if track and not album:
        raise ValueError("a track note needs its album: --track X requires --album Y")
    if album is None and artist is None:
        raise ValueError("nothing to resolve: pass --artist, --album or --track")

    if album is None:
        return _resolve_artist(library, artist, allow_new=allow_new)
    return _resolve_album(library, artist, album, track, allow_new=allow_new)


def _resolve_artist(
    library: list[LibraryAlbum], artist: str, *, allow_new: bool
) -> Resolution:
    """Exact on the normalized key, which already folds the variance that
    matters — "Old 97's"/"Old 97s", "The Beatles"/"Beatles", diacritics."""
    matches = _albums_by_artist(library, artist)
    if matches:
        # Every spelling here shares one artist_key, so the choice is cosmetic:
        # take the most common form in the library as the display name.
        spelling = Counter(a.albumartist for a in matches).most_common(1)[0][0]
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=NoteSubjectRef.for_artist(spelling),
            reason=f"{len(matches)} album(s) in the library",
        )

    if allow_new:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=NoteSubjectRef.for_artist(artist),
            reason="new subject (--new)",
        )
    near = _near_artists(library, artist)
    return Resolution(
        ResolutionStatus.UNMATCHED,
        candidates=tuple(Candidate(label=n, artist=n) for n in near),
        reason=f"no artist matching {artist!r} in the library",
    )


def _resolve_album(
    library: list[LibraryAlbum],
    artist: str | None,
    album: str,
    track: str | None,
    *,
    allow_new: bool,
) -> Resolution:
    matched, status, reason = _find_album(library, artist, album)

    if status is MatchStatus.OWNED and matched is not None:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=_subject_for(matched.albumartist, matched.album, matched.mb_albumid, track),
            reason=reason,
        )

    if status is MatchStatus.UNCERTAIN or (matched is not None and not allow_new):
        # A near miss is never auto-accepted: confirming it is a human decision.
        return Resolution(
            ResolutionStatus.AMBIGUOUS,
            candidates=tuple(_candidate(a) for a in _shortlist(library, artist, matched)),
            reason=reason,
        )

    if allow_new:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=_subject_for(artist, album, None, track),
            reason="new subject (--new)",
        )
    return Resolution(
        ResolutionStatus.UNMATCHED,
        candidates=tuple(_candidate(a) for a in _shortlist(library, artist, None)),
        reason=reason,
    )


def _find_album(
    library: list[LibraryAlbum], artist: str | None, album: str
) -> tuple[LibraryAlbum | None, MatchStatus, str]:
    """Delegate to the contract-tested matcher when an artist is known.

    Without an artist the matcher cannot help — it is artist-scoped by design —
    so fall back to exact normalized-title equality across the whole library.
    That is deliberately strict: one hit resolves, several are ambiguous, and a
    near miss is not a match at all.
    """
    if artist:
        item = CollectionItem(source="note", source_id="query", artist=artist, title=album)
        result = match_items([item], library)[0]
        return result.matched, result.status, result.reason

    key = normalize_title(album)
    hits = [a for a in library if key and normalize_title(a.album) == key]
    if len(hits) == 1:
        return hits[0], MatchStatus.OWNED, "exact title, one album in the library"
    if len(hits) > 1:
        return None, MatchStatus.UNCERTAIN, (
            f"{len(hits)} albums titled {album!r} — name the artist with --artist"
        )
    return None, MatchStatus.MISSING, f"no album matching {album!r} in the library"


def _shortlist(
    library: list[LibraryAlbum], artist: str | None, matched: LibraryAlbum | None
) -> list[LibraryAlbum]:
    """What to offer a human who missed. The artist's own albums are the most
    useful answer to a mistyped title, so they lead."""
    out: list[LibraryAlbum] = []
    if matched is not None:
        out.append(matched)
    if artist:
        out.extend(a for a in _albums_by_artist(library, artist) if a not in out)
    return out[:MAX_CANDIDATES]


def _subject_for(
    artist: str | None, album: str, mb_albumid: str | None, track: str | None
) -> NoteSubjectRef:
    """Build the subject. A track's ALBUM is verified against the library; the
    track title itself is taken as typed, because LibraryAlbum carries no track
    list to check it against."""
    if track:
        return NoteSubjectRef.for_track(artist, album, track, mb_albumid)
    return NoteSubjectRef.for_album(artist, album, mb_albumid)
