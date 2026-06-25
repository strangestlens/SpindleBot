"""
Tests for spindlebot.core.identity.

Uses a minimal hand-assembled FLAC whose STREAMINFO audio-MD5 is controllable,
so identity behaviour is exercised with zero real audio and no network.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.core.enums import IdentityKind
from spindlebot.core.identity import (
    KIND_AUDIO_MD5,
    KIND_FILE_SHA256,
    audio_content_id,
    audio_md5,
    file_sha256,
    sha256_bytes,
)


def test_identity_kind_closed_set():
    assert set(IdentityKind) == {IdentityKind.AUDIO_MD5, IdentityKind.FILE_SHA256}
    assert KIND_AUDIO_MD5 is IdentityKind.AUDIO_MD5
    assert KIND_FILE_SHA256 is IdentityKind.FILE_SHA256
    with pytest.raises(ValueError):
        IdentityKind("sha1")


def _write_flac(path: Path, *, audio_md5_bytes: bytes = b"\x00" * 16,
                tags: dict | None = None) -> None:
    """Write a minimal valid FLAC with a controllable STREAMINFO audio-MD5.

    The last 16 bytes of STREAMINFO are the decoded-audio MD5; mutagen surfaces
    them as FLAC.info.md5_signature and preserves them across a metadata save.
    """
    assert len(audio_md5_bytes) == 16
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"
        + struct.pack(">Q", (44100 << 44) | (0 << 41) | (15 << 36) | 0)
        + audio_md5_bytes
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(str(path))
    f.add_tags()
    for k, v in (tags or {}).items():
        f[k] = [v]
    f.save()


# ── audio_md5 ─────────────────────────────────────────────────────────────────

def test_audio_md5_reads_streaminfo_signature(tmp_path):
    sig = bytes(range(1, 17))  # 0102...10
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=sig)
    assert audio_md5(p) == sig.hex()


def test_audio_md5_zero_signature_returns_none(tmp_path):
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=b"\x00" * 16)
    assert audio_md5(p) is None


def test_audio_md5_non_flac_returns_none(tmp_path):
    p = tmp_path / "notflac.txt"
    p.write_bytes(b"this is not a flac file")
    assert audio_md5(p) is None


def test_audio_md5_missing_file_returns_none(tmp_path):
    assert audio_md5(tmp_path / "does-not-exist.flac") is None


# ── identity stability vs. integrity ──────────────────────────────────────────

def test_audio_md5_stable_across_retag_but_file_sha256_changes(tmp_path):
    sig = bytes(range(1, 17))
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=sig, tags={"title": "Original"})

    md5_before, sha_before = audio_md5(p), file_sha256(p)

    f = mutagen.flac.FLAC(str(p))
    f["title"] = ["Edited Title"]
    f.save()

    assert audio_md5(p) == md5_before          # identity survives re-tagging
    assert file_sha256(p) != sha_before        # integrity hash reflects the edit


def test_identical_files_share_both_hashes(tmp_path):
    sig = bytes(range(17, 33))
    a, b = tmp_path / "a.flac", tmp_path / "b.flac"
    _write_flac(a, audio_md5_bytes=sig, tags={"title": "Same"})
    b.write_bytes(a.read_bytes())
    assert audio_md5(a) == audio_md5(b)
    assert file_sha256(a) == file_sha256(b)


# ── file_sha256 / sha256_bytes ────────────────────────────────────────────────

def test_file_sha256_matches_hashlib(tmp_path):
    p = tmp_path / "blob.bin"
    data = b"spindlebot" * 1000
    p.write_bytes(data)
    assert file_sha256(p) == hashlib.sha256(data).hexdigest()


def test_sha256_bytes_known_vector():
    assert sha256_bytes(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


# ── audio_content_id (fallback) ───────────────────────────────────────────────

def test_content_id_prefers_audio_md5(tmp_path):
    sig = bytes(range(1, 17))
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=sig)
    cid = audio_content_id(p)
    assert cid.kind == KIND_AUDIO_MD5
    assert cid.value == sig.hex()


def test_content_id_falls_back_to_file_sha256_when_md5_absent(tmp_path):
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=b"\x00" * 16)
    cid = audio_content_id(p)
    assert cid.kind == KIND_FILE_SHA256
    assert cid.value == file_sha256(p)


def test_content_id_value_is_lowercase_hex(tmp_path):
    p = tmp_path / "track.flac"
    _write_flac(p, audio_md5_bytes=bytes(range(1, 17)))
    cid = audio_content_id(p)
    assert cid.value == cid.value.lower()
    int(cid.value, 16)  # parses as hex
