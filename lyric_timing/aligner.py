"""Line-level lyric alignment: backend word timings -> per-line timestamps.

This module is the testable heart of the subsystem. Everything here is pure
and offline; the only audio-aware piece is the injected AlignmentBackend.

Pipeline: match backend words to lyric-line tokens (SequenceMatcher, robust
to words the backend dropped or mangled) -> line time = first matched word's
start -> fill unmatched/low-confidence lines by interpolating between
confident anchors -> enforce monotonic non-decreasing times -> clamp to
[0, duration].
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
# confidence keeps its low value so callers can highlight the line).
DEFAULT_MIN_CONFIDENCE = 0.5

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


def interpolate_missing(
    times: Sequence[float | None], duration: float | None = None
) -> list[float]:
    """Fill None entries by linear interpolation between anchored neighbours.

    Head/tail runs extrapolate from the nearest anchor using the anchors'
    average per-line gap. With no anchors at all, lines spread evenly across
    the duration (or FALLBACK_LINE_GAP apart without one).
    """
    anchors = [(i, t) for i, t in enumerate(times) if t is not None]
    n = len(times)
    if not anchors:
        if duration and n:
            return [duration * (i + 1) / (n + 1) for i in range(n)]
        return [i * FALLBACK_LINE_GAP for i in range(n)]

    first_i, first_t = anchors[0]
    last_i, last_t = anchors[-1]
    if last_i > first_i:
        gap = (last_t - first_t) / (last_i - first_i)
    else:
        gap = FALLBACK_LINE_GAP

    out: list[float] = list(range(n))  # placeholder, overwritten below
    for i in range(n):
        t = times[i]
        if t is not None:
            out[i] = t
        elif i < first_i:
            out[i] = max(0.0, first_t - (first_i - i) * gap)
        elif i > last_i:
            out[i] = last_t + (i - last_i) * gap
        else:
            prev = next(a for a in reversed(anchors) if a[0] < i)
            nxt = next(a for a in anchors if a[0] > i)
            frac = (i - prev[0]) / (nxt[0] - prev[0])
            out[i] = prev[1] + frac * (nxt[1] - prev[1])
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
    gets its time by interpolation like any unmatched line.
    """
    alignment_texts = [strip_parentheticals(t) for t in line_texts]
    transcript = "\n".join(alignment_texts)
    words = backend.word_timestamps(audio_path, transcript, language=language)

    assigned = assign_words_to_lines(alignment_texts, words)
    raw_times = [
        t if t is not None and conf >= min_confidence else None
        for t, conf in assigned
    ]
    times = enforce_monotonic(interpolate_missing(raw_times, duration))
    if duration:
        times = [min(t, duration) for t in times]
    times = [max(t, 0.0) for t in times]

    return [
        LineTiming(text=text, time=round(t, 2), confidence=round(conf, 3))
        for text, t, (_, conf) in zip(line_texts, times, assigned)
    ]
