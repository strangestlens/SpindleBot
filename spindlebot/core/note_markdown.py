"""Markdown <-> listening notes. Pure: no I/O, no DB, no print.

The shape is the one the existing note corpus already uses: a heading names a
subject, and the prose under it is the note. Nesting carries the level —

    # Artist
    ## Album
    ### Track

`root_level` shifts that window down the document, which is what makes a real
file work: the corpus starts with a `# CDs` grouping heading that is not a
subject at all, so `root_level=2` reads `##` as the artist level and records
`# CDs` as a skipped section rather than inventing an artist called "CDs".

Two rules do the real work:

**All prose under a heading, up to the next heading, is ONE note.** Splitting on
blank lines would be a guess about where one thought ends, and a wrong guess
shreds a paragraph into fragments attributed to the same subject. If the author
wants two notes they write two headings — and `render` emits exactly that, so
the split survives a round trip.

**A heading with no prose produces no note.** It is structure, not content:
`# Old 97's` above `## Fight Songs` says where the album sits, it is not an
empty note about the artist.

`parse(render(notes)) == notes` is the contract, and it is what makes
`note export` a real escape hatch rather than a lossy pretty-printer.

It holds for every chain shape including gapped ones (an album with no artist,
an artist with a track and no album) with one inherent exception: heading nesting
cannot express "this album has NO artist" once an artist heading is already in
scope, because there is no syntax that unsets a level without also setting it.
So a corpus where the SAME album appears both with and without an artist folds
those two subjects into the parented one on re-import. It is pinned by a test
rather than papered over; the outcome is a sensible neighbouring subject, not
corruption, and the situation only arises from hand-written input that names an
album without ever naming its artist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from spindlebot.core.enums import NoteSubjectKind
from spindlebot.core.notes import canonicalize_body

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# A body line that *looks* like a heading is escaped on the way out and
# unescaped on the way in. Counting the backslashes keeps it reversible, so an
# author who genuinely wrote `\# not a heading` gets that text back verbatim.
_ESCAPABLE = re.compile(r"^(\\*)(#{1,6}\s)")

MAX_DEPTH = 3  # artist, album, track


@dataclass(frozen=True)
class ParsedNote:
    """One note lifted out of a document, before any library resolution."""
    kind: NoteSubjectKind
    body: str
    artist: str | None = None
    album: str | None = None
    track: str | None = None
    # Where it came from, for the --dry-run resolution table. Excluded from
    # equality: a rendered document has no line numbers, and the round-trip
    # contract is about content.
    line_no: int = field(default=0, compare=False)

    @property
    def label(self) -> str:
        parts = [p for p in (self.artist, self.album, self.track) if p]
        return " — ".join(parts)


@dataclass(frozen=True)
class SkippedHeading:
    """A heading that named no subject. Surfaced so an import can report it
    rather than silently dropping a line the author wrote."""
    text: str
    level: int
    line_no: int
    reason: str


@dataclass(frozen=True)
class ParsedDocument:
    notes: tuple[ParsedNote, ...] = ()
    skipped: tuple[SkippedHeading, ...] = ()


def _unescape(line: str) -> str:
    m = _ESCAPABLE.match(line)
    return line[1:] if m and m.group(1) else line


def _escape(line: str) -> str:
    return "\\" + line if _ESCAPABLE.match(line) else line


def parse(text: str, *, root_level: int = 1) -> ParsedDocument:
    """Read a markdown document into notes plus the headings it could not use.

    `root_level` is the heading level that means "artist"; album and track are
    the two levels below it.
    """
    artist_level = root_level
    album_level = root_level + 1
    track_level = root_level + 2

    notes: list[ParsedNote] = []
    skipped: list[SkippedHeading] = []
    artist = album = track = None
    kind: NoteSubjectKind | None = None
    buf: list[str] = []
    start_line = 0

    def flush() -> None:
        nonlocal buf
        body = canonicalize_body("\n".join(buf))
        if kind is not None and body:
            notes.append(ParsedNote(
                kind=kind, body=body, artist=artist, album=album,
                track=track, line_no=start_line,
            ))
        buf = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        m = _HEADING.match(line)
        if not m:
            buf.append(_unescape(line))
            continue

        level, heading = len(m.group(1)), m.group(2)

        if level > track_level:
            # Deeper than a track: not a subject, but the author wrote it, so it
            # stays in the current note's body verbatim rather than vanishing.
            skipped.append(SkippedHeading(
                heading, level, line_no, "deeper than track level"))
            buf.append(line)
            continue

        flush()
        start_line = line_no
        if level < root_level:
            # A grouping heading above the subject window ("CDs"). It does not
            # name an artist; it ends whatever came before it.
            skipped.append(SkippedHeading(
                heading, level, line_no, "above the subject levels"))
            artist = album = track = kind = None
        elif level == artist_level:
            artist, album, track = heading, None, None
            kind = NoteSubjectKind.ARTIST
        elif level == album_level:
            album, track = heading, None
            kind = NoteSubjectKind.ALBUM
        elif level == track_level:
            track = heading
            kind = NoteSubjectKind.TRACK

    flush()
    return ParsedDocument(notes=tuple(notes), skipped=tuple(skipped))


def render(notes, *, root_level: int = 1) -> str:
    """Write notes back out as markdown that `parse` reads identically.

    Parent headings are emitted only when they change, so an artist heading is
    not repeated above every one of its albums. Two notes on the SAME subject
    re-emit the deepest heading — that is what keeps them two notes instead of
    merging into one body on the way back in.
    """
    out: list[str] = []
    prev: tuple[str | None, ...] = (None, None, None)

    for note in notes:
        chain = (note.artist, note.album, note.track)
        # The levels this note actually occupies. NOT a count: `parse` can
        # legitimately produce a note with a gap — `## Album` with no artist
        # heading above it, or `# Artist` followed by `### Track` — and treating
        # the filled levels as a dense prefix renders those as `# None`, which
        # re-imports as a different subject entirely.
        levels = [i for i, part in enumerate(chain) if part is not None]
        if not levels:
            continue
        # First occupied level that differs; if none does, re-emit the deepest so
        # a repeated subject stays a separate note. Searching only the occupied
        # levels is what makes an artist note following an album note pop back
        # out to its own heading instead of emitting nothing at all.
        # A level this note leaves EMPTY but the previous note filled has to be
        # cleared, and markdown has no "unset" — only a shallower heading resets
        # the levels below it. So restart the chain from its shallowest occupied
        # level, which is what makes `# A` / `### C` come back as artist A with
        # NO album rather than inheriting the album above it.
        needs_reset = any(
            chain[i] is None and prev[i] is not None for i in range(levels[-1])
        )
        start = (
            levels[0] if needs_reset
            else next((i for i in levels if chain[i] != prev[i]), levels[-1])
        )
        for i in levels:
            if i < start:
                continue
            out.append(f"{'#' * (root_level + i)} {chain[i]}")
            out.append("")
        out.extend(_escape(line) for line in note.body.split("\n"))
        out.append("")
        prev = chain

    return "\n".join(out).rstrip("\n") + "\n" if out else ""
