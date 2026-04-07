"""
Tests for spindlebot.pipeline.stages.fetch_art.
"""
from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac
import pytest

from spindlebot.pipeline.stages.fetch_art import (
    ArtResult,
    _detect_mime,
    _embed_art_in_file,
    _fetch_from_caa,
    _fetch_from_itunes,
    _get_album_tags,
    _has_art,
    fetch_art,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg() -> MagicMock:
    return MagicMock()


_FAKE_JPEG = b"\xff\xd8" + b"\x00" * 10
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10


def _write_minimal_flac(path: Path, tags: dict) -> None:
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


def _urlopen_returning(data: bytes):
    """Build a mock urlopen context manager that returns data bytes."""
    resp = io.BytesIO(data)
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=resp)
    mock_cm.__exit__ = MagicMock(return_value=False)
    return mock_cm


def _urlopen_json(payload: dict):
    return _urlopen_returning(json.dumps(payload).encode())


# ── unit: helpers ─────────────────────────────────────────────────────────────


class TestHelpers:

    def test_detect_mime_jpeg(self):
        assert _detect_mime(_FAKE_JPEG) == "image/jpeg"

    def test_detect_mime_png(self):
        assert _detect_mime(_FAKE_PNG) == "image/png"

    def test_detect_mime_defaults_to_jpeg(self):
        assert _detect_mime(b"\x00\x00\x00") == "image/jpeg"


# ── unit: _has_art ────────────────────────────────────────────────────────────


class TestHasArt:

    def test_flac_without_art_returns_false(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song"})
        assert _has_art(str(f)) is False

    def test_flac_with_art_returns_true(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song"})

        flac = mutagen.flac.FLAC(str(f))
        pic = mutagen.flac.Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = _FAKE_JPEG
        flac.add_picture(pic)
        flac.save()

        assert _has_art(str(f)) is True

    def test_nonexistent_file_returns_false(self, tmp_path):
        assert _has_art(str(tmp_path / "missing.flac")) is False

    def test_unreadable_file_returns_false(self, tmp_path):
        bad = tmp_path / "bad.flac"
        bad.write_bytes(b"not a flac")
        assert _has_art(str(bad)) is False


# ── unit: _fetch_from_caa ─────────────────────────────────────────────────────


class TestFetchFromCaa:

    def _make_caa_index(self, img_url: str) -> bytes:
        return json.dumps({
            "images": [{"image": img_url, "front": True}]
        }).encode()

    def test_returns_bytes_on_success(self):
        index_resp = _urlopen_returning(self._make_caa_index("https://example.com/cover.jpg"))
        img_resp = _urlopen_returning(_FAKE_JPEG)

        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            side_effect=[index_resp, img_resp],
        ):
            result = _fetch_from_caa("mbid-123")

        assert result == _FAKE_JPEG

    def test_returns_none_on_http_error(self):
        import urllib.error
        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None),
        ):
            assert _fetch_from_caa("bad-mbid") is None

    def test_returns_none_when_no_images(self):
        resp = _urlopen_json({"images": []})
        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            return_value=resp,
        ):
            assert _fetch_from_caa("mbid-123") is None


# ── unit: _fetch_from_itunes ──────────────────────────────────────────────────


class TestFetchFromItunes:

    def test_returns_bytes_on_success(self):
        search_payload = {
            "results": [{"artworkUrl100": "https://example.com/100x100bb.jpg"}]
        }
        search_resp = _urlopen_json(search_payload)
        img_resp = _urlopen_returning(_FAKE_JPEG)

        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            side_effect=[search_resp, img_resp],
        ):
            result = _fetch_from_itunes("Band", "Album")

        assert result == _FAKE_JPEG

    def test_returns_none_on_no_results(self):
        resp = _urlopen_json({"results": []})
        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            return_value=resp,
        ):
            assert _fetch_from_itunes("Band", "Album") is None

    def test_returns_none_on_network_error(self):
        with patch(
            "spindlebot.pipeline.stages.fetch_art.urllib.request.urlopen",
            side_effect=OSError("network unreachable"),
        ):
            assert _fetch_from_itunes("Band", "Album") is None


# ── unit: _embed_art_in_file ──────────────────────────────────────────────────


