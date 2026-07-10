"""Audit .lrc files for timing that likely needs AI re-timing.

Pure text heuristics — no audio access. Callers that know the track duration
(from mutagen or the beets DB) pass it in; the crammed-early check is skipped
without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from lyric_timing.lrc import parse_lrc

# Distinct-timestamp ratio below which a file looks bulk-stamped rather than
# individually timed (only applied at MIN_LINES_FOR_RATIO+ lines so tiny files
# don't false-positive).
DISTINCT_RATIO_THRESHOLD = 0.3
MIN_LINES_FOR_RATIO = 4

# If the last timestamp lands before this fraction of the track, every lyric
# is crammed into the front of the song.
EARLY_CRAM_FRACTION = 0.5

# The crammed-early check only fires when lines are also bulk-stamped tightly
# together (median gap between consecutive timestamps at or below this).
# Well-spread timing that simply ends early is deliberate — a hand-timed song
# with a long instrumental tail, or a lone [Instrumental] marker.
MAX_BULK_GAP = 2.0

ALL_TIMESTAMPS_IDENTICAL = "all-timestamps-identical"
LOW_DISTINCT_TIMESTAMPS = "low-distinct-timestamps"
TIMESTAMPS_CRAMMED_EARLY = "timestamps-crammed-early"
NON_MONOTONIC = "non-monotonic"
NO_TIMED_LINES = "no-timed-lines"


@dataclass(frozen=True)
class AuditResult:
    suspicious: bool
    reasons: tuple[str, ...]
    stats: dict = field(default_factory=dict)
    path: Path | None = None


def audit_lrc_text(
    text: str, *, duration: float | None = None, path: Path | None = None
) -> AuditResult:
    lines = parse_lrc(text)
    times = [ln.time for ln in lines]
    distinct = sorted(set(times))
    reasons: list[str] = []

    if not lines:
        # A file with lyric content but zero parseable timestamps still needs
        # timing; a genuinely empty file is just empty, not suspicious.
        if text.strip():
            reasons.append(NO_TIMED_LINES)
    else:
        if len(lines) >= 2 and len(distinct) == 1:
            reasons.append(ALL_TIMESTAMPS_IDENTICAL)
        elif (
            len(lines) >= MIN_LINES_FOR_RATIO
            and len(distinct) / len(lines) < DISTINCT_RATIO_THRESHOLD
        ):
            reasons.append(LOW_DISTINCT_TIMESTAMPS)

        if duration and duration > 0 and len(lines) >= 2:
            # gaps in time order, not file order: a non-monotonic file's
            # negative gaps would drag the median down and mislabel spread
            # timing as bulk-stamped
            ordered = sorted(times)
            if (
                max(times) < duration * EARLY_CRAM_FRACTION
                and median(b - a for a, b in zip(ordered, ordered[1:]))
                <= MAX_BULK_GAP
            ):
                reasons.append(TIMESTAMPS_CRAMMED_EARLY)

        if any(b < a for a, b in zip(times, times[1:])):
            reasons.append(NON_MONOTONIC)

    stats = {
        "line_count": len(lines),
        "distinct_times": len(distinct),
        "max_time": max(times) if times else None,
        "duration": duration,
    }
    return AuditResult(
        suspicious=bool(reasons), reasons=tuple(reasons), stats=stats, path=path
    )


def audit_lrc(path: Path, *, duration: float | None = None) -> AuditResult:
    return audit_lrc_text(
        path.read_text(encoding="utf-8"), duration=duration, path=path
    )
