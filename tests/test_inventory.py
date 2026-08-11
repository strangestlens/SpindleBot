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
    action_repo,
    album_repo,
    audio_repo,
    location_repo,
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


def test_inventory_skips_appledouble_files(conn, tmp_path):
    # macOS writes ._<name> AppleDouble companions onto exFAT/FAT (DAP cards).
    # They carry a real file's extension but are resource-fork junk — must be
    # skipped before classification, never hashed or recorded.
    root = tmp_path / "M0Pro"
    album = root / "Artist" / "Album"
    _write_flac(album / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)),
                tags={"artist": "Artist", "album": "Album", "title": "One"})
    (album / "cover.jpg").write_bytes(b"real cover")
    # AppleDouble companions — sort before their real counterparts, so a naive
    # walk would hit these first (this is what crashed the M0Pro scan at 0/N).
    (album / "._01.flac").write_bytes(b"Mac OS X\x00\x00\x00\x02")
    (album / "._cover.jpg").write_bytes(b"Mac OS X\x00\x00\x00\x02")
    (album / "._01.lrc").write_bytes(b"Mac OS X\x00\x00\x00\x02")

    result, loc = _run(conn, root)

    assert result.scanned == 1 and result.new == 1 and result.errors == 0
    assert audio_repo.count(conn) == 1
    assert result.sidecars == 1  # only the real cover.jpg, not ._cover.jpg / ._01.lrc

    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert audio is not None and audio.title == "One"


def test_read_tags_returns_full_shape_for_unreadable_file(tmp_path):
    # The contract that KeyError'd the M0Pro scan: an unreadable audio file must
    # yield the full key set (all None), never a dict missing keys.
    bad = tmp_path / "garbage.flac"
    bad.write_bytes(b"this is not a flac file")
    tags = inventory._read_tags(bad)
    assert set(tags) == {"artist", "albumartist", "album", "title",
                         "disc_no", "track_no", "duration_s", "mb_albumid"}
    assert all(v is None for v in tags.values())


def test_inventory_does_not_abort_on_unreadable_audio_file(conn, tmp_path):
    # Regression for the KeyError crash: a mutagen-unreadable file (sorted first)
    # must not abort the whole scan — the following good track still records.
    root = tmp_path / "Pending"
    (root / "Artist" / "Album").mkdir(parents=True)
    (root / "Artist" / "Album" / "00_bad.flac").write_bytes(b"not a real flac")
    _write_flac(root / "Artist" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)),
                tags={"artist": "Artist", "album": "Album", "title": "One"})

    result, _ = _run(conn, root)

    # Both files scanned; the good one is fully tagged. The bad one falls back to
    # a file_sha256 identity (a real file whose bytes we still track), tags None.
    assert result.scanned == 2 and result.errors == 0
    good = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert good is not None and good.title == "One"


def test_inventory_tolerates_partial_tag_dict(conn, tmp_path, monkeypatch):
    # Belt-and-suspenders: if _read_tags ever regresses to a partial dict, the
    # caller merges over the empty shape so indexing can't KeyError — the track
    # is recorded (with the missing tags None), no error, scan continues. We do
    # NOT broadly catch KeyError, so unrelated bugs still surface as errors.
    root = tmp_path / "Pending"
    _write_flac(root / "Artist" / "Album" / "00_first.flac",
                audio_md5_bytes=bytes(range(1, 17)),
                tags={"artist": "A", "album": "Album", "title": "First"})
    _write_flac(root / "Artist" / "Album" / "01_second.flac",
                audio_md5_bytes=bytes(range(17, 33)),
                tags={"artist": "A", "album": "Album", "title": "Second"})

    real_read_tags = inventory._read_tags

    def flaky(path):
        if path.name == "00_first.flac":
            return {}  # simulate the old contract violation (missing keys)
        return real_read_tags(path)

    monkeypatch.setattr(inventory, "_read_tags", flaky)
    result, _ = _run(conn, root)

    assert result.scanned == 2 and result.errors == 0
    # Both tracks recorded; the partial-dict one has None tags but is not dropped.
    first = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    assert first is not None and first.title is None
    assert audio_repo.get_by_identity(conn, bytes(range(17, 33)).hex()) is not None


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
    assert scan["started_utc"] == 1234
    # finished_utc is read from the clock, NOT the injected start — a scan that
    # reports zero duration cannot be told apart from a full re-hash.
    assert scan["finished_utc"] > scan["started_utc"]


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


