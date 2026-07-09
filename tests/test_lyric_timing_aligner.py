from pathlib import Path

from lyric_timing.aligner import (
    LineTiming,
    align,
    assign_words_to_lines,
    enforce_monotonic,
    interpolate_missing,
    strip_parentheticals,
)
from lyric_timing.backends.base import Word
from lyric_timing.backends.mock import MockBackend

AUDIO = Path("/fake/song.flac")


def words_for(*specs):
    """specs: (text, start) tuples; end/conf filled in."""
    return [Word(text=t, start=s, end=s + 0.4, confidence=0.9) for t, s in specs]


# ── assign_words_to_lines ────────────────────────────────────────────────────


def test_lines_get_first_word_start():
    lines = ["Hello world", "Goodbye moon"]
    words = words_for(("Hello", 10.0), ("world", 10.5), ("Goodbye", 20.0), ("moon", 20.5))
    assigned = assign_words_to_lines(lines, words)
    assert [t for t, _ in assigned] == [10.0, 20.0]
    assert all(conf > 0.8 for _, conf in assigned)


def test_matching_survives_dropped_and_mangled_words():
    lines = ["Hello beautiful world", "Goodbye moon"]
    # backend lost "beautiful" and mangled "Goodbye"
    words = words_for(("Hello", 10.0), ("world", 11.0), ("XXXX", 20.0), ("moon", 20.5))
    assigned = assign_words_to_lines(lines, words)
    assert assigned[0][0] == 10.0
    assert assigned[0][1] < 0.9  # partial coverage lowers confidence
    assert assigned[1][0] == 20.5  # matched via "moon" alone


def test_repeated_chorus_lines_resolve_positionally():
    lines = ["Verse one here", "Sing the chorus", "Verse two here", "Sing the chorus"]
    words = words_for(
        ("Verse", 5.0), ("one", 5.5), ("here", 6.0),
        ("Sing", 30.0), ("the", 30.3), ("chorus", 30.6),
        ("Verse", 60.0), ("two", 60.5), ("here", 61.0),
        ("Sing", 90.0), ("the", 90.3), ("chorus", 90.6),
    )
    assigned = assign_words_to_lines(lines, words)
    assert [t for t, _ in assigned] == [5.0, 30.0, 60.0, 90.0]


def test_unmatched_line_gets_none():
    lines = ["Hello world", "Totally absent line"]
    words = words_for(("Hello", 10.0), ("world", 10.5))
    assigned = assign_words_to_lines(lines, words)
    assert assigned[1] == (None, 0.0)


def test_case_and_punctuation_insensitive():
    lines = ["Don't stop, believin'!"]
    words = words_for(("don't", 12.0), ("STOP", 12.5), ("believin'", 13.0))
    assigned = assign_words_to_lines(lines, words)
    assert assigned[0][0] == 12.0


# ── interpolate_missing / enforce_monotonic ──────────────────────────────────


def test_interpolate_between_anchors():
    assert interpolate_missing([10.0, None, None, 40.0]) == [10.0, 20.0, 30.0, 40.0]


def test_extrapolate_head_and_tail_from_anchor_gap():
    # anchors at idx 1 (t=20) and idx 3 (t=40): per-line gap 10
    times = interpolate_missing([None, 20.0, None, 40.0, None])
    assert times == [10.0, 20.0, 30.0, 40.0, 50.0]


def test_head_extrapolation_clamps_at_zero():
    assert interpolate_missing([None, 2.0, 12.0])[0] == 0.0


def test_no_anchors_spreads_across_duration():
    assert interpolate_missing([None, None, None], duration=120.0) == [30.0, 60.0, 90.0]


def test_enforce_monotonic():
    assert enforce_monotonic([10.0, 5.0, 20.0, 15.0]) == [10.0, 10.0, 20.0, 20.0]


# ── align (end to end with mock) ─────────────────────────────────────────────


