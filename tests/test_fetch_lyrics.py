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
    LRCLIB_MAX_DURATION,
    LRCLIB_RETRY_ATTEMPTS,
    LRCLIB_RETRY_BACKOFF,
    _get_tags,
    _plain_to_lrc,
    _process_file,
    _fetch_from_lrclib,
    _query_lrclib,
    _strip_cjk,
    _usable_duration,
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
        assert any(line == "" for line in lines)

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
        # patched sleep: 500 is retryable, so this now walks the backoff loop
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(500)):
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)

    def test_connection_error_is_transient(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("refused")):
            with pytest.raises(urllib.error.URLError):
                _query_lrclib("a", "t", "al", 100, 0.0)


# ── unit: the lrclib duration cap ────────────────────────────────────────────


class TestDurationCap:
    """lrclib validates `1 <= duration <= 3600` and 400s anything else.

    A 64-minute single-track rip (Brian Eno & Beatie Wolfe — Lateral, 3840s)
    therefore got a 400 on every attempt that carried its duration. The 400 was
    re-raised as an error, no terminal marker was written, and the album sat in
    Processing forever while `finalize` re-ran the identical failing query.
    """

    def test_over_cap_duration_is_dropped(self):
        assert _usable_duration(LRCLIB_MAX_DURATION + 1) is None
        assert _usable_duration(3840) is None

    def test_usable_durations_pass_through(self):
        assert _usable_duration(364) == 364
        assert _usable_duration(LRCLIB_MAX_DURATION) == LRCLIB_MAX_DURATION

    def test_absent_or_nonsense_duration_is_dropped(self):
        assert _usable_duration(0) is None
        assert _usable_duration(None) is None
        assert _usable_duration(-5) is None

    def test_an_over_cap_duration_never_reaches_the_url(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   return_value=_lrclib_response(synced=None, plain=None)) as urlopen:
            _query_lrclib("a", "t", "al", 3840, 0.0)
        url = urlopen.call_args.args[0].full_url
        assert "duration" not in url

    def test_a_usable_duration_does_reach_the_url(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   return_value=_lrclib_response(synced=None, plain=None)) as urlopen:
            _query_lrclib("a", "t", "al", 364, 0.0)
        assert "duration=364" in urlopen.call_args.args[0].full_url

    def test_an_over_cap_track_is_queried_once_not_twice(self, tmp_path):
        """The with-duration and without-duration variants collapse into one
        query rather than sending the identical request twice."""
        audio = tmp_path / "01. Long One.flac"
        audio.touch()
        with patch("spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
                   return_value=(None, None)) as q:
            _fetch_from_lrclib("Artist", "Long One", "Album", 3840, str(audio), 0.0)
        durations = [c.args[3] for c in q.call_args_list]
        assert durations == [None]


# ── unit: a permanent rejection is not a transient error ─────────────────────


class TestPermanentVsTransientFailure:
    """An "error" writes no terminal marker, so a PERMANENT failure reported as
    transient strands the album forever. Only a failure that could have hidden a
    hit may turn the whole lookup into an error."""

    def _http_error(self, code):
        return urllib.error.HTTPError(url="u", code=code, msg="x", hdrs=None, fp=None)

    def test_a_permanent_4xx_does_not_mask_a_clean_miss(self, tmp_path):
        audio = tmp_path / "01. Track.flac"
        audio.touch()
        # First variant is rejected outright; the next answers definitively.
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"),              patch("spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
                   side_effect=[self._http_error(400), (None, None)]):
            assert _fetch_from_lrclib(
                "Artist", "Track", "Album", 100, str(audio), 0.0) == (None, None)

    def test_a_transient_failure_still_raises(self, tmp_path):
        """Gotcha 7a intact: a blip must NOT be swept into a "no lyrics" marker,
        even when another variant came back a clean miss."""
        audio = tmp_path / "01. Track.flac"
        audio.touch()
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"),              patch("spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
                   side_effect=[self._http_error(503), (None, None)]):
            with pytest.raises(urllib.error.HTTPError):
                _fetch_from_lrclib("Artist", "Track", "Album", 100, str(audio), 0.0)

    def test_a_permanent_rejection_leaves_a_terminal_marker(self, tmp_path):
        """End to end: the stranding stops. A permanently rejected lookup with
        no lyrics anywhere writes `.nolrc`, so the album can complete."""
        audio = tmp_path / "01. Track.flac"
        _write_minimal_flac(audio, {"artist": "Artist", "title": "Track"})
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"),              patch("spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
                   side_effect=self._http_error(400)):
            assert _process_file(str(audio), 0.0) == "missing"
        assert (tmp_path / "01. Track.nolrc").exists()

    def test_embedded_lyrics_survive_a_permanent_rejection(self, tmp_path):
        """The Lateral case exactly: the file HAD embedded lyrics, and the
        raise skipped the fallback that would have completed the album."""
        audio = tmp_path / "01. Big Empty Country.flac"
        _write_minimal_flac(audio, {"artist": "Artist", "title": "Big Empty Country",
                                    "lyrics": "a line\nanother line"})
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"),              patch("spindlebot.pipeline.stages.fetch_lyrics._query_lrclib",
                   side_effect=self._http_error(400)):
            assert _process_file(str(audio), 0.0) == "plain"
        lrc = tmp_path / "01. Big Empty Country.lrc"
        assert lrc.exists() and "a line" in lrc.read_text()


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
        called_titles = {t for _, t in call_log}
        # No title_english value means no English title queries
        assert "Hyperlife" not in called_titles


