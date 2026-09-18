"""Vocal-activity detection: which stretches of a track are actually sung.

Forced alignment only places lines it can hear; lines it cannot (an ad-lib
line, a word the model missed) get interpolated, and plain linear
interpolation happily parks them in the middle of an eight-bar instrumental.
Knowing where singing happens lets the aligner interpolate over *sung* time
instead of wall-clock time.

The detection input is a frame-wise RMS envelope of the isolated vocal stem —
computing that needs torch, so it lives in the backend; every decision made
from it lives here, pure and unit-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

# Frames quieter than this fraction of the track's loud-vocal reference
# (~-26 dB) count as silence. A relative threshold is necessary because a
# Demucs stem is never digitally silent — it carries bleed from the mix, at a
# level that varies per track.
SILENCE_RATIO = 0.05

# The reference level — "how loud is this track when someone is singing" — is
# the median of the loudest frames, taking as many frames as the shortest
# stretch that would count as singing at all (MIN_ACTIVE_SECONDS).
#
# A quantile over every frame cannot do this job at any percentile. The share of
# a track that is sung ranges from nearly all of it to under one percent, so
# wherever the quantile sits, a sparser track reads its own silence as its
# reference and reports no vocal at all; raising the percentile only moves the
# cliff. Selecting the loudest frames has no cliff, and taking their median
# rather than their maximum keeps a lone percussive transient from setting the
# scale.

# Sung-through gaps (breaths, stops between words) are shorter than this;
# only longer silences separate one sung stretch from the next.
MIN_GAP_SECONDS = 0.4

# Below this an "active" stretch is bleed or a stray transient, not singing.
MIN_ACTIVE_SECONDS = 0.3

# Reported intervals start slightly before the detected energy: a lyric line
# should appear as the singer draws breath, not a syllable late.
ONSET_LEAD_SECONDS = 0.15


def intervals_from_rms(
    rms: Sequence[float], hop_seconds: float
) -> list[tuple[float, float]]:
    """Frame-wise RMS envelope -> merged (start, end) intervals of singing.

    Returns an empty list when nothing crosses the threshold (a stem with no
    vocal at all), which callers should read as "no activity", not "unknown".
    """
    if not rms or hop_seconds <= 0:
        return []
    window = max(1, round(MIN_ACTIVE_SECONDS / hop_seconds))
    threshold = median(sorted(rms, reverse=True)[:window]) * SILENCE_RATIO
    if threshold <= 0:
        return []

    intervals: list[tuple[float, float]] = []
    for i, value in enumerate(rms):
        if value < threshold:
            continue
        start, end = i * hop_seconds, (i + 1) * hop_seconds
        if intervals and start - intervals[-1][1] < MIN_GAP_SECONDS:
            intervals[-1] = (intervals[-1][0], end)
        else:
            intervals.append((start, end))

    return [
        (max(0.0, s - ONSET_LEAD_SECONDS), e)
        for s, e in intervals
        if e - s >= MIN_ACTIVE_SECONDS
    ]
