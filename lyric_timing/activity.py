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

# Frames quieter than this fraction of the track's loud-vocal reference
# (~-26 dB) count as silence. A relative threshold is necessary because a
# Demucs stem is never digitally silent — it carries bleed from the mix, at a
# level that varies per track.
SILENCE_RATIO = 0.05

# Quantile taken as "this is how loud the vocal is when present". The 90th
# rather than the max: a single percussive plosive should not set the scale.
REFERENCE_QUANTILE = 0.9

# Sung-through gaps (breaths, stops between words) are shorter than this;
# only longer silences separate one sung stretch from the next.
MIN_GAP_SECONDS = 0.4

# Below this an "active" stretch is bleed or a stray transient, not singing.
MIN_ACTIVE_SECONDS = 0.3

# Reported intervals start slightly before the detected energy: a lyric line
# should appear as the singer draws breath, not a syllable late.
ONSET_LEAD_SECONDS = 0.15


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def intervals_from_rms(
    rms: Sequence[float], hop_seconds: float
) -> list[tuple[float, float]]:
    """Frame-wise RMS envelope -> merged (start, end) intervals of singing.

    Returns an empty list when nothing crosses the threshold (a stem with no
    vocal at all), which callers should read as "no activity", not "unknown".
    """
    if not rms or hop_seconds <= 0:
        return []
    threshold = _quantile(rms, REFERENCE_QUANTILE) * SILENCE_RATIO
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
