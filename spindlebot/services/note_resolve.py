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
from spindlebot.core.notes import NoteSubjectRef, artist_key, text_key

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
    # The mbid goes in the LABEL as well as the field: a candidate list whose
    # rows are textually identical is useless for choosing between editions,
    # and the id is what `--mbid` wants.
    suffix = f"  [--mbid {album.mb_albumid}]" if album.mb_albumid else ""
    return Candidate(
        label=f"{album.albumartist} — {album.album}{suffix}",
        artist=album.albumartist,
        album=album.album,
        mb_albumid=album.mb_albumid,
    )


def _albums_by_artist(library: list[LibraryAlbum], artist: str) -> list[LibraryAlbum]:
    """Albums whose artist is the SAME SUBJECT as `artist` — exact on `artist_key`.

    Not `normalize_artist`: it replaces punctuation with a SPACE, so "Old 97's"
    folds to `old 97 s` and "Old 97s" to `old 97s`. The matcher gets away with
    that because it scores candidates fuzzily afterwards; equality does not.

    Keying off `artist_key` also enforces the invariant that matters: if two
    spellings resolve to one subject, the resolver must already treat them as
    one artist, or resolution and identity disagree about what an artist is.
    """
    if not artist:
        return []
    key = artist_key(artist)
    return [a for a in library if a.albumartist and artist_key(a.albumartist) == key]


def _albums_by_loose_artist(
    library: list[LibraryAlbum], artist: str
) -> list[LibraryAlbum]:
    """Albums whose artist matches ignoring the leading article.

    Article-insensitive, so typing "Beatles" reaches "The Beatles". That fold is
    right here and wrong in `artist_key` — here a human confirms or the caller
    refuses; a uuid has no such recourse, and folding the article there merged
    "The Band" with "Band" permanently.

    `normalize_artist` alone is NOT a usable equality basis, for the fourth time
    on this branch: it replaces punctuation with a space, so "Old 97s" folds to
    `old 97s` and "Old 97's" to `old 97 s`. This function silently matched
    nothing for exactly the artist that motivated the strict key, which emptied
    the candidate list an ambiguous release needs. Running the result through
    `text_key` removes the separators and leaves the article strip intact.
    """
    key = _loose_key(artist)
    return [a for a in library if key and _loose_key(a.albumartist) == key]


def _loose_key(name: str | None) -> str:
    """Article-stripped AND separator-free — the two folds composed."""
    return text_key(normalize_artist(name))


def _near_artists(library: list[LibraryAlbum], artist: str) -> list[str]:
    """Spelling suggestions only. These never resolve anything on their own."""
    names = {a.albumartist for a in library if a.albumartist}
    by_key = {normalize_artist(n): n for n in names}
    close = difflib.get_close_matches(
        normalize_artist(artist), list(by_key), n=MAX_CANDIDATES, cutoff=0.7
    )
    return [by_key[k] for k in close]


def _releases_of(scope: list[LibraryAlbum], album: str) -> list[LibraryAlbum]:
    key = normalize_title(album)
    return [a for a in scope if key and normalize_title(a.album) == key]


def resolve(
    library: list[LibraryAlbum],
    *,
    artist: str | None = None,
    album: str | None = None,
    track: str | None = None,
    mb_albumid: str | None = None,
    allow_new: bool = False,
) -> Resolution:
    """Resolve a typed subject against the library.

    `mb_albumid` picks one release when an artist and title match several — the
    only way to say "this edition, not that one" without `--new`, which would
    throw the release identity away.

    `allow_new` is the caller's explicit "yes, this really is something the
    library does not have" — without it an unmatched query is an error rather
    than a silently-created subject, so a typo cannot quietly become a second
    subject alongside the real one.
    """
    if track and not album:
        raise ValueError("a track note needs its album: --track X requires --album Y")
    if album is None and artist is None:
        raise ValueError("nothing to resolve: pass --artist, --album or --track")

    if mb_albumid:
        library = [a for a in library if a.mb_albumid == mb_albumid]
        if not library and not allow_new:
            return Resolution(
                ResolutionStatus.UNMATCHED,
                reason=f"no album with MusicBrainz id {mb_albumid!r} in the library",
            )
    if album is None:
        return _resolve_artist(library, artist, allow_new=allow_new)
    return _resolve_album(library, artist, album, track, allow_new=allow_new)


