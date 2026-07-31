"""Fixture collection provider — a collection you write by hand.

`account` is the path to a JSON file. Two jobs, both real:

  * tests get a provider with no network and no mocking ceremony;
  * anyone without a Discogs account (or auditing a shelf they've listed
    themselves) gets a supported path in without writing a provider.

Accepts either a bare list of rows or `{"items": [...]}`. Every field except
`title` is optional; `id` defaults to the row's position.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from spindlebot.core.collection import CollectionItem
from spindlebot.core.enums import MediaKind
from spindlebot.core.errors import CollectionFetchError


def _media(raw) -> frozenset[MediaKind]:
    if raw is None:
        return frozenset({MediaKind.CD})
    if isinstance(raw, str):
        raw = [raw]
    out = set()
    for value in raw:
        try:
            out.add(MediaKind(str(value).strip().casefold()))
        except ValueError:
            out.add(MediaKind.OTHER)
    return frozenset(out)


def _year(raw, *, title: str) -> int | None:
    """Coerce a hand-written year. `"1991"` is as valid as `1991` here."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text) or None
    except ValueError:
        raise CollectionFetchError(
            f"invalid year {raw!r} for {title!r} — expected a number like 1991"
        ) from None


def _strs(raw) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(v) for v in raw if str(v).strip())


def to_items(rows: list[dict], *, source: str = "fixture") -> list[CollectionItem]:
    """Map fixture rows to CollectionItems. Pure."""
    items = []
    for index, row in enumerate(rows):
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        items.append(CollectionItem(
            source=source,
            source_id=str(row.get("id", index)),
            artist=str(row.get("artist") or "").strip(),
            title=title,
            media=_media(row.get("media")),
            year=_year(row.get("year"), title=title),
            catno=(str(row["catno"]) if row.get("catno") else None),
            artist_alts=_strs(row.get("artist_alts")),
            title_alts=_strs(row.get("title_alts")),
            mb_release_id=(str(row["mb_release_id"]) if row.get("mb_release_id") else None),
            url=(str(row["url"]) if row.get("url") else None),
            thumb_url=(str(row["thumb_url"]) if row.get("thumb_url") else None),
        ))
    return items


@dataclass
class FixtureProvider:
    name: str = "fixture"

    def fetch(
        self, account: str, *, refresh: bool = False, cached_only: bool = False
    ) -> list[CollectionItem]:
        # No remote to avoid: reading the file IS the local path.
        path = Path(account).expanduser()
        if not path.exists():
            raise CollectionFetchError(f"fixture collection not found: {path}")
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise CollectionFetchError(f"fixture collection is not valid JSON: {path}") from e
        rows = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise CollectionFetchError(
                f"fixture collection must be a list of items or {{\"items\": [...]}}: {path}"
            )
        return to_items(rows)
