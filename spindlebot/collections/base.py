"""Collection provider protocol + registry.

The interface is deliberately one method: a provider turns an account
identifier into collection items. Everything else — paging, auth, caching,
rate limiting, file formats — is a provider's private business.
"""
from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from spindlebot.core.collection import CollectionItem
from spindlebot.core.errors import UnknownProvider


@runtime_checkable
class CollectionProvider(Protocol):
    name: str

    def fetch(
        self, account: str, *, refresh: bool = False, cached_only: bool = False
    ) -> list[CollectionItem]:
        """Return every item in `account`'s collection.

        `refresh` bypasses any local cache. `cached_only` is the opposite
        promise: answer from local data or not at all, never from the network.
        Callers that only want labels use it so a bookkeeping command can't
        stall on HTTP. A provider with no remote (fixture) satisfies it for
        free.
        """
        ...


def _discogs(cfg) -> CollectionProvider:
    from spindlebot.collections.discogs import DiscogsProvider
    return DiscogsProvider.from_config(cfg)


def _fixture(cfg) -> CollectionProvider:
    from spindlebot.collections.fixture import FixtureProvider
    return FixtureProvider()


# name -> factory. Imports are lazy so selecting one provider never drags in
# another's dependencies.
PROVIDERS: dict[str, Callable[[object], CollectionProvider]] = {
    "discogs": _discogs,
    "fixture": _fixture,
}


def get_provider(name: str, cfg) -> CollectionProvider:
    try:
        factory = PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise UnknownProvider(
            f"unknown collection source {name!r} (known: {known})"
        ) from None
    return factory(cfg)
