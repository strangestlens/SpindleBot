"""Line-level lyric alignment: backend word timings -> per-line timestamps.

This module is the testable heart of the subsystem. Everything here is pure
and offline; the only audio-aware piece is the injected AlignmentBackend.

Pipeline: match backend words to lyric-line tokens (SequenceMatcher, robust
to words the backend dropped or mangled) -> line time = first matched word's
start -> fill unmatched/low-confidence lines by interpolating between
confident anchors, over sung time when the backend reports where the vocal is
active -> enforce monotonic non-decreasing times -> clamp to [0, duration].
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from statistics import fmean
from typing import Sequence

from lyric_timing.backends.base import AlignmentBackend, Word

# Below this line confidence, a matched time is considered unreliable and is
# replaced by interpolation between confident neighbours (the reported
# confidence keeps its low value so callers can still highlight the line —
# this threshold decides what to *do* with a match, not what to show).
# Benchmarked against two albums of hand-timed lyrics: a weakly matched time
# beats an interpolated one far more often than not, and the old 0.5 threw
# away good matches (mean error 1.75 s -> 1.04 s on one album, 35 s -> 21 s on
# the other). Below ~0.1 the curve flattens and near-evidence-free matches
# start being trusted, so keep a floor.
DEFAULT_MIN_CONFIDENCE = 0.15

# Line spacing used when interpolation has no second anchor to derive a gap
# from (e.g. a single confident line in the whole song).
FALLBACK_LINE_GAP = 3.0

_TOKEN_RE = re.compile(r"[^\W_]+'?[^\W_]*", re.UNICODE)

# Parenthesized fragments in lyrics are backing-vocal echoes/ad-libs
# ("walk away (walk away)") that overlap the lead vocal; forcing the
# alignment path through them distorts neighbouring line times.
_PAREN_RE = re.compile(r"\([^)]*\)")


def strip_parentheticals(text: str) -> str:
    return _PAREN_RE.sub(" ", text)


@dataclass(frozen=True)
class LineTiming:
    text: str
    time: float
    confidence: float


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _norm_word(text: str) -> str:
    return "".join(tokenize(text))


def assign_words_to_lines(
    line_texts: Sequence[str], words: Sequence[Word]
) -> list[tuple[float | None, float]]:
    """Match backend words to lines; return (start_time | None, confidence) per line.

    Matching is positional over the whole song (SequenceMatcher on normalized
    token sequences), so repeated chorus lines resolve to their own occurrence
    rather than all snapping to the first one. Confidence = mean matched-word
    confidence x fraction of the line's tokens that matched.
    """
    flat: list[tuple[int, str]] = []  # (line_idx, token)
    for i, text in enumerate(line_texts):
        flat.extend((i, tok) for tok in tokenize(text))

    word_norms = [_norm_word(w.text) for w in words]
    token_norms = [tok for _, tok in flat]

    matched_words: dict[int, list[Word]] = {}
    matcher = SequenceMatcher(None, word_norms, token_norms, autojunk=False)
    for w_start, t_start, size in matcher.get_matching_blocks():
        for k in range(size):
            line_idx = flat[t_start + k][0]
            matched_words.setdefault(line_idx, []).append(words[w_start + k])

    results: list[tuple[float | None, float]] = []
    for i, text in enumerate(line_texts):
        line_token_count = len(tokenize(text))
        hits = matched_words.get(i)
        if not hits or line_token_count == 0:
            results.append((None, 0.0))
            continue
        coverage = len(hits) / line_token_count
        confidence = fmean(w.confidence for w in hits) * coverage
        results.append((min(w.start for w in hits), confidence))
    return results


Activity = Sequence[tuple[float, float]]


def _sung_clock(activity: Activity | None):
    """Wall-clock <-> sung-time coordinate pair.

    Sung time advances only while the singer is audible, so interpolating in
    it spaces lines by singing rather than by wall clock: a line never lands
    inside an instrumental stretch, and the inverse map places one that would
    have at the next vocal onset. Without activity data both maps are the
    identity and interpolation is plain linear.
    """
    if not activity:
        return (lambda t: t), (lambda x: x)

    def to_sung(t: float) -> float:
        return sum(max(0.0, min(end, t) - start) for start, end in activity)

    def from_sung(x: float) -> float:
        # before the first onset / after the last offset, extend in wall time
        if x < 0:
            return activity[0][0] + x
        elapsed = 0.0
        for start, end in activity:
            span = end - start
            if x < elapsed + span:
                return start + (x - elapsed)
            elapsed += span
        return activity[-1][1] + (x - elapsed)

    return to_sung, from_sung


def interpolate_missing(
    times: Sequence[float | None],
    duration: float | None = None,
    activity: Activity | None = None,
) -> list[float]:
    """Fill None entries by linear interpolation between anchored neighbours.

    Head/tail runs extrapolate from the nearest anchor using the anchors'
    average per-line gap. With no anchors at all, lines spread evenly across
    the duration (or FALLBACK_LINE_GAP apart without one). Interpolation
    happens in sung time when `activity` is known (see `_sung_clock`);
    anchored lines keep their exact times either way.
    """
    to_sung, from_sung = _sung_clock(activity)
    anchors = [(i, to_sung(t)) for i, t in enumerate(times) if t is not None]
    n = len(times)
    if not anchors:
        horizon = to_sung(duration) if duration else None
        if horizon:
            return [max(0.0, from_sung(horizon * (i + 1) / (n + 1))) for i in range(n)]
        if duration and n:
            return [duration * (i + 1) / (n + 1) for i in range(n)]
        return [i * FALLBACK_LINE_GAP for i in range(n)]

    first_i, first_x = anchors[0]
    last_i, last_x = anchors[-1]
    if last_i > first_i:
        gap = (last_x - first_x) / (last_i - first_i)
    else:
        gap = FALLBACK_LINE_GAP

    out: list[float] = list(range(n))  # placeholder, overwritten below
    for i in range(n):
        t = times[i]
        if t is not None:
            out[i] = t
            continue
        if i < first_i:
            x = first_x - (first_i - i) * gap
        elif i > last_i:
            x = last_x + (i - last_i) * gap
        else:
            prev = next(a for a in reversed(anchors) if a[0] < i)
            nxt = next(a for a in anchors if a[0] > i)
            frac = (i - prev[0]) / (nxt[0] - prev[0])
            x = prev[1] + frac * (nxt[1] - prev[1])
        out[i] = max(0.0, from_sung(x))
    return out


def enforce_monotonic(times: Sequence[float]) -> list[float]:
    out: list[float] = []
    for t in times:
        out.append(max(t, out[-1]) if out else t)
    return out


def align(
    audio_path: Path,
    line_texts: Sequence[str],
    backend: AlignmentBackend,
    *,
    language: str | None = None,
    duration: float | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list[LineTiming]:
    """Produce a timestamp + confidence for every lyric line, in line order.

    Alignment runs on the lines with parenthetical ad-libs stripped (the
    output keeps each line's original text); a line that is *only* an ad-lib
    gets its time by interpolation like any unmatched line. When the backend
    reports where the vocal is active, interpolated lines are placed in sung
    time, so they land on singing rather than mid-instrumental.
    """
    alignment_texts = [strip_parentheticals(t) for t in line_texts]
    transcript = "\n".join(alignment_texts)
    heard = backend.align_audio(audio_path, transcript, language=language)

    assigned = assign_words_to_lines(alignment_texts, heard.words)
    raw_times = [
        t if t is not None and conf >= min_confidence else None
        for t, conf in assigned
    ]
    times = enforce_monotonic(
        interpolate_missing(raw_times, duration, heard.vocal_activity)
    )
    if duration:
        times = [min(t, duration) for t in times]
    times = [max(t, 0.0) for t in times]

    return [
        LineTiming(text=text, time=round(t, 2), confidence=round(conf, 3))
        for text, t, (_, conf) in zip(line_texts, times, assigned)
    ]
