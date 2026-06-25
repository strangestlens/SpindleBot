"""
Content identity and integrity hashing.

Pure functions — no database, no side effects beyond reading the given file.

Two distinct notions of "hash", kept deliberately separate:

  * **audio identity** — the FLAC STREAMINFO MD5 of the *decoded audio*. Editing
    VorbisComments does not change it, so it is the stable identity of a track
    across re-tagging. Some encoders don't store it (the field is all-zero); in
    that case we fall back to the whole-file sha256, recording which kind was
    used so the fallback is explicit and a later re-encode can be re-identified.

  * **file integrity** — the sha256 of the *whole file's bytes*. Changes whenever
    anything in the file changes (including tags). Used to check whether a
    particular copy at a location is byte-for-byte intact — never as identity.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC

from spindlebot.core.enums import IdentityKind

_CHUNK = 1 << 20  # 1 MiB streaming read

# Backward-compatible aliases for the enum members.
KIND_AUDIO_MD5 = IdentityKind.AUDIO_MD5
KIND_FILE_SHA256 = IdentityKind.FILE_SHA256


@dataclass(frozen=True)
class ContentId:
    kind: IdentityKind
    value: str  # lowercase hex


def file_sha256(path: str | Path) -> str:
    """sha256 of the whole file's bytes, as lowercase hex."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """sha256 of an in-memory blob (sidecars: .lrc, cover.jpg), as lowercase hex."""
    return hashlib.sha256(data).hexdigest()


def audio_md5(path: str | Path) -> str | None:
    """
    Decoded-audio MD5 from a FLAC's STREAMINFO, as 32-char lowercase hex.

    Returns None when the file is not a readable FLAC, or when the encoder did
    not store an audio MD5 (the field is all-zero) — callers fall back to
    file_sha256.
    """
    try:
        info = FLAC(str(path)).info
    except (MutagenError, OSError):
        return None
    sig = getattr(info, "md5_signature", 0) or 0
    if sig == 0:
        return None
    return f"{sig:032x}"


def audio_content_id(path: str | Path) -> ContentId:
    """
    Stable identity for an audio file: the decoded-audio MD5 when available,
    otherwise the whole-file sha256 (tagged with its kind so the fallback is
    explicit).
    """
    md5 = audio_md5(path)
    if md5 is not None:
        return ContentId(IdentityKind.AUDIO_MD5, md5)
    return ContentId(IdentityKind.FILE_SHA256, file_sha256(path))
