"""Deterministic fake backend for tests and CLI smoke runs — no audio access."""

from __future__ import annotations

from pathlib import Path

from lyric_timing.backends.base import Word


class MockBackend:
    """Returns canned words if given, else spreads the transcript's tokens
    evenly across [5%, 95%] of the configured duration."""

    def __init__(self, words: list[Word] | None = None, *, duration: float = 180.0):
        self._words = words
        self._duration = duration

    def word_timestamps(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> list[Word]:
        if self._words is not None:
            return list(self._words)
        from lyric_timing.aligner import tokenize

        tokens = tokenize(transcript)
        if not tokens:
            return []
        start = self._duration * 0.05
        span = self._duration * 0.90
        step = span / len(tokens)
        return [
            Word(
                text=tok,
                start=round(start + i * step, 3),
                end=round(start + i * step + step * 0.9, 3),
                confidence=0.9,
            )
            for i, tok in enumerate(tokens)
        ]
