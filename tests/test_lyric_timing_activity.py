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


def test_sparse_vocals_are_still_detected():
    # under a second of singing in a ten-second stretch: the reference level
    # has to come from the loud frames, or the track's own silence sets it
    intervals = intervals_from_rms(rms((0.5, 9), (0.0, 91)), HOP)
    assert intervals == [(0.0, 0.9)]


def test_sustained_bleed_does_not_become_the_reference():
    # a dense mix leaves the stem humming at 3% of the vocal's level for most
    # of the track. If that run sets the reference, the threshold drops under
    # it and the whole instrumental reads as singing.
    intervals = intervals_from_rms(rms((0.03, 95), (1.0, 5)), HOP)
    assert len(intervals) == 1
    assert intervals[0][0] >= 9.5 - ONSET_LEAD_SECONDS


def test_a_lone_phrase_in_a_long_instrumental_is_detected():
    # under 1% of the track is sung. No quantile over all frames can find this:
    # at the 90th percentile the reference is silence, and at the 99th it is
    # still silence — the cliff just moves. The reference has to come from the
    # loudest frames however few there are.
    intervals = intervals_from_rms(rms((0.5, 9), (0.0, 991)), HOP)
    assert intervals == [(0.0, 0.9)]
