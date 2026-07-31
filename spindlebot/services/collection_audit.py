"""Audit an external collection against the digital library.

Orchestration only: pick a provider, fetch items, filter by medium, match
against the library, bucket the results. Purely assistive — reads the library,
touches nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from spindlebot.collections.base import get_provider
from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.collection_match import ItemMatch, MatchStatus, match_items
from spindlebot.core.enums import MediaKind
from spindlebot.services import library_index
from spindlebot.services.collection_ignore import IgnoreStore

DEFAULT_MEDIA = frozenset({MediaKind.CD})


@dataclass(frozen=True)
class AuditReport:
    source: str
    account: str
    media: frozenset[MediaKind]
    fetched: int          # items in the collection
    considered: int       # items left after the media filter
    library_albums: int
    matches: list[ItemMatch]
    # Which index contributed what. Surfaced everywhere the report is rendered:
    # when an album is wrongly called missing, the index is the first suspect.
    library_sources: dict = field(default_factory=dict)
    library_errors: dict = field(default_factory=dict)

    def _of(self, status: MatchStatus) -> list[ItemMatch]:
        # Ignored items are held out of the actionable buckets — that is the
        # entire point of ignoring them — but keep their real match status, so
        # un-ignoring restores the original verdict with nothing recomputed.
        return [m for m in self.matches if m.status is status and not m.ignored]

    @property
    def owned(self) -> list[ItemMatch]:
        return self._of(MatchStatus.OWNED)

    @property
    def uncertain(self) -> list[ItemMatch]:
        return self._of(MatchStatus.UNCERTAIN)

    @property
    def missing(self) -> list[ItemMatch]:
        return self._of(MatchStatus.MISSING)

    @property
    def ignored(self) -> list[ItemMatch]:
        return [m for m in self.matches if m.ignored]


def filter_media(
    items: list[CollectionItem], media: frozenset[MediaKind]
) -> list[CollectionItem]:
    """Keep items released on any of `media`. An empty filter keeps everything."""
    if not media:
        return list(items)
    return [i for i in items if i.media & media]


def _sort_key(match: ItemMatch) -> tuple[str, str]:
    return (match.item.artist.casefold(), match.item.title.casefold())


def run_audit(
    cfg,
    *,
    account: str,
    source: str = "discogs",
    media: frozenset[MediaKind] = DEFAULT_MEDIA,
    refresh: bool = False,
    index: str = "auto",
    strict: bool = False,
    provider=None,
    library: list[LibraryAlbum] | None = None,
    ignore=None,
) -> AuditReport:
    """Fetch, filter, and match a collection against the library.

    `provider` and `library` are injection points for tests; in normal use both
    are resolved from config.
    """
    provider = provider or get_provider(source, cfg)
    items = provider.fetch(account, refresh=refresh)
    considered = filter_media(items, media)

    if library is None:
        loaded = library_index.load(cfg, index)
        albums, sources, errors = loaded.albums, loaded.counts, loaded.errors
    else:
        albums, sources, errors = library, {"injected": len(library)}, {}
    matches = sorted(match_items(considered, albums), key=_sort_key)

    if strict:
        # Fold uncertainty into the actionable list — for when you'd rather
        # re-check a shelf than miss a gap.
        matches = [
            replace(m, status=MatchStatus.MISSING)
            if m.status is MatchStatus.UNCERTAIN else m
            for m in matches
        ]

    if ignore is None:
        ignore = IgnoreStore.load(cfg.collection.ignore_path)
    # An owned album is never "ignored" — if you ignored it and later ripped it,
    # the rip wins and the stale entry stops mattering without any cleanup.
    matches = [
        replace(m, ignored=True)
        if m.status is not MatchStatus.OWNED and m.item.key in ignore else m
        for m in matches
    ]

    return AuditReport(
        source=source,
        account=account,
        media=media,
        fetched=len(items),
        considered=len(considered),
        library_albums=len(albums),
        matches=matches,
        library_sources=sources,
        library_errors=errors,
    )
