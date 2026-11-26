"""Album identity guards, including native beets against isolated fixtures."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.services.beets_guard import guarded_beet
from spindlebot.services.directory_collision import (
    DirectoryCollision,
    IdentityCache,
    check_album_destinations,
)


def _flac(path: Path, identity: int, *, album="MAYA", title="Track"):
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00" * 6
        + struct.pack(">Q", (44100 << 44) | (15 << 36))
        + bytes([identity]) * 16
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0, 0, 0x22]) + streaminfo)
    audio = mutagen.flac.FLAC(path)
    audio.add_tags()
    for key, value in {
        "artist": "Artist",
        "albumartist": "Artist",
        "album": album,
        "title": title,
        "tracknumber": "1",
    }.items():
        audio[key] = [value]
    audio.save()


def _case_insensitive(path):
    probe = path / "CaseProbe"
    probe.write_text("probe")
    supported = (path / "caseprobe").exists()
    probe.unlink()
    if not supported:
        pytest.skip(
            "requires a case-insensitive filesystem to reproduce Maya/MAYA aliasing"
        )


def test_foreign_audio_refuses_the_whole_album_before_writes(tmp_path):
    source = tmp_path / "Import/new.flac"
    dest = tmp_path / "Pending/Artist/Album/new.flac"
    _flac(source, 2)
    _flac(dest.parent / "old.flac", 1)
    before = source.read_bytes(), (dest.parent / "old.flac").read_bytes()
    with pytest.raises(DirectoryCollision, match="different audio"):
        check_album_destinations([source], [dest])
    assert before == (source.read_bytes(), (dest.parent / "old.flac").read_bytes())
    assert not dest.exists()


def test_same_audio_with_changed_tags_is_safe(tmp_path):
    source = tmp_path / "Import/new.flac"
    dest = tmp_path / "Pending/Artist/Album/new.flac"
    _flac(source, 1, title="New tags")
    _flac(dest, 1, title="Old tags")
    assert source.read_bytes() != dest.read_bytes()
    check_album_destinations([source], [dest])


def test_existing_incoming_subset_is_safe_but_a_foreign_track_is_not(tmp_path):
    first, second = tmp_path / "Import/first.flac", tmp_path / "Import/second.flac"
    dest = tmp_path / "Processing/Artist/Album"
    _flac(first, 1)
    _flac(second, 2)
    _flac(dest / "first.flac", 1)
    check_album_destinations([first, second], [dest / first.name, dest / second.name])
    _flac(dest / "foreign.flac", 3)
    with pytest.raises(DirectoryCollision):
        check_album_destinations(
            [first, second], [dest / first.name, dest / second.name]
        )


def test_case_alias_is_decided_by_filesystem_and_identity(tmp_path):
    _case_insensitive(tmp_path)
    source = tmp_path / "Import/new.flac"
    _flac(source, 2)
    old = tmp_path / "Pending/Artist/Maya/old.flac"
    _flac(old, 1)
    with pytest.raises(DirectoryCollision):
        check_album_destinations([source], [old.parent.with_name("MAYA") / source.name])


def test_nested_foreign_audio_is_part_of_the_existing_directory(tmp_path):
    source = tmp_path / "Import/new.flac"
    old = tmp_path / "Pending/Album/Disc1/old.flac"
    _flac(source, 2)
    _flac(old, 1)
    with pytest.raises(DirectoryCollision):
        check_album_destinations([source], [old.parent.parent / source.name])


def test_appledouble_resource_forks_are_not_foreign_audio(tmp_path):
    source = tmp_path / "Import/new.flac"
    old = tmp_path / "Pending/Album/old.flac"
    _flac(source, 1)
    _flac(old, 1)
    (old.parent / "._old.flac").write_bytes(b"AppleDouble resource fork")
    check_album_destinations([source], [old.parent / source.name])


def test_operation_identity_cache_reuses_reads_and_invalidates_changed_files(
    tmp_path, monkeypatch
):
    import spindlebot.services.directory_collision as guard

    source = tmp_path / "source.flac"
    _flac(source, 1)
    calls = []
    original = guard.audio_content_id

    def counted(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(guard, "audio_content_id", counted)
    cache = IdentityCache()
    first = cache(source)
    for _ in range(10):
        assert cache(source) == first
    assert calls == [source]
    _flac(source, 2, title="changed source bytes and tags")
    assert cache(source) != first
    assert calls == [source, source]


def test_unreadable_destination_traversal_refuses_verification(tmp_path, monkeypatch):
    import spindlebot.services.directory_collision as guard

    source = tmp_path / "source.flac"
    directory = tmp_path / "Album"
    directory.mkdir()
    _flac(source, 1)

    def unreadable(directory, onerror):
        onerror(PermissionError("fixture unreadable album subdirectory"))

    monkeypatch.setattr(guard.os, "walk", unreadable)
    with pytest.raises(PermissionError):
        check_album_destinations([source], [directory / source.name])


def test_adapter_refuses_a_changed_source_after_preflight(tmp_path):
    from types import SimpleNamespace

    pytest.importorskip(
        "beets", reason="native adapter tests need beets (installed in CI)"
    )
    from beets import ui
    from spindlebot.beets_plugins.spindlebot_collision_guard import (
        SpindlebotCollisionGuardPlugin,
    )

    source = tmp_path / "Import/source.flac"
    destination = tmp_path / "Pending/Album/source.flac"
    _flac(source, 1)
    item = SimpleNamespace(id=1, path=os.fsencode(source))
    plugin = SpindlebotCollisionGuardPlugin()
    plugin._check_items([item], [destination])
    _flac(source, 2)
    plugin.record_tag_write(item)  # A source write cannot bless changed input.
    with pytest.raises(ui.UserError, match="changed after preflight"):
        plugin.check_move(item, os.fsencode(source), os.fsencode(destination))


@pytest.fixture
def native(tmp_path, monkeypatch):
    pytest.importorskip(
        "beets", reason="native adapter tests need beets (installed in CI)"
    )
    configdir = tmp_path / "BeetsConfig"
    configdir.mkdir()
    pending = tmp_path / "Pending"
    pending.mkdir()
    config = {
        "directory": str(pending),
        "library": str(tmp_path / "beets.db"),
        "plugins": [],
        "pluginpath": [],
        "musicbrainz": {"enabled": False},
        "threaded": False,
        "paths": {"default": "$albumartist/$album/$track $title"},
        "import": {
            "autotag": False,
            "move": True,
            "copy": False,
            "write": False,
            "resume": False,
            "incremental": False,
            "quiet": True,
            "duplicate_action": "skip",
        },
    }
    (configdir / "config.yaml").write_text(json.dumps(config))
    monkeypatch.setenv("BEETSDIR", str(configdir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "EmptyConfig"))
    beet = tmp_path / "beet"
    beet.write_text(
        f"#!{sys.executable}\n"
        "import socket\n"
        "def refused(*a, **kw): raise OSError('network disabled in native guard tests')\n"
        "socket.socket.connect = refused\n"
        "socket.create_connection = refused\n"
        "from beets.ui import main\nmain()\n"
    )
    beet.chmod(0o755)
    return beet, pending, config


def _run(beet, *args):
    with guarded_beet(beet, args[0]) as (prefix, env):
        return subprocess.run(
            [*prefix, *args[1:]], env=env, capture_output=True, text=True
        )


@pytest.mark.parametrize("case_alias", [False, True])
def test_native_as_is_import_refuses_before_first_canonical_write(
    native, tmp_path, case_alias
):
    beet, pending, _ = native
    if case_alias:
        _case_insensitive(tmp_path)
    old = pending / "Artist" / ("Maya" if case_alias else "MAYA") / "old.flac"
    _flac(old, 1)
    source = tmp_path / "Import/new.flac"
    _flac(source, 2)
    before = source.read_bytes(), old.read_bytes()
    result = _run(beet, "import", "-A", "-q", str(source.parent))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collision" in result.stderr.lower()
    assert source.exists() and (source.read_bytes(), old.read_bytes()) == before
    assert sorted(p.name for p in old.parent.iterdir()) == [old.name]


def test_native_import_and_same_album_duplicate_remain_supported(native, tmp_path):
    beet, pending, _ = native
    source = tmp_path / "Import/first.flac"
    _flac(source, 1)
    first = _run(beet, "import", "-A", "-q", str(source.parent))
    assert first.returncode == 0, first.stdout + first.stderr
    assert not source.exists()
    duplicate = tmp_path / "ImportAgain/second.flac"
    _flac(duplicate, 1)
    result = _run(beet, "import", "-A", "-q", str(duplicate.parent))
    assert result.returncode == 0, result.stdout + result.stderr
    assert duplicate.exists(), (
        "beets duplicate skip leaves the incoming rip for the existing Duplicates flow"
    )
    assert len(list(pending.rglob("*.flac"))) == 1


@pytest.mark.parametrize("area", ["Processing", "Pending"])
def test_native_move_refuses_a_foreign_album_directory(native, tmp_path, area):
    beet, pending, _ = native
    source = tmp_path / "Import/first.flac"
    _flac(source, 2)
    assert _run(beet, "import", "-A", "-q", str(source.parent)).returncode == 0
    current = next(pending.rglob("*.flac"))
    destination = tmp_path / area if area != "Pending" else tmp_path / "OtherPending"
    old = destination / "Artist/MAYA/old.flac"
    _flac(old, 1)
    before = current.read_bytes(), old.read_bytes()
    result = _run(beet, "move", "-d", str(destination), f"path:{current.parent}/")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (current.read_bytes(), old.read_bytes()) == before
    assert sorted(p.name for p in old.parent.iterdir()) == [old.name]


def _configure_offline_tagger(native, tmp_path):
    _, _, config = native
    plugins = tmp_path / "FixturePlugins"
    plugins.mkdir()
    marker = tmp_path / "metadata-applied"
    (plugins / "fixture_tagger.py").write_text("""
