"""Pure album-grouping identity. No DB, no I/O side effects."""
from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5


def album_key(
    albumartist: str | None,
    album: str | None,
    mb_albumid: str | None = None,
) -> str:
    """Deterministic grouping key for an album.

    Prefers the MusicBrainz release id when present (stable across re-tagging);
    otherwise falls back to a normalized albumartist + album pair. Built only
    from tag fields that survive pretag/posttag (notably NOT date, which posttag
    truncates to a year). The result is a stable opaque uuid, like location_uuid.
    """
    if mb_albumid and mb_albumid.strip():
        basis = f"mb:{mb_albumid.strip().lower()}"
    else:
        aa = (albumartist or "").strip().lower()
        al = (album or "").strip().lower()
        basis = f"aa:{aa}\x00{al}"
    return str(uuid5(NAMESPACE_URL, f"spindlebot:album:{basis}"))
