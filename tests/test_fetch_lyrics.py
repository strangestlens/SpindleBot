"""
Tests for spindlebot.pipeline.stages.fetch_lyrics.
"""
from __future__ import annotations

import json
import struct
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac
import pytest

from spindlebot.pipeline.stages.fetch_lyrics import (
    LyricsResult,
    _fetch_from_lrclib,
    _get_tags,
    _plain_to_lrc,
    _query_lrclib,
    _strip_cjk,
    _title_from_filename,
    album_lyrics_complete,
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


# ── unit: _query_lrclib miss vs transient error ──────────────────────────────


class TestQueryLrclibMissVsError:
    """The load-bearing distinction: a successful/404 response is a definitive
    miss (returns None,None); anything else is transient (raises)."""

    def _http_error(self, code):
        return urllib.error.HTTPError(
            url="u", code=code, msg="x", hdrs=None, fp=None)

    def test_404_is_definitive_miss(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(404)):
            assert _query_lrclib("a", "t", "al", 100, 0.0) == (None, None)

    def test_200_with_no_lyrics_is_definitive_miss(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   return_value=_lrclib_response(synced=None, plain=None)):
            assert _query_lrclib("a", "t", "al", 100, 0.0) == (None, None)

    def test_500_is_transient_error(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(500)):
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)

    def test_connection_error_is_transient(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            with pytest.raises(urllib.error.URLError):
                _query_lrclib("a", "t", "al", 100, 0.0)


# ── unit: per-track terminal markers (miss vs error) ─────────────────────────


class TestPerTrackTerminalMarkers:

    def test_definitive_miss_writes_per_track_nolrc(self, tmp_path):
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.missing == 1
        assert (tmp_path / "01. Song.nolrc").exists()
        assert not (tmp_path / "01. Song.lrc").exists()

    def test_transient_error_writes_nothing(self, tmp_path):
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   side_effect=urllib.error.URLError("connection refused")):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.errors == ["01. Song.flac"]
        assert not (tmp_path / "01. Song.nolrc").exists()
        assert not (tmp_path / "01. Song.lrc").exists()

    def test_synced_hit_clears_stale_per_track_nolrc(self, tmp_path):
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        (tmp_path / "01. Song.nolrc").write_text("")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Hello", None)):
            result = fetch_lyrics(tmp_path, _cfg(), force=True)

        assert result.synced == 1
        assert (tmp_path / "01. Song.lrc").exists()
        assert not (tmp_path / "01. Song.nolrc").exists()

    def test_existing_per_track_nolrc_not_requeried(self, tmp_path):
        """A definitively-missed track (has .nolrc, no .lrc) is terminal: no
        lrclib call on a later run, and it still counts as a miss."""
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        (tmp_path / "01. Song.nolrc").write_text("")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib") as mock_fetch:
            result = fetch_lyrics(tmp_path, _cfg())

        mock_fetch.assert_not_called()
        assert result.missing == 1
        assert result.total_found == 0
        assert album_lyrics_complete(tmp_path) is True

    def test_force_requeries_existing_nolrc(self, tmp_path):
        """--force re-queries a track that previously missed; a fresh hit writes
        .lrc and clears the stale .nolrc."""
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        (tmp_path / "01. Song.nolrc").write_text("")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Now found", None)) as mock_fetch:
            result = fetch_lyrics(tmp_path, _cfg(), force=True)

        mock_fetch.assert_called_once()
        assert result.synced == 1
        assert (tmp_path / "01. Song.lrc").exists()
        assert not (tmp_path / "01. Song.nolrc").exists()

    def test_stale_nolrc_removal_failure_aborts_before_writing_lrc(self, tmp_path):
        """If the stale .nolrc can't be removed, the track never ends up carrying
        both markers — it aborts as a transient error, .nolrc intact, no .lrc."""
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        (tmp_path / "01. Song.nolrc").write_text("")

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=("[00:01.00] Found", None)), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.os.remove",
                   side_effect=PermissionError("locked")):
            result = fetch_lyrics(tmp_path, _cfg(), force=True)

        assert result.errors == ["01. Song.flac"]
        assert (tmp_path / "01. Song.nolrc").exists()
        assert not (tmp_path / "01. Song.lrc").exists()

    def test_definitive_miss_not_written_on_dry_run(self, tmp_path):
        f = tmp_path / "01. Song.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            fetch_lyrics(tmp_path, _cfg(), dry_run=True)

        assert not (tmp_path / "01. Song.nolrc").exists()

    def test_album_nolrc_not_written_when_a_track_errors(self, tmp_path):
        """Mixed album: one definitive miss + one transient error. No blanket marker."""
        miss = tmp_path / "01. Miss.flac"
        err = tmp_path / "02. Err.flac"
        _write_minimal_flac(miss, {"artist": "Band", "title": "Miss", "album": "Record"})
        _write_minimal_flac(err, {"artist": "Band", "title": "Err", "album": "Record"})

        def _fetch(artist, title, *a, **k):
            if title == "Err":
                raise urllib.error.URLError("timeout")
            return (None, None)

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   side_effect=_fetch):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.missing == 1
        assert result.errors == ["02. Err.flac"]
        # The missed track still gets its own terminal marker...
        assert (tmp_path / "01. Miss.nolrc").exists()
        # ...but the blanket album-level marker must NOT fire (album incomplete).
        assert not (tmp_path / ".nolrc").exists()

    def test_album_nolrc_written_when_all_tracks_definitively_miss(self, tmp_path):
        for i, t in enumerate(["One", "Two"], start=1):
            f = tmp_path / f"0{i}. {t}.flac"
            _write_minimal_flac(f, {"artist": "Band", "title": t, "album": "Record"})

        with patch("spindlebot.pipeline.stages.fetch_lyrics._fetch_from_lrclib",
                   return_value=(None, None)):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.missing == 2 and not result.errors
        assert (tmp_path / ".nolrc").exists()
        assert (tmp_path / "01. One.nolrc").exists()
        assert (tmp_path / "02. Two.nolrc").exists()


