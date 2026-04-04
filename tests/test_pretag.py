"""
Tests for spindlebot.pipeline.stages.pretag — pretag and posttag stage logic.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac
import pytest

from spindlebot.pipeline.stages.pretag import (
    BEETS_ALIAS_TAGS,
    XLD_JUNK_TAGS,
    _fix_bandcamp_album_encoding,
    posttag,
    pretag,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_minimal_flac(path: Path, tags: dict[str, str]) -> None:
    """
    Write a minimal valid FLAC file and attach the given tags.

    mutagen can read and write FLAC tags but cannot create a FLAC file from
    scratch — it needs a parseable STREAMINFO block to open the file at all.
    So we hand-write the minimum valid binary structure (magic + STREAMINFO
    metadata block), then let mutagen attach the Vorbis comment block on save.

    STREAMINFO layout (34 bytes):
      min/max blocksize (2+2), min/max framesize (3+3), then a packed 64-bit
      field: sample_rate(20b) | channels-1(3b) | bps-1(5b) | total_samples(36b),
      then 16-byte MD5.
    """
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


def _mock_audio_file(tags: dict[str, str]) -> MagicMock:
    """
    Return a mock mutagen file object with a VorbisComment-style tags dict.
    Used for non-FLAC format tests where constructing a real parseable audio
    file is impractical (MPEG frame layout, M4A box structure, etc.).
    """
    m = MagicMock()
    m.tags = {k: [v] for k, v in tags.items()}
    m.save = MagicMock()
    return m


# ── pretag: FLAC ──────────────────────────────────────────────────────────────


class TestPretagFLAC:

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
            assert read_tag(tmp_path / f"track{i:02d}.flac", "artist") == "Right Name"


# ── pretag: non-FLAC ─────────────────────────────────────────────────────────


class TestPretagNonFLAC:
    """
    Verify pretag logic for non-FLAC/non-Vorbis formats (MP3, M4A, etc.)
    by mocking mutagen.File. We test tag-manipulation logic, not format parsing.
    """

    def _run_pretag(self, tmp_path: Path, ext: str, tags: dict) -> MagicMock:
        fake = tmp_path / f"track.{ext}"
        fake.write_bytes(b"placeholder")
        mock = _mock_audio_file(tags)
        with patch("spindlebot.pipeline.stages.pretag.mutagen.File", return_value=mock):
            pretag(str(tmp_path))
        return mock

    def test_artist_normalized_in_mp3(self, tmp_path):
        mock = self._run_pretag(tmp_path, "mp3", {
            "artist": "The Band", "albumartist": "Band",
            "title": "Song", "album": "Record",
        })
        assert mock.tags["artist"] == ["Band"]
        mock.save.assert_called_once()

    def test_feat_moved_in_mp3(self, tmp_path):
        mock = self._run_pretag(tmp_path, "mp3", {
            "artist": "Band feat. Guest", "albumartist": "Band",
            "title": "Song", "album": "Record",
        })
        assert mock.tags["title"] == ["Song (feat. Guest)"]
        assert mock.tags["artist"] == ["Band"]

    def test_va_skipped_in_mp3(self, tmp_path):
        mock = self._run_pretag(tmp_path, "mp3", {
            "artist": "Track Artist", "albumartist": "Various Artists",
            "title": "Song", "album": "Comp",
        })
        assert mock.tags["artist"] == ["Track Artist"]
        mock.save.assert_not_called()

    def test_xld_junk_not_stripped_for_non_vorbis(self, tmp_path):
        """XLD junk cleanup must only run on VorbisComment formats."""
        tags = {"artist": "A", "albumartist": "A", "title": "T"}
        for junk in XLD_JUNK_TAGS:
            tags[junk] = "junk"
        mock = self._run_pretag(tmp_path, "mp3", tags)
        # Junk tags should still be present — we didn't touch them
        for junk in XLD_JUNK_TAGS:
            assert junk in mock.tags


# ── Bandcamp encoding fix ─────────────────────────────────────────────────────


class TestBandcampAlbumFix:

    def test_fixes_ascii_mangled_album(self, tmp_path):
        album_dir = str(tmp_path / "Aquáticos")
        os.makedirs(album_dir)
        tags = {"album": ["Aqu'aticos"], "comment": ["Visit https://label.bandcamp.com"]}
        assert _fix_bandcamp_album_encoding(tags, album_dir) is True
        assert tags["album"] == ["Aquáticos"]

    def test_no_change_without_bandcamp_comment(self, tmp_path):
        album_dir = str(tmp_path / "Aquáticos")
        os.makedirs(album_dir)
        tags = {"album": ["Aqu'aticos"], "comment": [""]}
        assert _fix_bandcamp_album_encoding(tags, album_dir) is False

    def test_no_change_when_album_already_correct(self, tmp_path):
        album_dir = str(tmp_path / "Aquáticos")
        os.makedirs(album_dir)
        tags = {"album": ["Aquáticos"], "comment": ["Visit https://label.bandcamp.com"]}
        assert _fix_bandcamp_album_encoding(tags, album_dir) is False

    def test_no_change_when_dir_is_ascii(self, tmp_path):
        album_dir = str(tmp_path / "Normal Album")
        os.makedirs(album_dir)
        tags = {"album": ["Normal Album"], "comment": ["Visit https://artist.bandcamp.com"]}
        assert _fix_bandcamp_album_encoding(tags, album_dir) is False

    def test_strips_artist_prefix_from_dir_name(self, tmp_path):
        album_dir = str(tmp_path / "CLANN - Seelie")
        os.makedirs(album_dir)
        # "Seelie" is ASCII, dir has no Unicode → no change
        tags = {"album": ["Seelie"], "comment": ["Visit https://clann.bandcamp.com"]}
        assert _fix_bandcamp_album_encoding(tags, album_dir) is False

    def test_bandcamp_comment_preserved_in_flac(self, tmp_path):
        """Bandcamp COMMENT tag must survive pretag."""
        f = tmp_path / "01.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "albumartist": "Band", "title": "Song",
            "comment": "Visit https://clann.bandcamp.com",
        })
        pretag(str(tmp_path))
        flac = mutagen.flac.FLAC(str(f))
        assert "comment" in flac.tags
        assert "bandcamp.com" in flac.tags["comment"][0]


# ── posttag ───────────────────────────────────────────────────────────────────


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

    def test_unrecognized_extension_skipped(self, tmp_path):
        """Files with extensions not in AUDIO_EXTENSIONS are ignored."""
        jpg = tmp_path / "cover.jpg"
        jpg.write_bytes(b"fake")
        assert posttag([str(jpg)]) == 0

    def test_missing_path_skipped(self, tmp_path):
        assert posttag([str(tmp_path / "nonexistent.flac")]) == 0

    def test_returns_count_of_modified_files(self, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"track{i}.flac"
            _write_minimal_flac(f, {"date": "2020-01-01" if i < 2 else "2020", "artist": "A"})
            files.append(str(f))
        assert posttag(files) == 2

    def test_non_flac_date_truncated(self, tmp_path):
        """posttag truncates DATE for non-FLAC formats too."""
        fake_mp3 = tmp_path / "track.mp3"
        fake_mp3.write_bytes(b"placeholder")
        mock = _mock_audio_file({"date": "2021-03-15"})
        with patch("spindlebot.pipeline.stages.pretag.mutagen.File", return_value=mock):
            count = posttag([str(fake_mp3)])
        assert count == 1
        assert mock.tags["date"] == ["2021"]
        mock.save.assert_called_once()
