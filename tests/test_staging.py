"""
Tests for spindlebot.staging.scan_staging().

All tests use tmp_path fixtures — no real Staging directory is touched.
"""
import unittest
from pathlib import Path

from spindlebot.staging import scan_staging, StagingItem


def _touch(path: Path, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


class TestScanStagingEmpty(unittest.TestCase):

    def test_empty_directory_returns_empty_list(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = scan_staging(td)
        self.assertEqual(result, [])

    def test_nonexistent_directory_returns_empty_list(self):
        result = scan_staging("/nonexistent/path/that/does/not/exist")
        self.assertEqual(result, [])

    def test_staging_with_only_non_audio_files_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td)
            subdir = staging / "Scans"
            subdir.mkdir()
            _touch(subdir / "front.jpg")
            _touch(subdir / "back.jpg")
            result = scan_staging(td)
        self.assertEqual(result, [])


class TestScanStagingDigitalDownload(unittest.TestCase):
    """Bandcamp / digital download: subdirectory with audio files, no .log."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_single_bandcamp_album_dir(self):
        album_dir = self.staging / "CLANN - Seelie"
        album_dir.mkdir()
        _touch(album_dir / "01 Caves.flac")
        _touch(album_dir / "cover.jpg")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "dir")
        self.assertEqual(result[0].path, album_dir)

    def test_multiple_bandcamp_albums(self):
        for name in ["Artist A - Album 1", "Artist B - Album 2", "Artist C - Album 3"]:
            d = self.staging / name
            d.mkdir()
            _touch(d / "01.flac")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 3)
        self.assertTrue(all(item.kind == "dir" for item in result))
        # Alphabetical order
        names = [item.path.name for item in result]
        self.assertEqual(names, sorted(names))

    def test_non_flac_audio_formats_detected(self):
        for ext in ["mp3", "m4a", "ogg", "opus", "wav", "aiff"]:
            d = self.staging / f"Album {ext}"
            d.mkdir()
            _touch(d / f"01.{ext}")

        result = scan_staging(self.staging)
        self.assertEqual(len(result), 6)

    def test_extra_assets_do_not_affect_detection(self):
        """A directory with audio AND extra images should still be detected."""
        album_dir = self.staging / "CLANN - Seelie"
        album_dir.mkdir()
        _touch(album_dir / "CLANN - Seelie - 01 Caves.flac")
        _touch(album_dir / "cover.jpg")
        _touch(album_dir / "inside panel.jpg")
        _touch(album_dir / "CLANN - Seelie - Faerie Court.jpg")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "dir")

    def test_hidden_directories_are_skipped(self):
        hidden = self.staging / ".DS_Store_album"
        hidden.mkdir()
        _touch(hidden / "01.flac")

        result = scan_staging(self.staging)
        self.assertEqual(result, [])


class TestScanStagingCDRip(unittest.TestCase):
    """XLD CD rip: subdir containing audio files and a .log file."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_subdir_with_log_uses_log_trigger(self):
        album_dir = self.staging / "The Album"
        album_dir.mkdir()
        _touch(album_dir / "01.flac")
        log = _touch(album_dir / "The Album.log")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "log")
        self.assertEqual(result[0].path, log)

    def test_root_level_log_file(self):
        """XLD rips where files land directly in Staging root."""
        _touch(self.staging / "01.flac")
        _touch(self.staging / "02.flac")
        log = _touch(self.staging / "Album.log")

        result = scan_staging(self.staging)

        # Root-level .log files are appended after subdirs
        log_items = [i for i in result if i.kind == "log"]
        self.assertTrue(any(i.path == log for i in log_items))

    def test_multiple_logs_in_subdir_uses_first(self):
        """If somehow multiple .log files exist, we use the first alphabetically."""
        album_dir = self.staging / "Multi"
        album_dir.mkdir()
        _touch(album_dir / "01.flac")
        log_a = _touch(album_dir / "aaa.log")
        _touch(album_dir / "zzz.log")

        result = scan_staging(self.staging)

        self.assertEqual(result[0].path, log_a)


class TestScanStagingMixed(unittest.TestCase):
    """Staging dir with both CD rips (subdirs with .log) and Bandcamp dirs."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mixed_staging(self):
        # Bandcamp album — no log
        bc = self.staging / "CLANN - Seelie"
        bc.mkdir()
        _touch(bc / "01.flac")

        # CD rip — has log
        cd = self.staging / "Sean Wolcott - Crystal Eyes"
        cd.mkdir()
        _touch(cd / "01.flac")
        _touch(cd / "Crystal Eyes.log")

        # Another Bandcamp album
        bc2 = self.staging / "Aquáticos"
        bc2.mkdir()
        _touch(bc2 / "01.flac")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 3)
        kinds = {item.path.name: item.kind for item in result}
        self.assertEqual(kinds["CLANN - Seelie"], "dir")
        self.assertEqual(kinds["Crystal Eyes.log"], "log")
        self.assertEqual(kinds["Aquáticos"], "dir")

    def test_dir_without_audio_is_excluded(self):
        """A subdir containing only .jpg and .pdf should be silently skipped."""
        scans_dir = self.staging / "Scans"
        scans_dir.mkdir()
        _touch(scans_dir / "front.jpg")
        _touch(scans_dir / "booklet.pdf")

        album_dir = self.staging / "Real Album"
        album_dir.mkdir()
        _touch(album_dir / "01.flac")

        result = scan_staging(self.staging)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].path.name, "Real Album")

    def test_result_type_is_staging_item(self):
        d = self.staging / "Album"
        d.mkdir()
        _touch(d / "01.flac")

        result = scan_staging(self.staging)

        self.assertIsInstance(result[0], StagingItem)
        self.assertIn(result[0].kind, ("log", "dir"))
        self.assertIsInstance(result[0].path, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