class TestEmbedArtInFile:

    def test_embeds_jpeg_in_flac(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song"})
        assert not mutagen.flac.FLAC(str(f)).pictures

        _embed_art_in_file(str(f), _FAKE_JPEG, "image/jpeg")

        pics = mutagen.flac.FLAC(str(f)).pictures
        assert len(pics) == 1
        assert pics[0].mime == "image/jpeg"
        assert pics[0].data == _FAKE_JPEG

    def test_replaces_existing_art_in_flac(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song"})

        # Embed initial art
        _embed_art_in_file(str(f), _FAKE_JPEG, "image/jpeg")
        # Replace with PNG
        _embed_art_in_file(str(f), _FAKE_PNG, "image/png")

        pics = mutagen.flac.FLAC(str(f)).pictures
        assert len(pics) == 1
        assert pics[0].data == _FAKE_PNG


# ── integration: fetch_art ───────────────────────────────────────────────────


class TestFetchArt:

    def test_empty_dir_returns_zero(self, tmp_path):
        result = fetch_art(tmp_path, _cfg())
        assert result.embedded == 0
        assert result.skipped == 0

    def test_already_has_art_skipped(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Rec"})

        flac = mutagen.flac.FLAC(str(f))
        pic = mutagen.flac.Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = _FAKE_JPEG
        flac.add_picture(pic)
        flac.save()

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa") as mock_caa, \
             patch("spindlebot.pipeline.stages.fetch_art._fetch_from_itunes") as mock_itunes:
            result = fetch_art(tmp_path, _cfg())

        mock_caa.assert_not_called()
        mock_itunes.assert_not_called()
        assert result.skipped == 1

    def test_art_embedded_from_caa(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "title": "Song", "album": "Rec",
            "musicbrainz_albumid": "mbid-abc",
        })

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa",
                   return_value=_FAKE_JPEG):
            result = fetch_art(tmp_path, _cfg())

        assert result.embedded == 1
        assert _has_art(str(f))
        assert (tmp_path / "cover.jpg").read_bytes() == _FAKE_JPEG

    def test_art_embedded_from_itunes_when_caa_misses(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Rec"})

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa", return_value=None), \
             patch("spindlebot.pipeline.stages.fetch_art._fetch_from_itunes",
                   return_value=_FAKE_JPEG):
            result = fetch_art(tmp_path, _cfg())

        assert result.embedded == 1

    def test_missing_art_recorded(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Rec"})

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa", return_value=None), \
             patch("spindlebot.pipeline.stages.fetch_art._fetch_from_itunes", return_value=None):
            result = fetch_art(tmp_path, _cfg())

        assert result.missing == 1
        assert result.embedded == 0
        assert not (tmp_path / "cover.jpg").exists()

    def test_dry_run_does_not_embed(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {"artist": "Band", "title": "Song", "album": "Rec"})

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa",
                   return_value=_FAKE_JPEG):
            result = fetch_art(tmp_path, _cfg(), dry_run=True)

        assert result.embedded == 1
        assert not _has_art(str(f))
        assert not (tmp_path / "cover.jpg").exists()

    def test_force_reembeds_when_art_exists(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "title": "Song", "album": "Rec",
            "musicbrainz_albumid": "mbid-abc",
        })

        # Pre-embed original art
        _embed_art_in_file(str(f), _FAKE_JPEG, "image/jpeg")

        new_art = b"\xff\xd8" + b"\x01" * 20

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa",
                   return_value=new_art):
            result = fetch_art(tmp_path, _cfg(), force=True)

        assert result.embedded == 1
        pics = mutagen.flac.FLAC(str(f)).pictures
        assert pics[0].data == new_art

    def test_cover_jpg_written_alongside_files(self, tmp_path):
        f = tmp_path / "track.flac"
        _write_minimal_flac(f, {
            "artist": "Band", "title": "Song", "album": "Rec",
            "musicbrainz_albumid": "mbid-abc",
        })

        with patch("spindlebot.pipeline.stages.fetch_art._fetch_from_caa",
                   return_value=_FAKE_JPEG):
            fetch_art(tmp_path, _cfg())

        assert (tmp_path / "cover.jpg").exists()
        assert (tmp_path / "cover.jpg").read_bytes() == _FAKE_JPEG