from pathlib import Path
from beets import config
from beets.plugins import BeetsPlugin
from beets.autotag.hooks import AlbumInfo, TrackInfo
class FixtureTaggerPlugin(BeetsPlugin):
    data_source = "Fixture"
    def __init__(self):
        super().__init__()
        self.config.add({"source_weight": 0.0})
        self.register_listener("import_task_apply", self.applied)
    def applied(self, session, task):
        Path(config["fixture_marker"].get()).write_text("matched metadata applied")
    def candidates(self, items, artist, album, va_likely):
        yield AlbumInfo(album="MAYA", album_id="fixture-album", artist="Artist",
            artist_id="fixture-artist", data_source="Fixture", mediums=1, tracks=[
            TrackInfo(title=f"Track {n}", track_id=f"fixture-track-{n}", artist="Artist",
                length=0, index=n, medium=1, medium_index=n)
            for n, item in enumerate(items, 1)])
    def item_candidates(self, item, artist, title):
        return []
    def album_for_id(self, album_id):
        return None
    def track_for_id(self, track_id):
        return None
""")
    config["plugins"] = ["fixture_tagger"]
    config["pluginpath"] = [str(plugins)]
    config["fixture_marker"] = str(marker)
    config["import"]["autotag"] = True
    config["import"]["write"] = True
    config["match"] = {"strong_rec_thresh": 1.0, "medium_rec_thresh": 1.0}
    config_path = tmp_path / "BeetsConfig/config.yaml"
    config_path.write_text(json.dumps(config))
    return marker


def test_native_matched_import_checks_the_post_autotag_destination(native, tmp_path):
    beet, pending, _ = native
    marker = _configure_offline_tagger(native, tmp_path)
    old = pending / "Artist/MAYA/old.flac"
    _flac(old, 1)
    source = tmp_path / "Import/new.flac"
    _flac(source, 2, album="BeforeTagging")
    before = source.read_bytes(), old.read_bytes()
    result = _run(beet, "import", "-q", str(source.parent))
    assert marker.exists(), result.stdout + result.stderr
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collision" in result.stderr.lower()
    assert (source.read_bytes(), old.read_bytes()) == before
    assert sorted(p.name for p in old.parent.iterdir()) == [old.name]


def test_native_matched_import_supports_tag_writes_with_sha_fallback(native, tmp_path):
    beet, pending, _ = native
    marker = _configure_offline_tagger(native, tmp_path)
    for n in (1, 2):
        source = tmp_path / f"Import/track{n}.flac"
        _flac(source, 0, album="BeforeTagging", title=f"Source {n}")
        audio = mutagen.flac.FLAC(source)
        audio["tracknumber"] = [str(n)]
        audio.save()
    result = _run(beet, "import", "-q", str(tmp_path / "Import"))
    assert marker.exists(), result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list(pending.rglob("*.flac"))) == 2
    assert not list((tmp_path / "Import").rglob("*.flac"))
    assert all(
        mutagen.flac.FLAC(p)["album"] == ["MAYA"] for p in pending.rglob("*.flac")
    )


def test_missing_adapter_refuses_the_guarded_command_before_writing(native, tmp_path):
    beet, pending, _ = native
    source = tmp_path / "Import/new.flac"
    _flac(source, 2)
    before = source.read_bytes()
    with guarded_beet(beet, "import") as (prefix, env):
        overlay = Path(prefix[2])
        config = json.loads(overlay.read_text())
        config["plugins"] = []
        overlay.write_text(json.dumps(config))
        result = subprocess.run(
            [*prefix, "-A", "-q", str(source.parent)],
            env=env,
            capture_output=True,
            text=True,
        )
    assert result.returncode != 0
    assert source.read_bytes() == before
    assert list(pending.rglob("*.flac")) == []


@pytest.mark.parametrize("case_alias", [False, True])
def test_runner_refuses_before_import_first_writes_a_canonical_directory(
    native, tmp_path, case_alias
):
    from spindlebot.pipeline.runner import (
        ImportConfig,
        ImportResult,
        ImportRunner,
        _AlbumBatch,
    )

    beet, pending, _ = native
    if case_alias:
        _case_insensitive(tmp_path)
    old = pending / "Artist" / ("Maya" if case_alias else "MAYA") / "old.flac"
    _flac(old, 1)
    source = tmp_path / "Import/new.flac"
    _flac(source, 2)
    before = source.read_bytes(), old.read_bytes()
    cfg = ImportConfig(
        trigger=source.parent,
        force=True,
        beet=beet,
        python=Path(sys.executable),
        db=tmp_path / "beets.db",
        pending_dir=pending,
        processing_dir=tmp_path / "Processing",
        import_dir=source.parent,
        archive=tmp_path / "Archive",
        duplicates_dir=tmp_path / "Duplicates",
        pipeline_dir=tmp_path,
        log_file=tmp_path / "watcher.log",
    )
    batch = _AlbumBatch("Artist - MAYA", [str(source.parent)], source.parent, [source])
    result = ImportResult(success=False)
    assert ImportRunner(cfg)._import_one_album(batch, result) is False
    assert (source.read_bytes(), old.read_bytes()) == before
    assert sorted(p.name for p in old.parent.iterdir()) == [old.name]


@pytest.mark.parametrize("case_alias", [False, True])
def test_promote_refuses_the_native_pending_collision(native, tmp_path, case_alias):
    from spindlebot.services.promote import promote_album

    beet, pending, config = native
    if case_alias:
        _case_insensitive(tmp_path)
    processing = tmp_path / "Processing"
    processing.mkdir()
    config["directory"] = str(processing)
    config_path = tmp_path / "BeetsConfig/config.yaml"
    config_path.write_text(json.dumps(config))
    source = tmp_path / "Import/new.flac"
    _flac(source, 2)
    seeded = _run(beet, "import", "-A", "-q", str(source.parent))
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    current = next(processing.rglob("*.flac"))
    current.with_suffix(".nolrc").write_text("")
    old = pending / "Artist" / ("Maya" if case_alias else "MAYA") / "old.flac"
    _flac(old, 1)
    config["directory"] = str(pending)
    config_path.write_text(json.dumps(config))
    before = current.read_bytes(), old.read_bytes()
    result = promote_album(current.parent, beet)
    assert result.promoted is False
    assert "collision" in result.move_error.lower()
    assert (current.read_bytes(), old.read_bytes()) == before
    assert current.with_suffix(".nolrc").exists()


def _seed_two_discs(native, tmp_path):
    beet, pending, config = native
    processing = tmp_path / "Processing"
    processing.mkdir()
    config["directory"] = str(processing)
    config["paths"]["default"] = "$albumartist/$album [Disk $disc]/$track $title"
    (tmp_path / "BeetsConfig/config.yaml").write_text(json.dumps(config))
    for disc in (1, 2):
        path = tmp_path / f"Import/disc{disc}.flac"
        _flac(path, disc)
        audio = mutagen.flac.FLAC(path)
        audio["discnumber"] = [str(disc)]
        audio["disctotal"] = ["2"]
        audio.save()
    imported = _run(beet, "import", "-A", "-q", str(tmp_path / "Import"))
    assert imported.returncode == 0, imported.stdout + imported.stderr
    tracks = sorted(processing.rglob("*.flac"))
    assert len(tracks) == 2
    return beet, pending, config, tracks


def test_native_multidisc_move_preflights_later_collision_before_any_write(
    native, tmp_path
):
    beet, pending, _, tracks = _seed_two_discs(native, tmp_path)
    old = (
        pending / tracks[1].parent.relative_to(tmp_path / "Processing") / "foreign.flac"
    )
    _flac(old, 3)
    before = [p.read_bytes() for p in tracks], old.read_bytes()
    result = _run(beet, "move", "-d", str(pending), f"path:{tmp_path / 'Processing'}/")
    assert result.returncode != 0, result.stdout + result.stderr
    assert "collision" in result.stderr.lower()
    assert all(p.exists() for p in tracks)
    assert ([p.read_bytes() for p in tracks], old.read_bytes()) == before
    assert list(pending.rglob("*.flac")) == [old]


def test_native_promotion_ignores_an_unselected_offline_sibling_disc(native, tmp_path):
    from spindlebot.services.promote import promote_album

    beet, pending, config, tracks = _seed_two_discs(native, tmp_path)
    first, offline = tracks
    before = first.read_bytes()
    offline.unlink()  # Its beets row remains, as with a pruned/offline sibling.
    first.with_suffix(".nolrc").write_text("")
    config["directory"] = str(pending)
    (tmp_path / "BeetsConfig/config.yaml").write_text(json.dumps(config))
    result = promote_album(first.parent, beet)
    assert result.promoted, result.move_error
    assert not first.exists()
    moved = list(pending.rglob("*.flac"))
    assert len(moved) == 1 and moved[0].read_bytes() == before
    assert not offline.exists()


def test_native_multidisc_move_resumes_with_a_selected_disc_already_in_place(
    native, tmp_path
):
    beet, pending, _, tracks = _seed_two_discs(native, tmp_path)
    first = _run(beet, "move", "-d", str(pending), f"path:{tracks[0].parent}/")
    assert first.returncode == 0, first.stdout + first.stderr
    assert not tracks[0].exists() and tracks[1].exists()
    resumed = _run(beet, "move", "-d", str(pending), "album:MAYA")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert len(list(pending.rglob("*.flac"))) == 2
    assert not any(p.exists() for p in tracks)
