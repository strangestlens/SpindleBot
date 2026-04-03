"""
Tests for spindlebot.disc — disc-detection logic used by music-import.sh.
"""
import unittest
from unittest.mock import MagicMock, patch

from spindlebot.disc import check_wait, count_discs


def _mock_flac(discnumber: int = 1, disctotal: int = 1) -> MagicMock:
    """Return a mock mutagen.flac.FLAC object with the given disc tags."""
    m = MagicMock()
    m.tags = {
        "discnumber": [str(discnumber)],
        "disctotal":  [str(disctotal)],
    }
    return m


def _patch_flacs(flacs: list) -> "contextlib.AbstractContextManager":
    """Patch _read_disc_tags to return sets derived from the given mock FLACs."""
    disc_totals = {int(f.tags["disctotal"][0]) for f in flacs}
    disc_numbers = {int(f.tags["discnumber"][0]) for f in flacs}
    return patch(
        "spindlebot.disc._read_disc_tags",
        return_value=(disc_totals, disc_numbers),
    )


class TestCheckWait(unittest.TestCase):

    def test_single_disc_passes(self):
        with _patch_flacs([_mock_flac(discnumber=1, disctotal=1)]):
            self.assertIsNone(check_wait("/fake/dir"))

    def test_missing_disc2_triggers_wait(self):
        # MusicBrainz says disctotal=2 but we only have disc 1
        with _patch_flacs([_mock_flac(discnumber=1, disctotal=2)]):
            result = check_wait("/fake/dir")
        self.assertEqual(result, "WAIT:1:2")

    def test_all_discs_present_passes(self):
        with _patch_flacs([
            _mock_flac(discnumber=1, disctotal=2),
            _mock_flac(discnumber=2, disctotal=2),
        ]):
            self.assertIsNone(check_wait("/fake/dir"))

    def test_empty_dir_passes(self):
        with patch("spindlebot.disc._read_disc_tags", return_value=(set(), set())):
            self.assertIsNone(check_wait("/fake/empty"))

    def test_inconsistent_disctotals_passes(self):
        # If files disagree on disctotal, don't hold — let beets sort it out
        with patch("spindlebot.disc._read_disc_tags", return_value=({1, 2}, {1})):
            self.assertIsNone(check_wait("/fake/dir"))

    def test_wait_format(self):
        # Three-disc set, only have disc 1 and 2
        with patch("spindlebot.disc._read_disc_tags", return_value=({3}, {1, 2})):
            result = check_wait("/fake/dir")
        self.assertEqual(result, "WAIT:2:3")


class TestCountDiscs(unittest.TestCase):

    def test_single_disc(self):
        with _patch_flacs([_mock_flac(discnumber=1, disctotal=1)]):
            self.assertEqual(count_discs("/fake/dir"), 1)

    def test_two_discs(self):
        with _patch_flacs([
            _mock_flac(discnumber=1, disctotal=2),
            _mock_flac(discnumber=2, disctotal=2),
        ]):
            self.assertEqual(count_discs("/fake/dir"), 2)

    def test_empty_dir_returns_one(self):
        with patch("spindlebot.disc._read_disc_tags", return_value=(set(), set())):
            self.assertEqual(count_discs("/fake/empty"), 1)

    def test_multiple_tracks_same_disc(self):
        # 10 tracks all on disc 1 → count is 1
        with _patch_flacs([_mock_flac(discnumber=1, disctotal=1)] * 10):
            self.assertEqual(count_discs("/fake/dir"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