# ── unit: lrclib 5xx retry ───────────────────────────────────────────────────


class TestQueryLrclibRetry:
    """lrclib serves sporadic 5xx under load — the same query alternates between
    200 and 503 seconds apart. Without a retry one blip converts a definitive
    miss into a transient error, no terminal marker is written, and the album
    never becomes lyric-complete enough to promote out of Processing."""

    def _http_error(self, code):
        return urllib.error.HTTPError(
            url="u", code=code, msg="x", hdrs=None, fp=None)

    def test_503_then_success_returns_lyrics(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=[self._http_error(503),
                                _lrclib_response(synced="[00:01.00] hi")]) as op:
            assert _query_lrclib("a", "t", "al", 100, 0.0) == ("[00:01.00] hi", None)
        assert op.call_count == 2

    def test_503_then_clean_miss_is_a_definitive_miss(self):
        # THE stranding case: the retry answers cleanly with no lyrics, so this
        # is a real miss and the caller may write the terminal .nolrc marker.
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=[self._http_error(503),
                                _lrclib_response(synced=None, plain=None)]):
            assert _query_lrclib("a", "t", "al", 100, 0.0) == (None, None)

    def test_persistent_503_still_raises_after_bounded_attempts(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(503)) as op:
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)
        assert op.call_count == LRCLIB_RETRY_ATTEMPTS

    def test_connection_error_is_retried(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=[urllib.error.URLError("refused"),
                                _lrclib_response(plain="verse")]) as op:
            assert _query_lrclib("a", "t", "al", 100, 0.0) == (None, "verse")
        assert op.call_count == 2

    def test_429_is_retried(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=[self._http_error(429),
                                _lrclib_response(synced="[00:01.00] hi")]) as op:
            assert _query_lrclib("a", "t", "al", 100, 0.0) == ("[00:01.00] hi", None)
        assert op.call_count == 2

    def test_400_is_not_retried(self):
        # A 4xx other than 429 is the server answering definitively; retrying
        # it only burns requests against a rate-limited service.
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(400)) as op:
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)
        assert op.call_count == 1

    def test_404_is_not_retried(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(404)) as op:
            assert _query_lrclib("a", "t", "al", 100, 0.0) == (None, None)
        assert op.call_count == 1

    def test_attempt_count_follows_the_module_constant(self):
        # The seam the removed `attempts=` parameter was meant to provide: the
        # bound is a module constant, so it is patchable and cannot be handed a
        # nonsense value (attempts=0 would have left last_exc None -> raise None).
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.LRCLIB_RETRY_ATTEMPTS", 2), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(503)) as op:
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)
        assert op.call_count == 2

    def test_query_takes_no_attempts_override(self):
        # Removed rather than guarded: nothing passed it, and a caller-supplied
        # 0 would skip the loop and `raise None`. Keeping the surface small means
        # that state is unreachable instead of merely rejected.
        import inspect
        assert "attempts" not in inspect.signature(_query_lrclib).parameters

    def test_backoff_grows_between_attempts(self):
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep") as slp, \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=self._http_error(503)):
            with pytest.raises(urllib.error.HTTPError):
                _query_lrclib("a", "t", "al", 100, 0.0)
        waits = [c.args[0] for c in slp.call_args_list if c.args and c.args[0] > 0]
        assert waits == [LRCLIB_RETRY_BACKOFF, LRCLIB_RETRY_BACKOFF * 2]


class TestStrandingRegression:
    """End to end: the shape that left four albums sitting in Processing."""

    def test_transient_503_no_longer_strands_a_lyricless_track(self, tmp_path):
        # An instrumental: lrclib genuinely has nothing, and the first call 503s.
        # Pre-fix this wrote no marker at all and the album never promoted.
        audio = tmp_path / "01. Overture.flac"
        _write_minimal_flac(audio, {"artist": "A", "title": "Overture", "album": "Al"})
        err = urllib.error.HTTPError(url="u", code=503, msg="x", hdrs=None, fp=None)
        with patch("spindlebot.pipeline.stages.fetch_lyrics.time.sleep"), \
             patch("spindlebot.pipeline.stages.fetch_lyrics.urllib.request.urlopen",
                   side_effect=[err] + [_lrclib_response(synced=None, plain=None)
                                        for _ in range(20)]):
            outcome = _process_file(str(audio), 0.0)

        assert outcome == "missing"
        assert (tmp_path / "01. Overture.nolrc").exists()
        assert album_lyrics_complete(tmp_path)
