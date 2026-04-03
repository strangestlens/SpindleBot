"""
Tests for music-pretag.py — tag cleanup, Bandcamp encoding fix, multi-format support.

We build minimal real FLAC/audio fixtures using mutagen so tests never touch
real music files and run identically in CI.
"""
import os
import sys
import struct
import unittest
from pathlib import Path

import mutagen.flac
import mutagen.id3

sys.path.insert(0, str(Path(__file__).parent.parent))

# music-pretag.py is registered as "music_pretag" by tests/conftest.py
from music_pretag import pretag, posttag, _fix_bandcamp_album_encoding  # noqa: E402


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _write_minimal_flac(path: str, tags: dict) -> None:
    """
    Write a minimal valid FLAC file with the given Vorbis comment tags.

    Structure: fLaC marker + STREAMINFO metadata block (last block, 34 bytes).
    STREAMINFO bit layout:
      min_blocksize(16) max_blocksize(16) min_framesize(24) max_framesize(24)
      sample_rate(20) channels(3) bps(5) total_samples(36) md5(128)
    Total payload = 2+2+3+3+8+16 = 34 bytes.
    """
    si = bytearray(34)
    si[0:2]  = struct.pack(">H", 4096)   # min blocksize
    si[2:4]  = struct.pack(">H", 4096)   # max blocksize
    si[4:7]  = b"\x00\x00\x00"           # min framesize (unknown)
    si[7:10] = b"\x00\x00\x00"           # max framesize (unknown)
    # 8 bytes: sample_rate(20b)=44100 | channels(3b)=0 | bps(5b)=15 | total_samples(36b)=0
    combined = (44100 << 44) | (0 << 41) | (15 << 36)
    si[10:18] = combined.to_bytes(8, "big")
    si[18:34] = b"\x00" * 16             # MD5

    # Metadata block header: last-block=1, type=0 (STREAMINFO), length=34
    block_hdr = bytearray(4)
    block_hdr[0] = 0x80  # last-metadata-block=1, block-type=0
    block_hdr[1:4] = (34).to_bytes(3, "big")

    with open(path, "wb") as fh:
        fh.write(b"fLaC")
        fh.write(bytes(block_hdr))
        fh.write(bytes(si))

    # Attach Vorbis comment tags using mutagen
    f = mutagen.flac.FLAC(path)
    f.add_tags()
    for k, v in tags.items():
        f.tags[k] = [str(v)]
    f.save()


