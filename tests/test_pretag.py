"""
Tests for spindlebot.pipeline.stages.pretag — pretag and posttag stage logic.
"""
from __future__ import annotations

import struct
from pathlib import Path

import mutagen.flac
import pytest

from spindlebot.pipeline.stages.pretag import BEETS_ALIAS_TAGS, XLD_JUNK_TAGS, posttag, pretag


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_minimal_flac(path: Path, tags: dict[str, str]) -> None:
    """Write a minimal valid FLAC file with the given Vorbis comment tags."""
    sample_rate = 44100
    channels = 1
    bps = 16
    total_samples = 0

    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"  # min/max frame size (3 bytes each)
        + struct.pack(
            ">Q",
            (sample_rate << 44)
            | ((channels - 1) << 41)
            | ((bps - 1) << 36)
            | total_samples,
        )
        + b"\x00" * 16  # MD5
    )

    # METADATA_BLOCK_HEADER: last=1, type=STREAMINFO(0), length=34
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)

    flac = mutagen.flac.FLAC(str(path))
    flac.add_tags()
    for key, val in tags.items():
        flac[key] = val  # str or list[str], mutagen accepts both
    flac.save()


def read_tag(path: Path, key: str) -> str | None:
    flac = mutagen.flac.FLAC(str(path))
    vals = flac.tags.get(key)
    return vals[0] if vals else None


# ── pretag tests ──────────────────────────────────────────────────────────────


class TestPretag:

    def test_empty_dir_returns_false(self, tmp_path):
        assert pretag(str(tmp_path)) is False

    def test_no_op_file_unchanged(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Solo Artist",
            "albumartist": "Solo Artist",
            "title": "A Song",
        })
        mtime_before = f.stat().st_mtime
        pretag(str(tmp_path))
        assert f.stat().st_mtime == mtime_before

    def test_xld_junk_tags_stripped(self, tmp_path):
        f = tmp_path / "track.flac"
        tags = {"albumartist": "Artist", "artist": "Artist", "title": "T"}
        for junk in XLD_JUNK_TAGS:
            tags[junk] = "junk"
        _write_minimal_flac(f, tags)

        pretag(str(tmp_path))

        flac = mutagen.flac.FLAC(str(f))
        for junk in XLD_JUNK_TAGS:
            assert junk not in flac.tags, f"Expected {junk!r} to be removed"

    def test_compilation_tag_stripped(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Artist", "albumartist": "Artist",
            "title": "T", "compilation": "1",
        })
        pretag(str(tmp_path))
        assert read_tag(f, "compilation") is None

    def test_feat_moved_from_artist_to_title(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Main Artist feat. Featured",
            "albumartist": "Main Artist",
            "title": "Song Name",
        })
        pretag(str(tmp_path))
        assert read_tag(f, "title") == "Song Name (feat. Featured)"
        assert read_tag(f, "artist") == "Main Artist"

    def test_feat_with_brackets(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Main Artist [feat. Guest]",
            "albumartist": "Main Artist",
            "title": "Track",
        })
        pretag(str(tmp_path))
        assert "feat. Guest" in read_tag(f, "title")
        assert read_tag(f, "artist") == "Main Artist"

    def test_artist_normalized_to_albumartist(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Some Other Name",
            "albumartist": "Canonical Name",
            "title": "Track",
        })
        pretag(str(tmp_path))
        assert read_tag(f, "artist") == "Canonical Name"

    def test_various_artists_skips_normalization(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Individual Artist",
            "albumartist": "Various Artists",
            "title": "Track",
        })
        pretag(str(tmp_path))
        # VA track artists must not be overwritten
        assert read_tag(f, "artist") == "Individual Artist"

    def test_multiple_files_all_processed(self, tmp_path):
        for i in range(3):
            f = tmp_path / f"track{i:02d}.flac"
            _write_minimal_flac(f, {
                "artist": "Wrong Name",
                "albumartist": "Right Name",
                "title": f"Track {i}",
            })
        pretag(str(tmp_path))
        for i in range(3):
            f = tmp_path / f"track{i:02d}.flac"
            assert read_tag(f, "artist") == "Right Name"


# ── posttag tests ─────────────────────────────────────────────────────────────


class TestPosttag:

    def test_beet_alias_tags_stripped(self, tmp_path):
        f = tmp_path / "track.flac"
        tags = {"albumartist": "Artist", "artist": "Artist", "title": "T"}
        for alias in BEETS_ALIAS_TAGS:
            tags[alias] = "something"
        _write_minimal_flac(f, tags)

        count = posttag([str(f)])

        assert count == 1
        flac = mutagen.flac.FLAC(str(f))
        for alias in BEETS_ALIAS_TAGS:
            assert alias not in flac.tags

    def test_date_truncated_to_year(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"date": "2019-06-21", "artist": "A"})

        posttag([str(f)])

        assert read_tag(f, "date") == "2019"

    def test_year_only_date_untouched(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"date": "2019", "artist": "A"})
        mtime_before = f.stat().st_mtime
        posttag([str(f)])
        assert f.stat().st_mtime == mtime_before

    def test_non_flac_paths_skipped(self, tmp_path):
        mp3 = tmp_path / "track.mp3"
        mp3.write_bytes(b"fake")
        count = posttag([str(mp3)])
        assert count == 0

    def test_missing_path_skipped(self, tmp_path):
        count = posttag([str(tmp_path / "nonexistent.flac")])
        assert count == 0

    def test_returns_count_of_modified_files(self, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"track{i}.flac"
            # Only the first two need modification (long date)
            date = "2020-01-01" if i < 2 else "2020"
            _write_minimal_flac(f, {"date": date, "artist": "A"})
            files.append(str(f))

        count = posttag(files)
        assert count == 2
