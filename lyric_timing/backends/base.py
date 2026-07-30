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


@dataclass(frozen=True)
class BackendResult:
    """Everything a backend heard in one track.

    vocal_activity is the (start, end) stretches where someone is singing,
    or None when the backend cannot tell — which is different from an empty
    list, meaning "listened, heard no vocal".
    """

    words: list[Word]
    vocal_activity: list[tuple[float, float]] | None = None


class AlignmentBackend(Protocol):
    def align_audio(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> BackendResult:
        """Return word timings for the known transcript, in transcript order.

        Words the backend could not place may be omitted; the aligner
        interpolates around gaps.
        """
        ...
