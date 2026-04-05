"""
Tests for spindlebot.pipeline.stages.fetch_lyrics.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac
import pytest

from spindlebot.pipeline.stages.fetch_lyrics import (
    LyricsResult,
    _get_tags,
    _plain_to_lrc,
    _strip_cjk,
    _title_from_filename,
    fetch_lyrics,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(delay: float = 0.0) -> MagicMock:
    cfg = MagicMock()
    cfg.lyrics.request_delay_seconds = delay
    return cfg


def _write_minimal_flac(path: Path, tags: dict) -> None:
    """Write a minimal valid FLAC file with VorbisComment tags."""
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"
        + struct.pack(">Q", (44100 << 44) | (0 << 41) | (15 << 36) | 0)
        + b"\x00" * 16
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(str(path))
    f.add_tags()
    for k, v in tags.items():
        f[k] = [v]
    f.save()


def _lrclib_response(synced=None, plain=None) -> MagicMock:
    """Build a mock urlopen context manager that returns lrclib JSON."""
    data = json.dumps({"syncedLyrics": synced, "plainLyrics": plain}).encode()
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=data)
    # json.load uses the response as a file-like object
    mock_resp.read.return_value = data

    import io
    real_resp = io.BytesIO(data)
    mock_resp.__enter__ = MagicMock(return_value=real_resp)
    return mock_resp


# ── unit: helpers ─────────────────────────────────────────────────────────────


class TestHelpers:

    def test_strip_cjk_removes_japanese(self):
        assert _strip_cjk("東京 Tokyo") == "Tokyo"

    def test_strip_cjk_leaves_ascii_unchanged(self):
        assert _strip_cjk("Blue Lines") == "Blue Lines"

    def test_plain_to_lrc_wraps_lines(self):
        lrc = _plain_to_lrc("Line one\nLine two")
        assert "[00:00.00] Line one" in lrc
        assert "[00:00.00] Line two" in lrc

    def test_plain_to_lrc_empty_lines_not_stamped(self):
        lrc = _plain_to_lrc("Line one\n\nLine two")
        lines = lrc.splitlines()
        assert any(l == "" for l in lines)

    def test_title_from_filename_strips_track_number(self):
        assert _title_from_filename("/album/01. Song Title.flac") == "Song Title"
        assert _title_from_filename("/album/1-02. Song Title.mp3") == "Song Title"

    def test_title_from_filename_no_prefix(self):
        assert _title_from_filename("/album/Song Title.flac") == "Song Title"


# ── unit: _get_tags ───────────────────────────────────────────────────────────


class TestGetTags:

    def test_reads_flac_vorbis_tags(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "title": "Song", "album": "Record",
        })
        tags = _get_tags(str(f))
        assert tags["artist"] == "Band"
        assert tags["title"] == "Song"
        assert tags["album"] == "Record"

    def test_returns_empty_for_nonexistent_file(self, tmp_path):
        tags = _get_tags(str(tmp_path / "missing.flac"))
        assert tags == {} or "_error" in tags

    def test_returns_error_key_for_unreadable_file(self, tmp_path):
        bad = tmp_path / "bad.flac"
        bad.write_bytes(b"not a flac file")
        tags = _get_tags(str(bad))
        assert "_error" in tags


# ── unit: fetch_lyrics orchestration ─────────────────────────────────────────


class TestFetchLyrics:

    def test_empty_dir_returns_empty_result(self, tmp_path):
        result = fetch_lyrics(tmp_path, _cfg())
        assert result.synced == 0
        assert result.missing == 0

    def test_synced_lyrics_written(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        lrc_content = "[00:01.00] Hello"

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(lrc_content, None)):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.synced == 1
        lrc_path = tmp_path / "track.lrc"
        assert lrc_path.exists()
        assert "[00:01.00] Hello" in lrc_path.read_text()

    def test_plain_lyrics_wrapped_in_lrc(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, "Verse one\nVerse two")):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.plain == 1
        lrc_path = tmp_path / "track.lrc"
        assert "[00:00.00] Verse one" in lrc_path.read_text()

    def test_missing_lyrics_recorded(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.missing == 1
        assert result.total_found == 0

    def test_existing_lrc_skipped_without_force(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        lrc = tmp_path / "track.lrc"
        lrc.write_text("[00:00.00] existing")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib") as mock_fetch:
            result = fetch_lyrics(tmp_path, _cfg())

        mock_fetch.assert_not_called()
        assert result.skipped == 1

    def test_force_overwrites_existing_lrc(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        lrc = tmp_path / "track.lrc"
        lrc.write_text("[00:00.00] old")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] new", None)):
            result = fetch_lyrics(tmp_path, _cfg(), force=True)

        assert result.synced == 1
        assert "[00:01.00] new" in lrc.read_text()

    def test_dry_run_does_not_write_file(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Hello", None)):
            result = fetch_lyrics(tmp_path, _cfg(), dry_run=True)

        assert result.synced == 1
        assert not (tmp_path / "track.lrc").exists()

    def test_nolrc_marker_written_when_all_missing(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            fetch_lyrics(tmp_path, _cfg())

        assert (tmp_path / ".nolrc").exists()

    def test_nolrc_marker_removed_when_lyrics_found(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        (tmp_path / ".nolrc").touch()

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Hello", None)):
            fetch_lyrics(tmp_path, _cfg())

        assert not (tmp_path / ".nolrc").exists()

    def test_nolrc_not_written_on_dry_run(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            fetch_lyrics(tmp_path, _cfg(), dry_run=True)

        assert not (tmp_path / ".nolrc").exists()

    def test_multiple_audio_formats_processed(self, tmp_path):
        """find_audio_files returns all formats — fetch_lyrics should process all."""
        flac = tmp_path / "track.flac"
        mp3 = tmp_path / "track2.mp3"
        _write_minimal_flac(flac, {"artist": "Band", "title": "Song 1", "album": "Record"})
        mp3.write_bytes(b"placeholder")  # non-parseable, will be "skipped" on tag read

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Hello", None)):
            result = fetch_lyrics(tmp_path, _cfg())

        # At minimum the FLAC was processed; the MP3 placeholder was skipped cleanly
        assert result.synced + result.skipped + result.missing + len(result.errors) == 2

    def test_embedded_lyrics_used_as_fallback(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "title": "Song", "album": "Record",
            "lyrics": "Embedded verse",
        })

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.plain == 1
        lrc_path = tmp_path / "track.lrc"
        assert "Embedded verse" in lrc_path.read_text()