def test_inventory_per_track_nolrc_parents_to_track(conn, tmp_path):
    # A per-track <base>.nolrc marker (definitive miss) is a track-parented NOLRC
    # sidecar, matched by stem like a .lrc — NOT an album-level marker.
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    _write_flac(root / "AA" / "Album" / "02.flac",
                audio_md5_bytes=bytes(range(17, 33)), tags=_album_tags("Two", 2))
    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]a\n")
    (root / "AA" / "Album" / "02.nolrc").write_bytes(b"")
    result, _ = _run(conn, root)

    assert result.sidecars == 2
    track1 = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    track2 = audio_repo.get_by_identity(conn, bytes(range(17, 33)).hex())
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                            parent_id=track1.id, role=SidecarRole.LRC) is not None
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                            parent_id=track2.id, role=SidecarRole.NOLRC) is not None
    # It is NOT attached at the album level.
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                            parent_id=al.id, role=SidecarRole.NOLRC) is None


def test_inventory_orphan_per_track_nolrc_is_skipped(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    (root / "AA" / "Album" / "99.nolrc").write_bytes(b"")
    result, _ = _run(conn, root)
    assert result.sidecars == 0
    assert sidecar_repo.count(conn) == 0


def test_inventory_registers_orphan_lrc_linked_to_existing_track(conn, tmp_path):
    # The real gap: audio is synced elsewhere and pruned from here, then a late
    # .lrc lands. On re-inventory the track is no longer on disk (in-scan stem
    # index misses it), but the audio_content is already in the DB from the first
    # scan — so the orphan .lrc must attach to it, not be skipped.
    root = tmp_path / "Pending"
    flac = root / "AA" / "Album" / "01.flac"
    _write_flac(flac, audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    result1, loc = _run(conn, root)
    assert result1.sidecars == 0

    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]hello\n")
    flac.unlink()  # audio pruned; only the late lyric remains
    result2, _ = _run(conn, root)

    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    sc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                          parent_id=audio.id, role=SidecarRole.LRC)
    assert sc is not None
    pres = sidecar_presence_repo.get(conn, sc.id, loc.id)
    assert pres is not None and pres.present is True
    assert pres.rel_path == "AA/Album/01.lrc"
    assert result2.sidecars == 1 and result2.sidecars_new == 1


def test_inventory_registers_orphan_sidecars_from_another_location(conn, tmp_path):
    # Audio lives (and stays) on a retention location; a separate location holds
    # only late sidecars whose parents were never scanned there. Both the
    # track-level .lrc and the album-level cover.jpg must link to the identities
    # recorded from the other location.
    rugged_root = tmp_path / "DwRugged"
    _write_flac(rugged_root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    rugged = location_repo.upsert(conn, uuid="rugged", name="DwRugged",
                                  kind="local_drive", is_retention=True)
    inventory_location(conn, location=rugged, root=rugged_root, now=1000)

    pending_root = tmp_path / "Pending"
    (pending_root / "AA" / "Album").mkdir(parents=True)
    (pending_root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]hi\n")
    (pending_root / "AA" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    result, pending = _run(conn, pending_root)

    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    lrc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                           parent_id=audio.id, role=SidecarRole.LRC)
    assert lrc is not None
    assert sidecar_presence_repo.get(conn, lrc.id, pending.id).present is True

    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    cover = sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                             parent_id=al.id, role=SidecarRole.COVER)
    assert cover is not None
    assert sidecar_presence_repo.get(conn, cover.id, pending.id).present is True
    assert result.sidecars == 2


def test_inventory_orphan_sidecar_with_unknown_album_is_skipped(conn, tmp_path):
    # A .lrc and cover.jpg whose album/track the DB has never seen: there is
    # nothing to link them to, so both are skipped without error (logged).
    root = tmp_path / "Pending"
    (root / "Ghost" / "Album").mkdir(parents=True)
    (root / "Ghost" / "Album" / "01.lrc").write_text("[00:01.00]x\n")
    (root / "Ghost" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    result, _ = _run(conn, root)
    assert result.sidecars == 0
    assert sidecar_repo.count(conn) == 0


def test_inventory_orphan_lrc_ambiguous_parent_is_skipped(conn, tmp_path):
    # The same rel_path is present for two DISTINCT audio_ids (a re-rip to
    # different decoded audio, present at that path on two locations). There is
    # no single correct parent, so the orphan .lrc is skipped, never mis-attached.
    from spindlebot.core.identity import ContentId
    a = audio_repo.upsert(conn, ContentId("audio_md5", "a" * 32), now=1000)
    b = audio_repo.upsert(conn, ContentId("audio_md5", "b" * 32), now=1000)
    loc_a = location_repo.upsert(conn, uuid="la", name="LocA", kind="local_drive")
    loc_b = location_repo.upsert(conn, uuid="lb", name="LocB", kind="local_drive")
    for aud, loc in ((a, loc_a), (b, loc_b)):
        presence_repo.set_presence(conn, audio_id=aud.id, location_id=loc.id,
                                   present=True, observed_utc=1000,
                                   rel_path="AA/Album/01.flac")

    root = tmp_path / "Pending"
    (root / "AA" / "Album").mkdir(parents=True)
    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]hi\n")
    result, _ = _run(conn, root)
    assert result.sidecars == 0
    assert sidecar_repo.count(conn) == 0


