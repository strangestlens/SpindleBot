"""The ignore list: collection items you know are missing and don't want told.

Damaged discs, gifts you'll never rip, a release the matcher can't reach
(a library tagged in a different script than the collection lists it). Without
somewhere to put these, the missing list keeps a permanent floor of noise and
stops being worth opening — which is the whole failure mode this feature exists
to avoid.

Deliberately a JSON file, not a schema migration: the audit stays read-only
against the library, and an ignore list is a personal annotation, not a fact
about content identity.

Entries denormalize artist/title alongside the key. `discogs:26936627` alone is
unreadable a month later, and `--list` has to be answerable without a network
round-trip.

Ignoring is always reversible — `remove()` and the `--remove` flag exist because
the mistake this list invites is ignoring the wrong row.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from spindlebot.core.errors import SpindleBotError

STORE_VERSION = 1


class IgnoreStoreError(SpindleBotError):
    """The ignore store could not be read or written."""


@dataclass(frozen=True)
class IgnoredItem:
    key: str                  # "<source>:<source_id>"
    artist: str = ""
    title: str = ""
    reason: str = ""
    ignored_utc: int = 0

    @property
    def label(self) -> str:
        both = f"{self.artist} — {self.title}".strip(" —")
        return both or self.key

    def to_json(self) -> dict:
        return {
            "artist": self.artist,
            "title": self.title,
            "reason": self.reason,
            "ignored_utc": self.ignored_utc,
        }

    @classmethod
    def from_json(cls, key: str, raw: dict) -> IgnoredItem:
        return cls(
            key=key,
            artist=str(raw.get("artist") or ""),
            title=str(raw.get("title") or ""),
            reason=str(raw.get("reason") or ""),
            ignored_utc=int(raw.get("ignored_utc") or 0),
        )


@dataclass
class IgnoreStore:
    """Keyed set of ignored collection items, persisted as JSON."""

    path: Path
    items: dict = None  # dict[str, IgnoredItem]

    def __post_init__(self) -> None:
        if self.items is None:
            self.items = {}

    # ── persistence ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> IgnoreStore:
        """Read the store. A missing file is an empty store, not an error."""
        path = Path(path).expanduser()
        if not path.exists():
            return cls(path=path, items={})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise IgnoreStoreError(
                f"ignore list at {path} is not valid JSON: {e}"
            ) from e
        except OSError as e:
            raise IgnoreStoreError(f"could not read ignore list at {path}: {e}") from e

        raw = payload.get("ignored") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            raise IgnoreStoreError(
                f"ignore list at {path} is malformed (expected an 'ignored' object)"
            )
        return cls(
            path=path,
            items={k: IgnoredItem.from_json(k, v if isinstance(v, dict) else {})
                   for k, v in raw.items()},
        )

    def save(self) -> None:
        """Write the store atomically — a half-written ignore list is worse
        than none, since the next run silently un-ignores things."""
        payload = {
            "version": STORE_VERSION,
            "ignored": {k: v.to_json() for k, v in sorted(self.items.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            raise IgnoreStoreError(f"could not write ignore list at {self.path}: {e}") from e

    # ── queries ──────────────────────────────────────────────────────────────

    def __contains__(self, key: str) -> bool:
        return key in self.items

    def __len__(self) -> int:
        return len(self.items)

    def listing(self) -> list:
        """Ignored items, newest first."""
        return sorted(self.items.values(), key=lambda i: (-i.ignored_utc, i.key))

    # ── mutation ─────────────────────────────────────────────────────────────

    def add(self, key: str, *, artist: str = "", title: str = "",
            reason: str = "", now: int | None = None) -> IgnoredItem:
        """Ignore a key. Re-ignoring refreshes the reason but keeps the
        original timestamp — when you first decided is the interesting fact."""
        existing = self.items.get(key)
        item = IgnoredItem(
            key=key,
            artist=artist or (existing.artist if existing else ""),
            title=title or (existing.title if existing else ""),
            reason=reason or (existing.reason if existing else ""),
            ignored_utc=existing.ignored_utc if existing else (
                now if now is not None else int(time.time())
            ),
        )
        self.items[key] = item
        return item

    def remove(self, key: str) -> IgnoredItem | None:
        """Un-ignore a key. Returns the removed entry, or None if not ignored."""
        return self.items.pop(key, None)

    def clear(self) -> int:
        count = len(self.items)
        self.items = {}
        return count


def resolve_key(token: str, *, source: str) -> str:
    """Accept a full `source:id` key or a bare id, and return the full key.

    The audit prints full keys, but nobody wants to retype `discogs:` — and a
    bare id is what you get from a Discogs URL.
    """
    token = token.strip()
    if not token:
        raise ValueError("empty id")
    return token if ":" in token else f"{source}:{token}"