# ── unit: album_lyrics_complete predicate ─────────────────────────────────────


class TestAlbumLyricsComplete:

    def test_empty_dir_is_complete(self, tmp_path):
        assert album_lyrics_complete(tmp_path) is True

    def test_complete_when_every_track_has_lrc(self, tmp_path):
        for i in (1, 2):
            _write_minimal_flac(tmp_path / f"0{i}. T.flac",
                                {"artist": "B", "title": "T", "album": "R"})
            (tmp_path / f"0{i}. T.lrc").write_text("[00:00.00] x\n")
        assert album_lyrics_complete(tmp_path) is True

    def test_complete_with_mix_of_lrc_and_nolrc(self, tmp_path):
        _write_minimal_flac(tmp_path / "01. A.flac", {"artist": "B", "title": "A", "album": "R"})
        _write_minimal_flac(tmp_path / "02. B.flac", {"artist": "B", "title": "B", "album": "R"})
        (tmp_path / "01. A.lrc").write_text("[00:00.00] x\n")
        (tmp_path / "02. B.nolrc").write_text("")
        assert album_lyrics_complete(tmp_path) is True

    def test_incomplete_when_a_track_has_neither(self, tmp_path):
        _write_minimal_flac(tmp_path / "01. A.flac", {"artist": "B", "title": "A", "album": "R"})
        _write_minimal_flac(tmp_path / "02. B.flac", {"artist": "B", "title": "B", "album": "R"})
        (tmp_path / "01. A.lrc").write_text("[00:00.00] x\n")
        # 02 has nothing → not terminal
        assert album_lyrics_complete(tmp_path) is False

    def test_album_level_nolrc_implies_complete(self, tmp_path):
        # No per-track terminal state at all, but the album marker says all-miss.
        _write_minimal_flac(tmp_path / "01. A.flac", {"artist": "B", "title": "A", "album": "R"})
        _write_minimal_flac(tmp_path / "02. B.flac", {"artist": "B", "title": "B", "album": "R"})
        (tmp_path / ".nolrc").write_text("")
        assert album_lyrics_complete(tmp_path) is True

    def test_predicate_writes_nothing(self, tmp_path):
        _write_minimal_flac(tmp_path / "01. A.flac", {"artist": "B", "title": "A", "album": "R"})
        before = {p.name for p in tmp_path.iterdir()}
        album_lyrics_complete(tmp_path)
        assert {p.name for p in tmp_path.iterdir()} == before