def test_registered_orphan_lrc_yields_a_reconciler_copy(conn, tmp_path):
    # End-to-end: the orphan .lrc registered against DwRugged's audio is now
    # visible to the reconciler, which proposes copying it back to retention.
    from spindlebot.core.enums import ActionKind
    from spindlebot.services.reconciler import reconcile_location

    rugged_root = tmp_path / "DwRugged"
    _write_flac(rugged_root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    rugged = location_repo.upsert(conn, uuid="rugged", name="DwRugged",
                                  kind="local_drive", is_retention=True)
    inventory_location(conn, location=rugged, root=rugged_root, now=1000)

    pending_root = tmp_path / "Pending"
    (pending_root / "AA" / "Album").mkdir(parents=True)
    (pending_root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]hi\n")
    pending = location_repo.upsert(conn, uuid="pending", name="Pending",
                                   kind="library", is_authoritative_audio=True)
    inventory_location(conn, location=pending, root=pending_root, now=1000)

    result = reconcile_location(conn, target=rugged,
                                source_locations=[pending], now=2000)
    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    lrc = sidecar_repo.get(conn, parent_kind=SidecarParentKind.TRACK,
                           parent_id=audio.id, role=SidecarRole.LRC)
    sidecar_copies = [a for a in action_repo.list_for_run(conn, result.run_id)
                      if a.action_kind == ActionKind.COPY and a.content_kind == "sidecar"]
    assert len(sidecar_copies) == 1
    assert sidecar_copies[0].content_id == lrc.id
    assert sidecar_copies[0].dest_location_id == rugged.id


def test_inventory_bare_nolrc_still_album_level(conn, tmp_path):
    # The bare .nolrc (no stem) remains an album-level marker even now that
    # per-track <base>.nolrc exists.
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    (root / "AA" / "Album" / ".nolrc").write_bytes(b"")
    result, _ = _run(conn, root)

    assert result.sidecars == 1
    al = album_repo.get_by_key(conn, album_key("AA", "Album"))
    assert sidecar_repo.get(conn, parent_kind=SidecarParentKind.ALBUM,
                            parent_id=al.id, role=SidecarRole.NOLRC) is not None


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


def test_inventory_checkpoints_every_n_files(conn, tmp_path):
    root = tmp_path / "Pending"
    for i in range(1, 5):
        _write_flac(root / f"{i:02d}.flac", audio_md5_bytes=bytes([i]) * 16)
    calls = []
    loc = ensure_pending_location(conn, 1000)
    inventory_location(conn, location=loc, root=root, now=1000,
                       checkpoint=lambda: calls.append(1), commit_every=2)
    # 4 audio files, commit every 2 → checkpoints at done=2 and done=4
    assert len(calls) == 2


def test_inventory_partial_progress_survives_interrupt(tmp_path, monkeypatch):
    # With batched commits, a scan killed mid-way keeps what it already wrote —
    # the regression guard against the old commit-once-at-the-end behavior.
    db = tmp_path / "spindlebot.db"
    conn = open_db(db)
    root = tmp_path / "Pending"
    for i in range(1, 5):
        _write_flac(root / f"{i:02d}.flac", audio_md5_bytes=bytes([i]) * 16)

    calls = {"n": 0}
    real = inventory.audio_content_id

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 3:                 # blow up on the third file
            raise RuntimeError("killed mid-scan")
        return real(path)

    monkeypatch.setattr(inventory, "audio_content_id", flaky)
    loc = ensure_pending_location(conn, 1000)
    conn.commit()
    with pytest.raises(RuntimeError):
        inventory_location(conn, location=loc, root=root, now=1000,
                           checkpoint=conn.commit, commit_every=1)
    conn.close()

    # a fresh connection sees the two files committed before the crash
    other = open_db(db)
    assert other.execute("SELECT COUNT(*) FROM audio_presence").fetchone()[0] == 2
    other.close()


def test_inventory_emits_progress_events(conn, tmp_path):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)), tags=_album_tags("One", 1))
    _write_flac(root / "AA" / "Album" / "02.flac",
                audio_md5_bytes=bytes(range(17, 33)), tags=_album_tags("Two", 2))
    (root / "AA" / "Album" / "01.lrc").write_text("[00:01.00]a\n")
    (root / "AA" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")

    events = []
    loc = ensure_pending_location(conn, 1000)
    inventory_location(conn, location=loc, root=root, now=1000,
                       progress=events.append)

    # one initial "scan" event + one per file (2 audio + 2 sidecars)
    assert events[0].phase == "scan" and events[0].done == 0
    assert events[0].total == 4
    assert events[0].total_bytes > 0
    # done climbs monotonically and ends at the total
    dones = [e.done for e in events]
    assert dones == sorted(dones)
    assert events[-1].done == events[-1].total == 4
    # audio phase precedes sidecar phase; bytes only advance during audio
    phases = [e.phase for e in events[1:]]
    assert phases == ["audio", "audio", "sidecar", "sidecar"]
    assert events[-1].done_bytes == events[0].total_bytes  # all audio bytes counted
    assert any(e.current.endswith(".flac") for e in events)


def test_inventory_progress_optional_and_safe(conn, tmp_path):
    # no callback → no error; a throwing callback never breaks the scan
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))

    def boom(_ev):
        raise RuntimeError("reporter blew up")

    loc = ensure_pending_location(conn, 1000)
    result = inventory_location(conn, location=loc, root=root, now=1000, progress=boom)
    assert result.new == 1   # scan completed despite the callback raising


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


