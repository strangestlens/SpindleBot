"""Tests for spindlebot.services.inventory (read-only scan → DB)."""
from __future__ import annotations

import struct
from pathlib import Path

import mutagen.flac
import pytest

import os
import sqlite3

from spindlebot.core.albums import album_key
from spindlebot.core.enums import SidecarParentKind, SidecarRole
from spindlebot.db.connection import open_db
from spindlebot.db.repositories import (
    album_repo,
    audio_repo,
    presence_repo,
    scan_repo,
    sidecar_presence_repo,
    sidecar_repo,
)
from spindlebot.services import inventory, volumes
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


def test_scan_status_closed_set():
    from spindlebot.core.enums import ScanStatus
    assert set(ScanStatus) == {
        ScanStatus.RUNNING, ScanStatus.OK, ScanStatus.INTERRUPTED, ScanStatus.ERROR
    }
    with pytest.raises(ValueError):
        ScanStatus("done")


def test_inventory_records_interrupted_on_unexpected_error(conn, tmp_path, monkeypatch):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))
    loc = ensure_pending_location(conn, 1000)

    def boom(_path):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(inventory, "audio_content_id", boom)
    with pytest.raises(RuntimeError):
        inventory_location(conn, location=loc, root=root, now=1000)

    scan = scan_repo.latest_scan(conn, loc.id)
    assert scan["status"] == "interrupted"


def test_inventory_writes_marker_and_records_scan(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))
    result, loc = _run(conn, root, now=1234)

    assert volumes.read_marker(root) == loc.uuid
    scan = scan_repo.latest_scan(conn, loc.id)
    assert scan["status"] == "ok"
    assert scan["files_seen"] == result.scanned == 1
    assert scan["finished_utc"] == 1234


def test_inventory_links_beets_item_id(conn, tmp_path):
    root = tmp_path / "Pending"
    flac = root / "Artist" / "Album" / "01.flac"
    _write_flac(flac, audio_md5_bytes=bytes(range(1, 17)))

    beets_db = tmp_path / "beets.db"
    bconn = sqlite3.connect(beets_db)
    bconn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
    bconn.execute("INSERT INTO items (id, path) VALUES (?, ?)",
                  (77, os.fsencode(str(flac))))
    bconn.commit()
    bconn.close()

    loc = ensure_pending_location(conn, 1000)
    inventory_location(conn, location=loc, root=root, now=1000, beets_db=beets_db)

    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert audio.beets_item_id == 77


def test_inventory_missing_beets_db_is_harmless(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))
    loc = ensure_pending_location(conn, 1000)
    result = inventory_location(conn, location=loc, root=root, now=1000,
                                beets_db=tmp_path / "nonexistent.db")
    assert result.new == 1
    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert audio.beets_item_id is None


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


# ── albums + sidecars ─────────────────────────────────────────────────────────

def _album_tags(title, track):
    return {"albumartist": "AA", "artist": "AA", "album": "Album",
            "title": title, "tracknumber": str(track), "discnumber": "1"}


def test_inventory_groups_tracks_into_one_album(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    _write_flac(root / "AA" / "Album" / "02.flac",
                audio_md5_bytes=bytes(range(17, 33)), tags=_album_tags("Two", 2))
    result, _ = _run(conn, root)

    assert result.albums == 1
    assert album_repo.count(conn) == 1
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert al is not None and len(album_repo.list_track_ids(conn, al.id)) == 2


def test_inventory_skips_album_when_no_album_tag(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)),
                tags={"artist": "AA", "title": "Loose"})
    result, _ = _run(conn, root)
    assert result.albums == 0 and album_repo.count(conn) == 0


def test_inventory_records_lrc_paired_to_its_track(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]hello\n")
    result, loc = _run(conn, root)

    assert result.sidecars == 1 and result.sidecars_new == 1
    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    sc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                          parent_id=audio.id, role=SidecarRole.LRC)
    assert sc is not None
    pres = sidecar_presence_repo.get(conn, sc.id, loc.id)
    assert pres.present is True
    assert pres.rel_path == "AA/Album/01.lrc"
    assert pres.file_sha256 == sc.sha256 and pres.byte_size > 0


def test_inventory_partial_lrc_coverage(conn, tmp_path):
    # An album where only some tracks have lyrics: each .lrc is its own track row,
    # the others get nothing, and no album-level .nolrc is implied.
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    _write_flac(root / "AA" / "Album" / "02.flac",
                audio_md5_bytes=bytes(range(17, 33)), tags=_album_tags("Two", 2))
    _write_flac(root / "AA" / "Album" / "03.flac",
                audio_md5_bytes=bytes(range(33, 49)), tags=_album_tags("Three", 3))
    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]a\n")
    (root / "AA" / "Album" / "03.lrc").write_text("[00:01.00]c\n")
    result, _ = _run(conn, root)

    assert result.albums == 1
    assert result.sidecars == 2          # only the two present .lrc files
    track1 = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    track2 = audio_repo.get_by_identity(conn, bytes(range(17, 33)).hex())
    track3 = audio_repo.get_by_identity(conn, bytes(range(33, 49)).hex())
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                            parent_id=track1.id, role=SidecarRole.LRC) is not None
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                            parent_id=track2.id, role=SidecarRole.LRC) is None
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                            parent_id=track3.id, role=SidecarRole.LRC) is not None
    # no album-level nolrc was created
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                            parent_id=al.id, role=SidecarRole.NOLRC) is None