# ── unit: _get_tags sort/English fields ───────────────────────────────────────


class TestGetTagsSortFields:

    def test_reads_artistsort(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "ベック", "title": "ハイパーライフ", "album": "ハイパースペース",
            "artistsort": "Beck",
        })
        tags = _get_tags(str(f))
        assert tags["artist_sort"] == "Beck"

    def test_reads_title_english(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "ベック", "title": "ハイパーライフ", "album": "ハイパースペース",
            "title_english": "Hyperlife",
        })
        tags = _get_tags(str(f))
        assert tags["title_english"] == "Hyperlife"

    def test_artist_sort_defaults_to_empty(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        tags = _get_tags(str(f))
        assert tags.get("artist_sort", "") == ""

    def test_title_english_defaults_to_empty(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Record"})
        tags = _get_tags(str(f))
        assert tags.get("title_english", "") == ""


# ── unit: English/sort fallback in _fetch_from_lrclib ────────────────────────


class TestFetchFromLrclibEnglishFallback:

    def _make_query_mock(self, hits: set):
        """
        Return a mock for _query_lrclib that returns synced lyrics only for
        specific (artist, title) pairs listed in `hits`.  Album is ignored so
        tests aren't sensitive to primary-vs-fallback album values.
        """
        def _query(artist, title, album, duration, delay):
            key = (artist.lower(), title.lower())
            if key in {(a.lower(), t.lower()) for a, t in hits}:
                return "[00:01.00] Found", None
            return None, None
        return _query

    def test_english_fallback_used_when_primary_fails(self, tmp_path):
        """artist_sort + title_english fires when the Japanese title misses."""
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "ベック", "title": "ハイパーライフ", "album": "ハイパースペース",
            "artistsort": "Beck", "title_english": "Hyperlife",
        })

        hits = {("Beck", "Hyperlife")}
        with patch(
            "spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
            side_effect=self._make_query_mock(hits),
        ):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.synced == 1

    def test_primary_used_first_english_not_tried_on_hit(self, tmp_path):
        """If the primary (Japanese) title hits, the English fallback is never tried."""
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Beck", "title": "Chemical", "album": "Hyperspace",
            "artistsort": "Beck", "title_english": "Chemical",
        })

        call_log: list[tuple] = []

        def _query(artist, title, album, duration, delay):
            call_log.append((artist, title))
            if title == "Chemical":
                return "[00:01.00] Primary hit", None
            return None, None

        with patch(
            "spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
            side_effect=_query,
        ):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.synced == 1
        # Should have stopped after the first hit — English combo never tried
        assert all(t == "Chemical" for _, t in call_log)

    def test_no_fallback_when_title_english_absent(self, tmp_path):
        """When title_english is empty, only primary attempts are made."""
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "ベック", "title": "ハイパーライフ", "album": "ハイパースペース",
            "artistsort": "Beck",
            # No title_english
        })

        call_log: list[tuple] = []

        def _query(artist, title, album, duration, delay):
            call_log.append((artist, title))
            return None, None

        with patch(
            "spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
            side_effect=_query,
        ):
            result = fetch_lyrics(tmp_path, _cfg())

        assert result.missing == 1
        # All attempts used the original artist ("ベック"); sort artist still
        # tried as a last-ditch variant, but never with a title_english we don't have
        called_artists = {a for a, _ in call_log}
        called_titles = {t for _, t in call_log}
        # No title_english value means no English title queries
        assert "Hyperlife" not in called_titles
