"""External-collection models — the common reduction every source adapts to.

A collection source (Discogs, a hand-written fixture, a future MusicBrainz
collection) knows a great deal about a release. The audit needs almost none of
it: enough to decide "is this the kind of thing I rip?" and "do I already have
it?". `CollectionItem` is that reduction, and a per-source transformer is the
only code allowed to know a source's quirks.

Pure: no I/O, no DB, no print.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from spindlebot.core.enums import MediaKind


@dataclass(frozen=True)
class CollectionItem:
    """One release in an external collection, reduced to what the audit needs.

    `artist_alts` / `title_alts` exist because sources name the same release
    several ways and the transformer is the only layer that knows which is
    which. Discogs, for instance, carries both a canonical artist name and an
    `anv` (the name as printed on the sleeve) — they genuinely differ in real
    collections, and either may be what the library was tagged with. Flattening
    them here keeps source quirks out of the matcher.

    Both alt tuples are alternates *in addition to* `artist`/`title`; the
    matcher tries the primary first and the alternates after.
    """
    source: str                      # provider name, e.g. "discogs"
    source_id: str                   # stable id within that source
    artist: str
    title: str
    media: frozenset[MediaKind] = field(default_factory=frozenset)
    year: int | None = None
    catno: str | None = None
    artist_alts: tuple[str, ...] = ()
    title_alts: tuple[str, ...] = ()
    mb_release_id: str | None = None  # None for Discogs; a free exact match when present
    url: str | None = None
    thumb_url: str | None = None

    @property
    def key(self) -> str:
        """Stable cross-source identity, e.g. `discogs:26936627`."""
        return f"{self.source}:{self.source_id}"

    @property
    def all_artists(self) -> tuple[str, ...]:
        return (self.artist, *self.artist_alts)

    @property
    def all_titles(self) -> tuple[str, ...]:
        return (self.title, *self.title_alts)


@dataclass(frozen=True)
class LibraryAlbum:
    """One album already in the digital library, from beets or the SpindleBot DB."""
    albumartist: str
    album: str
    year: int | None = None
    mb_albumid: str | None = None


def resolve_media(values) -> frozenset[MediaKind]:
    """Validate medium names into MediaKinds, failing loud on an unknown one.

    Config carries these as raw strings so that a typo can't break the whole
    pipeline at bootstrap; this is where they're checked, at the point of use.
    """
    if isinstance(values, str):
        values = [values]
    resolved = set()
    for value in values:
        name = str(value).strip().casefold()
        try:
            resolved.add(MediaKind(name))
        except ValueError:
            known = ", ".join(m.value for m in MediaKind)
            raise ValueError(
                f"unknown medium {name!r} (known: {known})"
            ) from None
    return frozenset(resolved)