def _make_easy_tags(tags: dict):
    """
    Return a mock object that behaves like a mutagen EasyMP3/EasyID3 tag dict.
    Used to test our pretag logic without needing a real parseable audio file.
    """
    from unittest.mock import MagicMock

    store = {k.lower(): [str(v)] for k, v in tags.items()}
    saved = {}

    mock_file = MagicMock()
    mock_file.tags = store
    mock_file.save = MagicMock(side_effect=lambda *a, **kw: saved.update(store))
    return mock_file, saved


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPretagFLAC(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _flac(self, name: str, tags: dict) -> Path:
        p = self.tmp / name
        _write_minimal_flac(str(p), tags)
        return p

    def test_pretag_no_changes_needed(self):
        """Clean tags: artist == albumartist, no junk — pretag is a no-op."""
        self._flac("01.flac", {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1",
        })
        result = pretag(str(self.tmp))
        self.assertTrue(result)

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertEqual(f.tags["artist"][0], "Band")
        self.assertEqual(f.tags["title"][0], "Song")

    def test_pretag_removes_xld_junk_tags(self):
        self._flac("01.flac", {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1",
            "year": "2020", "track": "1", "totaltracks": "10",
        })
        pretag(str(self.tmp))

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertNotIn("year", f.tags)
        self.assertNotIn("track", f.tags)
        self.assertNotIn("totaltracks", f.tags)

    def test_pretag_removes_compilation_tag(self):
        self._flac("01.flac", {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1", "compilation": "1",
        })
        pretag(str(self.tmp))

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertNotIn("compilation", f.tags)

    def test_pretag_moves_feat_into_title(self):
        self._flac("01.flac", {
            "title": "Cool Song", "artist": "Band feat. Other Artist",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        pretag(str(self.tmp))

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertEqual(f.tags["title"][0], "Cool Song (feat. Other Artist)")
        self.assertEqual(f.tags["artist"][0], "Band")

    def test_pretag_normalizes_artist_to_albumartist(self):
        self._flac("01.flac", {
            "title": "Song", "artist": "The Band",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        pretag(str(self.tmp))

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertEqual(f.tags["artist"][0], "Band")

    def test_pretag_skips_various_artists(self):
        """VA compilations: each track keeps its own artist."""
        self._flac("01.flac", {
            "title": "Song", "artist": "Track Artist",
            "albumartist": "Various Artists", "album": "Comp", "tracknumber": "1",
        })
        pretag(str(self.tmp))

        f = mutagen.flac.FLAC(str(self.tmp / "01.flac"))
        self.assertEqual(f.tags["artist"][0], "Track Artist")

    def test_pretag_handles_empty_dir(self):
        """Empty directory: returns False, does not raise."""
        result = pretag(str(self.tmp))
        self.assertFalse(result)

    def test_pretag_processes_multiple_files(self):
        for i in range(1, 4):
            self._flac(f"0{i}.flac", {
                "title": f"Track {i}", "artist": "The Band",
                "albumartist": "Band", "album": "Record", "tracknumber": str(i),
            })
        pretag(str(self.tmp))

        for i in range(1, 4):
            f = mutagen.flac.FLAC(str(self.tmp / f"0{i}.flac"))
            self.assertEqual(f.tags["artist"][0], "Band")


class TestBandcampAlbumFix(unittest.TestCase):
    """Unit tests for the Bandcamp album encoding fix."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_tags(self, album: str, comment: str = "") -> dict:
        """Return a dict-like object that _fix_bandcamp_album_encoding can work with."""
        return {
            "album":   [album],
            "comment": [comment],
        }

    def test_fixes_ascii_mangled_album_when_bandcamp_comment(self):
        # Directory name has real Unicode; tag has ASCII apostrophe substitution
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._make_tags("Aqu'aticos", "Visit https://music-from-memory.bandcamp.com")

        from music_pretag import _fix_bandcamp_album_encoding
        changed = _fix_bandcamp_album_encoding(tags, album_dir)

        self.assertTrue(changed)
        self.assertEqual(tags["album"][0], "Aquáticos")

    def test_no_change_without_bandcamp_comment(self):
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._make_tags("Aqu'aticos", "")  # no bandcamp comment

        from music_pretag import _fix_bandcamp_album_encoding
        changed = _fix_bandcamp_album_encoding(tags, album_dir)

        self.assertFalse(changed)

    def test_no_change_when_album_already_correct(self):
        album_dir = str(self.tmp / "Aquáticos")
        os.makedirs(album_dir)
        tags = self._make_tags("Aquáticos", "Visit https://label.bandcamp.com")

        from music_pretag import _fix_bandcamp_album_encoding
        changed = _fix_bandcamp_album_encoding(tags, album_dir)

        self.assertFalse(changed)

    def test_no_change_when_dir_is_ascii(self):
        album_dir = str(self.tmp / "Normal Album")
        os.makedirs(album_dir)
        tags = self._make_tags("Normal Album", "Visit https://artist.bandcamp.com")

        from music_pretag import _fix_bandcamp_album_encoding
        changed = _fix_bandcamp_album_encoding(tags, album_dir)

        self.assertFalse(changed)

    def test_handles_artist_prefix_in_dir_name(self):
        # Bandcamp often names dirs "Artist - Album"
        album_dir = str(self.tmp / "CLANN - Seelie")
        os.makedirs(album_dir)
        # If the album tag already matches after stripping prefix, no change needed
        tags = self._make_tags("Seelie", "Visit https://clann.bandcamp.com")

        from music_pretag import _fix_bandcamp_album_encoding
        # "Seelie" is ASCII, dir "CLANN - Seelie" has no Unicode → no change
        changed = _fix_bandcamp_album_encoding(tags, album_dir)
        self.assertFalse(changed)

    def test_bandcamp_comment_preserved_after_pretag(self):
        """Bandcamp COMMENT tag must NOT be removed by pretag."""
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


class TestPretagNonFLAC(unittest.TestCase):
    """
    Verify pretag logic for non-FLAC formats by mocking mutagen.File.

    We test our tag-manipulation logic here, not mutagen's ability to parse
    specific audio container formats.  Real format-level integration is covered
    by the FLAC tests above (which use actual files) and by manual QA.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mock_pretag_other(self, tags_in: dict) -> dict:
        """
        Call _pretag_other with a mocked mutagen.File and return the resulting tag state.
        """
        from unittest.mock import patch, MagicMock

        mock_file = MagicMock()
        mock_file.tags = {k.lower(): [str(v)] for k, v in tags_in.items()}
        mock_file.save = MagicMock()

        # Create a fake audio file path — it will never be opened since mutagen.File is mocked
        fake_path = str(self.tmp / "01.mp3")
        with open(fake_path, "w") as fh:
            fh.write("")  # placeholder so the file exists

        with patch("mutagen.File", return_value=mock_file):
            from music_pretag import _pretag_other
            _pretag_other(fake_path, str(self.tmp))

        return mock_file.tags

    def test_pretag_normalizes_artist_in_non_flac(self):
        result = self._mock_pretag_other({
            "title": "Song", "artist": "The Band",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        self.assertEqual(result["artist"][0], "Band")

    def test_pretag_moves_feat_into_title_in_non_flac(self):
        result = self._mock_pretag_other({
            "title": "Cool Song", "artist": "Band feat. Other Artist",
            "albumartist": "Band", "album": "Record", "tracknumber": "1",
        })
        self.assertEqual(result["title"][0], "Cool Song (feat. Other Artist)")
        self.assertEqual(result["artist"][0], "Band")

    def test_pretag_skips_va_in_non_flac(self):
        result = self._mock_pretag_other({
            "title": "Song", "artist": "Track Artist",
            "albumartist": "Various Artists", "album": "Comp", "tracknumber": "1",
        })
        self.assertEqual(result["artist"][0], "Track Artist")

    def test_pretag_mixed_formats_counts_all_audio(self):
        """
        find_audio_files must return both FLAC and MP3 when both exist.
        We verify via the file count in pretag's output.
        """
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch, MagicMock

        flac_path = str(self.tmp / "01.flac")
        mp3_path  = str(self.tmp / "02.mp3")
        _write_minimal_flac(flac_path, {
            "title": "Track 1", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1",
        })
        with open(mp3_path, "w") as fh:
            fh.write("")  # placeholder

        mock_mp3 = MagicMock()
        mock_mp3.tags = {"title": ["Track 2"], "artist": ["Band"], "albumartist": ["Band"], "album": ["Record"]}
        mock_mp3.save = MagicMock()

        buf = io.StringIO()
        with patch("mutagen.File", return_value=mock_mp3):
            with redirect_stdout(buf):
                result = pretag(str(self.tmp))

        self.assertTrue(result)
        # "pretag done: 2 files in ..."
        self.assertIn("2 files", buf.getvalue())


class TestPosttag(unittest.TestCase):

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_posttag_truncates_date_to_year(self):
        path = str(self.tmp / "01.flac")
        _write_minimal_flac(path, {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1", "date": "2024-06-15",
        })
        posttag([path])

        f = mutagen.flac.FLAC(path)
        self.assertEqual(f.tags["date"][0], "2024")

    def test_posttag_leaves_year_only_date_unchanged(self):
        path = str(self.tmp / "01.flac")
        _write_minimal_flac(path, {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1", "date": "2024",
        })
        posttag([path])

        f = mutagen.flac.FLAC(path)
        self.assertEqual(f.tags["date"][0], "2024")

    def test_posttag_removes_beets_alias_tags(self):
        path = str(self.tmp / "01.flac")
        _write_minimal_flac(path, {
            "title": "Song", "artist": "Band", "albumartist": "Band",
            "album": "Record", "tracknumber": "1",
            "year": "2024", "track": "1",
        })
        posttag([path])

        f = mutagen.flac.FLAC(path)
        self.assertNotIn("year", f.tags)
        self.assertNotIn("track", f.tags)

    def test_posttag_skips_nonexistent_paths(self):
        """Should not raise on missing files."""
        posttag(["/nonexistent/path/file.flac", ""])

    def test_posttag_skips_non_audio_extensions(self):
        """Should silently skip .jpg, .log, etc."""
        jpg = str(self.tmp / "cover.jpg")
        with open(jpg, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0" + b"\x00" * 20)  # minimal JPEG
        posttag([jpg])  # should not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
