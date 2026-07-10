"""Alignment backend contract.

A backend takes audio plus the known lyric text and returns word-level
timestamps. The heavy implementation (Demucs + torchaudio forced alignment)
is swappable and mockable behind this Protocol; all timing intelligence above
the word level (line aggregation, interpolation, monotonicity) lives in
aligner.py and is tested with the mock.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    confidence: float = 1.0


class AlignmentBackend(Protocol):
    def word_timestamps(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> list[Word]:
        """Return word timings for the known transcript, in transcript order.

        Words the backend could not place may be omitted; the aligner
        interpolates around gaps.
        """
        ...
