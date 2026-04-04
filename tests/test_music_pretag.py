"""
Tests for music-pretag.py — Bandcamp encoding fix and multi-format support.

music-pretag.py is the format-agnostic root-level script (handles FLAC + other
audio formats, Bandcamp ALBUM tag encoding fix). Registered as "music_pretag"
by tests/conftest.py.

Basic FLAC pretag/posttag logic is covered by tests/test_pretag.py (which tests
spindlebot.pipeline.stages.pretag, the extracted stage module).
"""
from __future__ import annotations

import io
import os
import struct
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac

from music_pretag import _fix_bandcamp_album_encoding, _pretag_other, posttag, pretag


# ── helpers ───────────────────────────────────────────────────────────────────


def _write_minimal_flac(path: str, tags: dict) -> None:
    si = bytearray(34)
    si[0:2] = struct.pack(">H", 4096)
    si[2:4] = struct.pack(">H", 4096)
    si[4:7] = b"\x00\x00\x00"
    si[7:10] = b"\x00\x00\x00"
    combined = (44100 << 44) | (0 << 41) | (15 << 36)
    si[10:18] = combined.to_bytes(8, "big")
    si[18:34] = b"\x00" * 16

    block_hdr = bytearray(4)
    block_hdr[0] = 0x80
    block_hdr[1:4] = (34).to_bytes(3, "big")

    with open(path, "wb") as fh:
        fh.write(b"fLaC")
        fh.write(bytes(block_hdr))
        fh.write(bytes(si))

    f = mutagen.flac.FLAC(path)
    f.add_tags()
    for k, v in tags.items():
        f.tags[k] = [str(v)]
    f.save()


# ── Bandcamp album encoding fix ───────────────────────────────────────────────


class TestBandcampAlbumFix(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _tags(self, album: str, comment: str = "") -> dict:
        return {"album": [album], "comment": [comment]}

    def test_fixes_ascii_mangled_album_when_bandcamp_comment(self):
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._tags("Aqu'aticos", "Visit https://music-from-memory.bandcamp.com")

        changed = _fix_bandcamp_album_encoding(tags, album_dir)

        self.assertTrue(changed)
        self.assertEqual(tags["album"][0], "Aquáticos")

    def test_no_change_without_bandcamp_comment(self):
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._tags("Aqu'aticos", "")

        self.assertFalse(_fix_bandcamp_album_encoding(tags, album_dir))

    def test_no_change_when_album_already_correct(self):
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._tags("Aquáticos", "Visit https://label.bandcamp.com")

        self.assertFalse(_fix_bandcamp_album_encoding(tags, album_dir))

    def test_no_change_when_dir_is_ascii(self):
        album_dir = str(self.tmp / "Normal Album")
        os.makedirs(album_dir)
        tags = self._tags("Normal Album", "Visit https://artist.bandcamp.com")

        self.assertFalse(_fix_bandcamp_album_encoding(tags, album_dir))

    def test_handles_artist_prefix_in_dir_name(self):
        album_dir = str(self.tmp / "CLANN - Seelie")
        os.makedirs(album_dir)
        tags = self._tags("Seelie", "Visit https://clann.bandcamp.com")

        # "Seelie" is ASCII, dir has no Unicode → no change needed
        self.assertFalse(_fix_bandcamp_album_encoding(tags, album_dir))

    def test_bandcamp_comment_preserved_after_pretag(self):
        """Bandcamp COMMENT tag must not be removed by pretag."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            flac_path = os.path.join(td, "01.flac")
            _write_minimal_flac(flac_path, {
                "title": "Song", "artist": "Band", "albumartist": "Band",
                "album": "Seelie", "tracknumber": "1",
                "comment": "Visit https://clann.bandcamp.com",
            })
            pretag(td)
            f = mutagen.flac.FLAC(flac_path)
            self.assertIn("comment", f.tags)
            self.assertIn("bandcamp.com", f.tags["comment"][0])


# ── Non-FLAC format support ───────────────────────────────────────────────────


class TestPretagNonFLAC(unittest.TestCase):
    """
    Verify pretag logic for non-FLAC formats by mocking mutagen.File.

    We test tag-manipulation logic here, not mutagen's format parsing. Real
    format-level integration is covered by the FLAC tests and manual QA.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mock_pretag_other(self, tags_in: dict) -> dict:
        mock_file = MagicMock()
        mock_file.tags = {k.lower(): [str(v)] for k, v in tags_in.items()}
        mock_file.save = MagicMock()

        fake_path = str(self.tmp / "01.mp3")
        with open(fake_path, "w") as fh:
            fh.write("")

        with patch("mutagen.File", return_value=mock_file):
            _pretag_other(fake_path, str(self.tmp))

        return mock_file.tags

    def test_normalizes_artist_to_albumartist(self):
        result = self._mock_pretag_other({
            "title": "Song", "artist": "The Band",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        self.assertEqual(result["artist"][0], "Band")

    def test_moves_feat_into_title(self):
        result = self._mock_pretag_other({
            "title": "Cool Song", "artist": "Band feat. Other Artist",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        self.assertEqual(result["title"][0], "Cool Song (feat. Other Artist)")
        self.assertEqual(result["artist"][0], "Band")

    def test_skips_various_artists(self):
        result = self._mock_pretag_other({
            "title": "Song", "artist": "Track Artist",
            "albumartist": "Various Artists", "album": "Comp", "tracknumber": "1",
        })
        self.assertEqual(result["artist"][0], "Track Artist")

    def test_mixed_formats_counts_all_audio(self):
        """find_audio_files must return both FLAC and MP3 when both exist."""
        flac_path = str(self.tmp / "01.flac")
        mp3_path = str(self.tmp / "02.mp3")
        _write_minimal_flac(flac_path, {
            "title": "Track 1", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1",
        })
        with open(mp3_path, "w") as fh:
            fh.write("")

        mock_mp3 = MagicMock()
        mock_mp3.tags = {
            "title": ["Track 2"], "artist": ["Band"],
            "albumartist": ["Band"], "album": ["Record"],
        }
        mock_mp3.save = MagicMock()

        buf = io.StringIO()
        with patch("mutagen.File", return_value=mock_mp3):
            with redirect_stdout(buf):
                result = pretag(str(self.tmp))

        self.assertTrue(result)
        self.assertIn("2 files", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