def test_inventory_orphan_lrc_is_skipped(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    (root / "AA" / "Album" / "99.lrc").write_text("no matching track\n")
    result, _ = _run(conn, root)
    assert result.sidecars == 0
    assert sidecar_repo.count(conn) == 0


def test_inventory_records_cover_and_nolrc_at_album_level(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    (root / "AA" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    (root / "AA" / "Album" / ".nolrc").write_bytes(b"")
    result, loc = _run(conn, root)

    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    roles = {s.role for s in sidecar_repo.list_for_parent(
        conn, parent_kind=SidecarParentKind.ALBUM, parent_id=al.id)}
    assert roles == {SidecarRole.COVER, SidecarRole.NOLRC}
    assert result.sidecars == 2
    cover = sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                             parent_id=al.id, role=SidecarRole.COVER)
    assert sidecar_presence_repo.get(conn, cover.id, loc.id).rel_path == "AA/Album/cover.jpg"


def test_inventory_cover_resolves_through_disc_subfolders(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "Disc 1" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    _write_flac(root / "AA" / "Album" / "Disc 2" / "01.flac",
                audio_md5_bytes=bytes(range(17, 33)), tags=_album_tags("Two", 1))
    (root / "AA" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    result, _ = _run(conn, root)

    assert result.albums == 1 and result.sidecars == 1
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                            parent_id=al.id, role=SidecarRole.COVER) is not None


def test_inventory_multidisc_sibling_folders_collapse_to_one_album(conn, tmp_path):
    # Real DwRugged layout: multidisc = sibling album folders ("Album [Disc 1]",
    # "Album [Disc 2]"), tracks + a cover directly inside each, sharing one album
    # tag. They must collapse to a single album keyed on tags, not folder names.
    root = tmp_path / "Pending"
    d1 = root / "AA" / "Album [Disc 1]"
    d2 = root / "AA" / "Album [Disc 2]"
    _write_flac(d1 / "01.flac", audio_md5_bytes=bytes(range(1, 17)),
                tags={"albumartist": "AA", "album": "Album", "title": "a",
                      "tracknumber": "1", "discnumber": "1"})
    _write_flac(d2 / "01.flac", audio_md5_bytes=bytes(range(17, 33)),
                tags={"albumartist": "AA", "album": "Album", "title": "b",
                      "tracknumber": "1", "discnumber": "2"})
    (d1 / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    (d2 / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    result, loc = _run(conn, root)

    assert result.albums == 1
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert len(album_repo.list_track_ids(conn, al.id)) == 2
    # both disc covers map to the single (album, cover) sidecar — one row
    covers = [s for s in sidecar_repo.list_for_parent(
        conn, parent_kind=SidecarParentKind.ALBUM, parent_id=al.id)
        if s.role is SidecarRole.COVER]
    assert len(covers) == 1
    pres = sidecar_presence_repo.get(conn, covers[0].id, loc.id)
    assert pres.present is True and pres.rel_path.endswith("cover.jpg")


def test_inventory_ambiguous_cover_is_skipped(conn, tmp_path):
    root = tmp_path / "Pending"
    # two distinct albums sharing one directory → which album owns cover.jpg? skip.
    _write_flac(root / "mix" / "01.flac", audio_md5_bytes=bytes(range(1, 17)),
                tags={"albumartist": "AA", "album": "Alpha", "title": "x",
                      "tracknumber": "1"})
    _write_flac(root / "mix" / "02.flac", audio_md5_bytes=bytes(range(17, 33)),
                tags={"albumartist": "BB", "album": "Beta", "title": "y",
                      "tracknumber": "1"})
    (root / "mix" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    result, _ = _run(conn, root)

    assert result.albums == 2 and result.sidecars == 0
    assert sidecar_repo.count(conn) == 0


def test_inventory_sidecar_idempotent_then_tracks_content_change(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    cover = root / "AA" / "Album" / "cover.jpg"
    cover.write_bytes(b"\xff\xd8original")
    _run(conn, root, now=1000)

    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    sc1 = sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                           parent_id=al.id, role=SidecarRole.COVER)

    cover.write_bytes(b"\xff\xd8edited-bytes")
    result, _ = _run(conn, root, now=2000)

    assert result.sidecars == 1 and result.sidecars_updated == 1 and result.sidecars_new == 0
    sc2 = sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                           parent_id=al.id, role=SidecarRole.COVER)
    assert sc2.id == sc1.id                 # same sidecar identity
    assert sc2.sha256 != sc1.sha256         # content hash followed the edit
    assert sidecar_repo.count(conn) == 1
