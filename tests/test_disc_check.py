"""
Tests for spindlebot.disc — disc-detection logic used by music-import.sh.
"""
import contextlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mutagen.flac

from spindlebot.core.albums import album_key
from spindlebot.disc import (
    check_wait,
    count_discs,
    group_by_album,
    normalize_for_match,
    parse_xld_log,
)


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


# ── album-aware, file-list-scoped tests (pytest style) ────────────────────────


def _write_flac(path: Path, *, tags: dict | None = None) -> None:
    """Write a minimal valid FLAC with the given Vorbis tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00\x00\x00\x00"
        + struct.pack(">Q", (44100 << 44) | (0 << 41) | (15 << 36) | 0)
        + b"\x00" * 16
    )
    path.write_bytes(b"fLaC" + bytes([0x80, 0x00, 0x00, 0x22]) + streaminfo)
    f = mutagen.flac.FLAC(str(path))
    f.add_tags()
    for k, v in (tags or {}).items():
        f[k] = [str(v)]
    f.save()


def test_check_wait_accepts_explicit_file_list(tmp_path):
    # Two files, one album, disctotal=2 but only disc 1 present → WAIT.
    a = tmp_path / "a.flac"
    b = tmp_path / "b.flac"
    _write_flac(a, tags={"discnumber": 1, "disctotal": 2, "tracknumber": 1})
    _write_flac(b, tags={"discnumber": 1, "disctotal": 2, "tracknumber": 2})
    assert check_wait([a, b]) == "WAIT:1:2"


def test_count_discs_scoped_to_file_list_diverges_from_whole_dir(tmp_path):
    # Target album: incomplete 2-disc set, only disc 1 present.
    # Contaminant: an UNRELATED album whose track happens to carry discnumber=2.
    # Reading the whole directory unions disc numbers → {1, 2} → looks like a
    # complete 2-disc set. Reading only the target album's file sees just disc 1.
    target = tmp_path / "album1-d1.flac"
    contaminant = tmp_path / "album2.flac"
    _write_flac(target, tags={"discnumber": 1, "disctotal": 2})
    _write_flac(contaminant, tags={"discnumber": 2, "disctotal": 1})

    # Per-album scoping gives the correct answer for the target album.
    assert count_discs([target]) == 1
    # The whole-directory read genuinely diverges — it conflates the two albums
    # into a bogus 2-disc count (this is exactly the pre-fix bug).
    assert count_discs(tmp_path) == 2

    # Same divergence on the wait decision: scoped read correctly WAITs (only
    # disc 1 of 2), whole-dir read wrongly sees discs {1,2} and would proceed.
    assert check_wait([target]) == "WAIT:1:2"
    assert check_wait(tmp_path) is None


def test_group_by_album_separates_distinct_albums(tmp_path):
    _write_flac(tmp_path / "rh1.flac", tags={"albumartist": "Radiohead", "album": "Kid A"})
    _write_flac(tmp_path / "rh2.flac", tags={"albumartist": "Radiohead", "album": "Kid A"})
    _write_flac(tmp_path / "dp1.flac", tags={"albumartist": "Daft Punk", "album": "Discovery"})

    groups = group_by_album(tmp_path)
    assert len(groups) == 2
    rh_key = album_key("Radiohead", "Kid A", None)
    dp_key = album_key("Daft Punk", "Discovery", None)
    assert {p.name for p in groups[rh_key]} == {"rh1.flac", "rh2.flac"}
    assert {p.name for p in groups[dp_key]} == {"dp1.flac"}


def test_group_by_album_prefers_mb_albumid(tmp_path):
    # Same mb id, different album text → still one group.
    _write_flac(tmp_path / "1.flac",
                tags={"albumartist": "X", "album": "Deluxe", "musicbrainz_albumid": "mbid-1"})
    _write_flac(tmp_path / "2.flac",
                tags={"albumartist": "X", "album": "Standard", "musicbrainz_albumid": "mbid-1"})
    groups = group_by_album(tmp_path)
    assert len(groups) == 1


def test_group_by_album_untagged_files_stay_together(tmp_path):
    _write_flac(tmp_path / "a.flac")
    _write_flac(tmp_path / "b.flac")
    groups = group_by_album(tmp_path)
    assert len(groups) == 1
    assert sum(len(v) for v in groups.values()) == 2


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestParseXLDLog(unittest.TestCase):
    """The .log is the only per-album completeness signal the pipeline has.

    Parsed from the BODY, never the filename: XLD substitutes lookalike
    characters for "/" and ":" when building filenames, so a real archived log
    is named `Electronic Toys： …` (fullwidth colon) while the tag — and the
    log body — carry a plain ASCII colon.
    """

    HEADER = (
        "X Lossless Decoder version 20250302 (157.2)\n"
        "\n"
        "XLD extraction logfile from 2026-08-09 22:46:09 -0400\n"
        "\n"
        "{identity}\n"
        "\n"
        "Used drive : HL-DT-ST DVDRAM GP75N (revision 1.01)\n"
        "Media type : Pressed CD\n"
    )

    def _log(self, tmp: Path, identity: str, name: str = "rip.log") -> Path:
        path = tmp / name
        path.write_text(self.HEADER.format(identity=identity), encoding="utf-8")
        return path

    def test_parses_artist_and_album(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            log = self._log(tmp, "Pink Floyd / Animals (2018 remix)")
            self.assertEqual(parse_xld_log(log), ("Pink Floyd", "Animals (2018 remix)"))

    def test_album_may_contain_a_colon(self):
        """The settings block is skipped by ' : ', which a title must survive."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            log = self._log(
                tmp, "Various Artists / Electronic Toys: A Retrospective"
            )
            self.assertEqual(
                parse_xld_log(log),
                ("Various Artists", "Electronic Toys: A Retrospective"),
            )

    def test_empty_log_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            log = tmp / "empty.log"
            log.write_text("", encoding="utf-8")
            self.assertIsNone(parse_xld_log(log))

    def test_non_xld_log_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            log = tmp / "junk.log"
            log.write_text("some other tool's output\n", encoding="utf-8")
            self.assertIsNone(parse_xld_log(log))

    def test_missing_file_returns_none(self):
        self.assertIsNone(parse_xld_log(Path("/nonexistent/nope.log")))


class TestNormalizeForMatch(unittest.TestCase):
    def test_casefolds_and_collapses_whitespace(self):
        self.assertEqual(
            normalize_for_match("  Wish  You   Were Here [Remaster] "),
            "wish you were here [remaster]",
        )

    def test_matches_across_case_only_differences(self):
        self.assertEqual(
            normalize_for_match("Animals (2018 Remix)"),
            normalize_for_match("animals (2018 remix)"),
        )
