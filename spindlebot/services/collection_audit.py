"""Audit an external collection against the digital library.

Orchestration only: pick a provider, fetch items, filter by medium, match
against the library, bucket the results. Purely assistive — reads the library,
touches nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from spindlebot.collections.base import get_provider
from spindlebot.core.collection import CollectionItem, LibraryAlbum
from spindlebot.core.collection_match import ItemMatch, MatchStatus, match_items
from spindlebot.core.enums import MediaKind
from spindlebot.services import library_index

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

    def _of(self, status: MatchStatus) -> list[ItemMatch]:
        return [m for m in self.matches if m.status is status]

    @property
    def owned(self) -> list[ItemMatch]:
        return self._of(MatchStatus.OWNED)

    @property
    def uncertain(self) -> list[ItemMatch]:
        return self._of(MatchStatus.UNCERTAIN)

    @property
    def missing(self) -> list[ItemMatch]:
        return self._of(MatchStatus.MISSING)


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
    index: str = "beets",
    strict: bool = False,
    provider=None,
    library: list[LibraryAlbum] | None = None,
) -> AuditReport:
    """Fetch, filter, and match a collection against the library.

    `provider` and `library` are injection points for tests; in normal use both
    are resolved from config.
    """
    provider = provider or get_provider(source, cfg)
    items = provider.fetch(account, refresh=refresh)
    considered = filter_media(items, media)

    albums = library_index.load(cfg, index) if library is None else library
    matches = sorted(match_items(considered, albums), key=_sort_key)

    if strict:
        # Fold uncertainty into the actionable list — for when you'd rather
        # re-check a shelf than miss a gap.
        matches = [
            replace(m, status=MatchStatus.MISSING)
            if m.status is MatchStatus.UNCERTAIN else m
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
    )