def test_align_end_to_end_with_canned_words():
    lines = ["Hello world", "Missing middle line", "Goodbye moon"]
    backend = MockBackend(
        words_for(("Hello", 10.0), ("world", 10.5), ("Goodbye", 50.0), ("moon", 50.5))
    )
    timings = align(AUDIO, lines, backend, duration=60.0)
    assert [t.text for t in timings] == lines
    assert timings[0].time == 10.0
    assert timings[2].time == 50.0
    assert 10.0 < timings[1].time < 50.0  # interpolated
    assert timings[1].confidence == 0.0
    assert isinstance(timings[0], LineTiming)


def test_align_low_confidence_time_is_interpolated_but_conf_reported():
    lines = ["Hello there world friend", "Second line okay", "Third line okay"]
    # line 0 matches only 1 of 4 tokens (conf 0.9 * 0.25 = 0.225 < 0.5) at a
    # bogus late time; lines 1-2 are solid anchors
    backend = MockBackend(
        words_for(
            ("hello", 55.0),
            ("second", 20.0), ("line", 20.4), ("okay", 20.8),
            ("third", 40.0), ("line", 40.4), ("okay", 40.8),
        )
    )
    timings = align(AUDIO, lines, backend, duration=60.0)
    assert timings[0].time < 20.0  # extrapolated before first anchor, not 55.0
    assert 0.0 < timings[0].confidence < 0.5
    assert timings[1].time == 20.0
    assert timings[2].time == 40.0


def test_align_output_is_monotonic_and_clamped():
    lines = ["Line a here", "Line b here", "Line c here"]
    backend = MockBackend(
        words_for(
            ("line", 50.0), ("a", 50.2), ("here", 50.4),
            ("line", 10.0), ("b", 10.2), ("here", 10.4),
            ("line", 99.0), ("c", 99.2), ("here", 99.4),
        )
    )
    timings = align(AUDIO, lines, backend, duration=60.0)
    times = [t.time for t in timings]
    assert times == sorted(times)
    assert all(0.0 <= t <= 60.0 for t in times)


def test_strip_parentheticals():
    assert strip_parentheticals("Walk away (walk away), stay (stay)").split() == [
        "Walk", "away", ",", "stay",
    ]
    assert strip_parentheticals("(Ooh)").strip() == ""


def test_align_ignores_backing_vocal_echoes():
    # the echo "(lips sealed)" is not in the alignment transcript, so full
    # coverage of the lead-vocal words gives high confidence
    lines = ["Keep your lips sealed (lips sealed)", "Walk away now"]
    backend = MockBackend(
        words_for(
            ("Keep", 10.0), ("your", 10.3), ("lips", 10.6), ("sealed", 11.0),
            ("Walk", 20.0), ("away", 20.4), ("now", 20.8),
        )
    )
    timings = align(AUDIO, lines, backend, duration=60.0)
    assert timings[0].time == 10.0
    assert timings[0].confidence > 0.8
    assert timings[0].text == "Keep your lips sealed (lips sealed)"  # text kept
    assert timings[1].time == 20.0


def test_align_pure_adlib_line_is_interpolated():
    lines = ["First line here", "(Ooh la la)", "Third line here"]
    backend = MockBackend(
        words_for(
            ("First", 10.0), ("line", 10.3), ("here", 10.6),
            ("Third", 30.0), ("line", 30.3), ("here", 30.6),
        )
    )
    timings = align(AUDIO, lines, backend, duration=60.0)
    assert 10.0 < timings[1].time < 30.0
    assert timings[1].confidence == 0.0


def test_align_with_generated_mock_words_is_ordered():
    lines = ["First line of song", "Second line of song", "Third line of song"]
    timings = align(AUDIO, lines, MockBackend(duration=180.0), duration=180.0)
    times = [t.time for t in timings]
    assert times == sorted(times)
    assert times[0] >= 0.0 and times[-1] <= 180.0
    assert all(t.confidence > 0.5 for t in timings)
