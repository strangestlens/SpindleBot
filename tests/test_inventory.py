"""Tests for spindlebot.services.inventory (read-only scan → DB)."""
from __future__ import annotations

import struct
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.db.connection import open_db
from spindlebot.db.repositories import audio_repo, presence_repo
from spindlebot.services import inventory
from spindlebot.services.inventory import (
    ensure_pending_location,
    inventory_location,
)


def _write_flac(path: Path, *, audio_md5_bytes: bytes = b"\x00" * 16,
                tags: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


@pytest.fixture
def conn(tmp_path):
    c = open_db(tmp_path / "spindlebot.db")
    yield c
    c.close()


def _run(conn, root, now=1000):
    loc = ensure_pending_location(conn, now)
    return inventory_location(conn, location=loc, root=root, now=now), loc


def test_ensure_pending_location_idempotent(conn):
    a = ensure_pending_location(conn, 1)
    b = ensure_pending_location(conn, 2)
    assert a.id == b.id
    assert a.is_authoritative_audio is True
    assert a.is_retention is False


def test_inventory_populates_content_and_presence(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "Artist" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)),
                tags={"artist": "Artist", "album": "Album", "title": "One",
                      "tracknumber": "1", "discnumber": "1"})
    result, loc = _run(conn, root)

    assert result.scanned == 1 and result.new == 1 and result.updated == 0
    assert result.errors == 0

    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert audio is not None
    assert audio.identity_kind == "audio_md5"
    assert audio.artist == "Artist" and audio.title == "One" and audio.track_no == 1

    pres = presence_repo.get(conn, audio.id, loc.id)
    assert pres.present is True
    assert pres.rel_path == "Artist/Album/01.flac"
    assert pres.file_sha256 and pres.byte_size > 0


def test_inventory_is_idempotent(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))
    _run(conn, root, now=1000)
    result, _ = _run(conn, root, now=2000)
    assert result.scanned == 1 and result.new == 0 and result.updated == 1
    assert audio_repo.count(conn) == 1   # no duplicate content row


def test_inventory_fallback_identity_for_zero_md5(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=b"\x00" * 16)
    result, _ = _run(conn, root)
    assert result.new == 1
    rows = conn.execute("SELECT identity_kind FROM audio_content").fetchall()
    assert rows[0]["identity_kind"] == "file_sha256"


def test_inventory_ignores_non_audio_and_missing_root(conn, tmp_path):
    root = tmp_path / "Pending"
    root.mkdir()
    (root / "cover.jpg").write_bytes(b"not audio")
    (root / "album.log").write_text("rip log")
    result, _ = _run(conn, root)
    assert result.scanned == 0

    missing, _ = _run(conn, tmp_path / "nope")
    assert missing.scanned == 0


def test_inventory_isolates_per_file_errors(conn, tmp_path, monkeypatch):
    root = tmp_path / "Pending"
    _write_flac(root / "good.flac", audio_md5_bytes=bytes(range(1, 17)))
    _write_flac(root / "bad.flac", audio_md5_bytes=bytes(range(17, 33)))

    real = inventory.file_sha256

    def flaky(path):
        if Path(path).name == "bad.flac":
            raise OSError("simulated read failure")
        return real(path)

    monkeypatch.setattr(inventory, "file_sha256", flaky)
    result, _ = _run(conn, root)

    assert result.scanned == 2
    assert result.errors == 1
    assert any("bad.flac" in e for e in result.error_paths)
    assert result.new == 1   # the good one still landed
