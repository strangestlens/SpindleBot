"""Pure comparison keys; the original paths remain the approved I/O paths."""
from __future__ import annotations

import unicodedata


def path_key(path: str | None, *, case_sensitive: bool | None) -> str | None:
    """Fold canonical case aliases only when a location proves insensitivity."""
    if path is None or case_sensitive is not False:
        return path
    return unicodedata.normalize("NFC", path).casefold()
