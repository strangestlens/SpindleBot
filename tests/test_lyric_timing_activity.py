from lyric_timing.activity import (
    MIN_ACTIVE_SECONDS,
    ONSET_LEAD_SECONDS,
    intervals_from_rms,
)

HOP = 0.1


def rms(*runs):
    """runs: (level, frame_count) pairs -> flat RMS envelope."""
    out = []
    for level, count in runs:
        out.extend([level] * count)
    return out


def test_no_frames_is_no_activity():
    assert intervals_from_rms([], HOP) == []


def test_digital_silence_is_no_activity():
    assert intervals_from_rms([0.0] * 50, HOP) == []


def test_continuous_singing_is_one_interval():
    assert intervals_from_rms(rms((0.5, 30)), HOP) == [(0.0, 3.0)]


def test_long_silence_splits_intervals():
    # 1 s sung, 1 s silent, 1 s sung
    intervals = intervals_from_rms(rms((0.5, 10), (0.0, 10), (0.5, 10)), HOP)
    assert len(intervals) == 2
    assert intervals[0] == (0.0, 1.0)  # lead clamped at the start of the track
    assert intervals[1][0] == round(2.0 - ONSET_LEAD_SECONDS, 10)
    assert intervals[1][1] == 3.0


def test_short_gap_is_sung_through():
    # a 0.2 s breath between phrases must not split the interval
    intervals = intervals_from_rms(rms((0.5, 10), (0.0, 2), (0.5, 10)), HOP)
    assert intervals == [(0.0, 2.2)]


def test_brief_transient_is_not_singing():
    # one loud frame in an otherwise quiet stem: bleed, not a line
    intervals = intervals_from_rms(rms((0.0, 20), (0.9, 1), (0.0, 20)), HOP)
    assert intervals == []


def test_bleed_below_the_relative_threshold_is_silence():
    # the quiet run is 2% of the loud run — a stem's instrumental bleed
    intervals = intervals_from_rms(rms((0.02, 20), (1.0, 20)), HOP)
    assert intervals == [(2.0 - ONSET_LEAD_SECONDS, 4.0)]


def test_intervals_are_ordered_and_non_overlapping():
    intervals = intervals_from_rms(
        rms((0.5, 10), (0.0, 10), (0.5, 10), (0.0, 10), (0.5, 10)), HOP
    )
    assert len(intervals) == 3
    assert all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:]))
    assert all(e - s >= MIN_ACTIVE_SECONDS for s, e in intervals)