# ── incremental re-scan (skip hashing unchanged files) ────────────────────────

class _HashSpy:
    """Wraps a hashing function, counting how many times it actually runs."""

    def __init__(self, fn):
        self._fn = fn
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return self._fn(path)


def _install_hash_spies(monkeypatch):
    from spindlebot.core import identity as _identity

    id_spy = _HashSpy(_identity.audio_content_id)
    sha_spy = _HashSpy(_identity.file_sha256)
    monkeypatch.setattr(inventory, "audio_content_id", id_spy)
    monkeypatch.setattr(inventory, "file_sha256", sha_spy)
    return id_spy, sha_spy


def _snapshot_presence(conn, loc_id):
    rows = conn.execute(
        "SELECT audio_id, rel_path, file_sha256, byte_size, mtime "
        "FROM audio_presence WHERE location_id = ? ORDER BY rel_path",
        (loc_id,),
    ).fetchall()
    return [tuple(r) for r in rows]


def test_second_scan_no_changes_rehashes_nothing(conn, tmp_path, monkeypatch):
    root = tmp_path / "Pending"
    _write_flac(root / "AA" / "Album" / "01.flac",
                audio_md5_bytes=bytes(range(1, 17)),
                tags={"album": "Album", "title": "One", "tracknumber": "1"})
    (root / "AA" / "Album" / "cover.jpg").write_bytes(b"\xff\xd8jpeg")

    _run(conn, root, now=1000)
    before = _snapshot_presence(conn, ensure_pending_location(conn, 1000).id)

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    result, loc = _run(conn, root, now=2000)

    # Nothing was re-hashed: neither identity nor per-copy integrity.
    assert id_spy.calls == 0
    assert sha_spy.calls == 0
    # Content/presence unchanged except observed_utc; identity + sha256 reused.
    assert result.scanned == 1 and result.new == 0 and result.updated == 1
    assert _snapshot_presence(conn, loc.id) == before
    pres = conn.execute(
        "SELECT observed_utc FROM audio_presence WHERE location_id = ?", (loc.id,)
    ).fetchone()
    assert pres["observed_utc"] == 2000   # refreshed


def test_changed_size_is_rehashed(conn, tmp_path, monkeypatch):
    root = tmp_path / "Pending"
    path = root / "01.flac"
    _write_flac(path, audio_md5_bytes=bytes(range(1, 17)))
    _run(conn, root, now=1000)

    loc = ensure_pending_location(conn, 1000)
    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    old = presence_repo.get(conn, audio.id, loc.id)

    # Rewrite with a different audio md5 → different identity AND different bytes.
    _write_flac(path, audio_md5_bytes=bytes(range(17, 33)))
    # Ensure size differs even if the FLAC container happened to be equal-length.
    with open(path, "ab") as fh:
        fh.write(b"padding")

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    result, _ = _run(conn, root, now=2000)

    assert id_spy.calls == 1 and sha_spy.calls == 1     # the file WAS re-hashed
    new_audio = audio_repo.get_by_identity(conn, bytes(range(17, 33)).hex())
    assert new_audio is not None                        # new identity recorded
    new = presence_repo.get(conn, new_audio.id, loc.id)
    assert new.file_sha256 != old.file_sha256           # integrity hash updated