@dataclass(frozen=True)
class _ArtistMatch:
    """The library's own name for a typed artist, or why it could not be settled."""
    canonical: str | None = None
    albums: tuple[LibraryAlbum, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""


def _match_artist(library: list[LibraryAlbum], artist: str) -> _ArtistMatch:
    """Settle a typed artist onto ONE library artist, or refuse.

    Exact on `artist_key` first, then article-insensitively. The two-step lets
    identity stay strict while typing stays forgiving: "Beatles" finds "The
    Beatles" and callers key off the LIBRARY's spelling, so both typings land on
    one subject without `artist_key` folding the article itself.

    Shared with the album path deliberately. When the album path called
    `match_items` directly it inherited the matcher's FUZZY artist candidates, so
    `Loreena McKennit` + `An Ancient Muse` came back OWNED and filed under
    `Loreena McKennitt` — while the artist-only path refused that exact typo.
    Two rules for "is this the same artist" is one rule too many.
    """
    exact = _albums_by_artist(library, artist)
    if exact:
        # Every spelling here shares one artist_key, so the choice is cosmetic:
        # take the most common form in the library as the display name.
        canonical = Counter(a.albumartist for a in exact).most_common(1)[0][0]
        return _ArtistMatch(canonical, tuple(exact),
                            reason=f"{len(exact)} album(s) in the library")

    loose = _albums_by_loose_artist(library, artist)
    if loose:
        # This fold can span genuinely different artists — "The Band" and "Band"
        # both normalize to `band`. Guessing between them is what this module
        # does not do.
        by_subject: dict[str, list[LibraryAlbum]] = {}
        for album in loose:
            by_subject.setdefault(artist_key(album.albumartist), []).append(album)
        if len(by_subject) == 1:
            canonical = Counter(a.albumartist for a in loose).most_common(1)[0][0]
            return _ArtistMatch(canonical, tuple(loose),
                                reason="matched ignoring the leading article")
        return _ArtistMatch(
            candidates=tuple(
                Candidate(label=group[0].albumartist, artist=group[0].albumartist)
                for group in by_subject.values()
            ),
            reason=f"{len(by_subject)} artists match {artist!r} ignoring the article",
        )

    return _ArtistMatch(
        candidates=tuple(
            Candidate(label=n, artist=n) for n in _near_artists(library, artist)
        ),
        reason=f"no artist matching {artist!r} in the library",
    )


def _resolve_artist(
    library: list[LibraryAlbum], artist: str, *, allow_new: bool
) -> Resolution:
    found = _match_artist(library, artist)
    if found.canonical:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=NoteSubjectRef.for_artist(found.canonical),
            reason=found.reason,
        )
    if allow_new:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=NoteSubjectRef.for_artist(artist),
            reason="new subject (--new)",
        )
    return Resolution(
        ResolutionStatus.AMBIGUOUS if found.candidates and len(found.candidates) > 1
        else ResolutionStatus.UNMATCHED,
        candidates=found.candidates,
        reason=found.reason,
    )


def _resolve_album(
    library: list[LibraryAlbum],
    artist: str | None,
    album: str,
    track: str | None,
    *,
    allow_new: bool,
) -> Resolution:
    """Settle the ARTIST first, then the title inside that artist's albums.

    The artist has to be settled by `_match_artist` — exactly, or uniquely
    ignoring the article — before any title matching happens. Going straight to
    `match_items` inherited its fuzzy artist candidates, which made a typo'd
    artist plus an exact album title come back OWNED.
    """
    canonical_artist = artist
    scope = library
    if artist:
        found = _match_artist(library, artist)
        if not found.canonical:
            if allow_new:
                # Nothing to canonicalize against; the typed spelling is all
                # there is, and --new says the library is not the authority.
                return _new_subject(artist, album, track)
            return Resolution(
                ResolutionStatus.AMBIGUOUS if len(found.candidates) > 1
                else ResolutionStatus.UNMATCHED,
                candidates=found.candidates,
                reason=found.reason,
            )
        canonical_artist, scope = found.canonical, list(found.albums)

    matched, status, reason = _find_album(scope, canonical_artist, album)

    if status is MatchStatus.OWNED and matched is not None:
        return Resolution(
            ResolutionStatus.RESOLVED,
            subject=_subject_for(matched.albumartist, matched.album, matched.mb_albumid, track),
            reason=reason,
        )

    if allow_new:
        # The artist resolved, so key the new subject off the LIBRARY's spelling
        # rather than what was typed — otherwise "Old 97s" and "Old 97's" fork
        # into two subjects for the same unowned record.
        return _new_subject(canonical_artist, album, track)

    # When several releases share the title, THEY are the choice to offer — the
    # artist's whole discography is noise, and the candidate labels carry the
    # `--mbid` value that resolves it.
    releases = _releases_of(scope, album)
    offer = releases if len(releases) > 1 else _shortlist(library, artist, matched)
    return Resolution(
        ResolutionStatus.AMBIGUOUS if matched is not None or status is MatchStatus.UNCERTAIN
        else ResolutionStatus.UNMATCHED,
        candidates=tuple(_candidate(a) for a in offer),
        reason=reason,
    )


def _new_subject(artist: str | None, album: str, track: str | None) -> Resolution:
    return Resolution(
        ResolutionStatus.RESOLVED,
        subject=_subject_for(artist, album, None, track),
        reason="new subject (--new)",
    )


def _find_album(
    scope: list[LibraryAlbum], artist: str | None, album: str
) -> tuple[LibraryAlbum | None, MatchStatus, str]:
    """Match a title within an already artist-scoped list.

    `scope` is one artist's albums when an artist was given, else the whole
    library — and without an artist the matcher cannot help at all, since it is
    artist-scoped by design, so that case is exact normalized-title equality
    only: one hit resolves, several are ambiguous, a near miss is nothing.

    An exact title that hits MORE THAN ONE release is ambiguous even when the
    artist is known. Two editions can share artist and title while differing in
    `mb_albumid`, and the matcher returns whichever comes first — which would
    then bake an arbitrary release id into the subject key.
    """
    exact = _releases_of(scope, album)
    if len({a.mb_albumid for a in exact}) > 1:
        return None, MatchStatus.UNCERTAIN, (
            f"{len(exact)} releases of {album!r} differ by MusicBrainz id — "
            "pick one with --mbid <id>"
        )
    if artist:
        item = CollectionItem(source="note", source_id="query", artist=artist, title=album)
        result = match_items([item], scope)[0]
        return result.matched, result.status, result.reason

    if len(exact) == 1:
        return exact[0], MatchStatus.OWNED, "exact title, one album in the library"
    if len(exact) > 1:
        return None, MatchStatus.UNCERTAIN, (
            f"{len(exact)} albums titled {album!r} — name the artist with --artist"
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
        # Loose, not exact: someone who typed "Beatles" and mistyped the album
        # should still be shown The Beatles' albums.
        out.extend(a for a in _albums_by_loose_artist(library, artist) if a not in out)
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