def test_changed_mtime_only_is_rehashed(conn, tmp_path, monkeypatch):
    import os as _os

    root = tmp_path / "Pending"
    path = root / "01.flac"
    _write_flac(path, audio_md5_bytes=bytes(range(1, 17)))
    _run(conn, root, now=1000)

    # Same bytes/size, but a newer mtime → treated as changed, so it re-hashes.
    st = path.stat()
    _os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    _run(conn, root, now=2000)
    assert id_spy.calls == 1 and sha_spy.calls == 1


def test_rehash_flag_rehashes_everything(conn, tmp_path, monkeypatch):
    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))
    (root / "cover.jpg").write_bytes(b"\xff\xd8jpeg")
    # Give the cover an album to attach to so it's inventoried as a sidecar.
    _write_flac(root / "02.flac", audio_md5_bytes=bytes(range(33, 49)),
                tags={"album": "Album", "title": "Two", "tracknumber": "2"})
    _run(conn, root, now=1000)

    loc = ensure_pending_location(conn, 1000)

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    inventory_location(conn, location=loc, root=root, now=2000, rehash=True)

    # Both audio files re-identified; every copy (audio + sidecar) re-hashed.
    assert id_spy.calls == 2
    assert sha_spy.calls >= 3   # 2 audio + at least 1 sidecar


def test_reused_identity_survives_a_moved_mtime_when_size_matches(conn, tmp_path, monkeypatch):
    """Sanity: unchanged (size,mtime) reuses even the fallback file_sha256 identity."""
    root = tmp_path / "Pending"
    # all-zero audio md5 → identity falls back to whole-file sha256
    _write_flac(root / "01.flac", audio_md5_bytes=b"\x00" * 16)
    _run(conn, root, now=1000)
    rows = conn.execute("SELECT identity_kind FROM audio_content").fetchall()
    assert rows[0]["identity_kind"] == "file_sha256"

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    _run(conn, root, now=2000)
    assert id_spy.calls == 0 and sha_spy.calls == 0
    assert audio_repo.count(conn) == 1


def test_noninventory_update_keeps_mtime_so_next_scan_still_skips(conn, tmp_path, monkeypatch):
    """A sync/copy write (new file_sha256, no mtime) must not defeat the skip.

    Simulates the copy executor updating a copy's integrity hash without an
    mtime; the recorded mtime is preserved (COALESCE), so the next inventory
    scan of the unchanged file still reuses everything and hashes nothing.
    """
    root = tmp_path / "Pending"
    path = root / "01.flac"
    _write_flac(path, audio_md5_bytes=bytes(range(1, 17)))
    _run(conn, root, now=1000)

    loc = ensure_pending_location(conn, 1000)
    audio = audio_repo.get_by_identity(conn, bytes(range(1, 17)).hex())
    recorded_mtime = presence_repo.get(conn, audio.id, loc.id).mtime
    assert recorded_mtime is not None

    # Non-inventory writer: refresh file_sha256 + observed_utc, omit mtime.
    presence_repo.set_presence(
        conn, audio_id=audio.id, location_id=loc.id, present=True,
        observed_utc=1500, rel_path="01.flac", file_sha256="rewritten",
        byte_size=path.stat().st_size,
    )
    assert presence_repo.get(conn, audio.id, loc.id).mtime == recorded_mtime

    id_spy, sha_spy = _install_hash_spies(monkeypatch)
    _run(conn, root, now=2000)
    assert id_spy.calls == 0 and sha_spy.calls == 0


def test_scan_records_a_real_finish_time(conn, tmp_path):
    """finished_utc is the actual finish, not the injected start.

    Reusing the start timestamp recorded every scan as zero-duration, hiding
    the one signal that distinguishes an incremental rescan from a full
    re-hash — precisely the measurement needed when a rescan runs slow.
    """
    import time as _time

    root = tmp_path / "Pending"
    _write_flac(root / "01.flac", audio_md5_bytes=bytes(range(1, 17)))

    before = int(_time.time())
    _result, loc = _run(conn, root, now=1)   # a start far in the past
    scan = scan_repo.latest_scan(conn, loc.id)

    assert scan["started_utc"] == 1
    assert scan["finished_utc"] >= before
