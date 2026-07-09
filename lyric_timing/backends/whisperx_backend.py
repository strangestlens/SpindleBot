"""Real alignment backend: Demucs vocal isolation + WhisperX forced alignment.

Heavy imports (torch, demucs, whisperx) happen lazily inside methods so this
module can be imported for construction/errors without the AI venv. Because we
already know the lyrics, this runs pure forced alignment (wav2vec2 CTC over
the whole track) — no ASR pass, no ctranslate2.

First run downloads models to ~/.cache (htdemucs ~300 MB, wav2vec2 per
language ~360 MB). Runtime is roughly 30 s–3 min per song; MPS strongly
recommended.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from lyric_timing.backends.base import Word

log = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "en"


def _auto_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class WhisperXBackend:
    """Word-level forced alignment of known lyrics against the audio.

    isolate_vocals: run Demucs (htdemucs) first and align on the vocal stem —
    dramatically more accurate on dense mixes, slower. device: torch device
    string; auto-detected when None, with a CPU retry if the accelerated pass
    fails (MPS op coverage varies by torch version).
    """

    def __init__(self, *, isolate_vocals: bool = True, device: str | None = None):
        self.isolate_vocals = isolate_vocals
        self.device = device
        self._align_models: dict[str, tuple] = {}  # language -> (model, metadata)

    def word_timestamps(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> list[Word]:
        language = language or DEFAULT_LANGUAGE
        device = self.device or _auto_device()

        with tempfile.TemporaryDirectory(prefix="lyric_timing_") as tmp:
            if self.isolate_vocals:
                audio_path = self._separate_vocals(audio_path, Path(tmp), device)
            try:
                return self._align(audio_path, transcript, language, device)
            except Exception:
                if device == "cpu":
                    raise
                log.warning("alignment failed on %s; retrying on cpu", device)
                self._align_models.clear()
                return self._align(audio_path, transcript, language, "cpu")

    def _separate_vocals(self, audio_path: Path, tmp: Path, device: str) -> Path:
        # demucs 4.0.1 (latest on PyPI) has no demucs.api module — that only
        # exists in unreleased 4.1 alphas — so drive the CLI entry point.
        from demucs.separate import main as demucs_main

        def run(dev: str) -> None:
            demucs_main(
                ["--two-stems", "vocals", "-n", "htdemucs", "-d", dev,
                 "-o", str(tmp), str(audio_path)]
            )

        try:
            run(device)
        except Exception:
            if device == "cpu":
                raise
            log.warning("demucs failed on %s; retrying on cpu", device)
            run("cpu")

        vocals_path = tmp / "htdemucs" / audio_path.stem / "vocals.wav"
        if not vocals_path.exists():
            raise RuntimeError(f"demucs did not produce {vocals_path}")
        return vocals_path

    def _align(
        self, audio_path: Path, transcript: str, language: str, device: str
    ) -> list[Word]:
        import whisperx

        audio = whisperx.load_audio(str(audio_path))
        duration = len(audio) / 16000.0

        if language not in self._align_models:
            self._align_models[language] = whisperx.load_align_model(
                language_code=language, device=device
            )
        model, metadata = self._align_models[language]

        # Forced alignment: one segment spanning the whole track carrying the
        # full known lyric text; wav2vec2 CTC places every word within it.
        segments = [{"text": transcript.replace("\n", " "), "start": 0.0, "end": duration}]
        result = whisperx.align(
            segments, model, metadata, audio, device, return_char_alignments=False
        )

        words: list[Word] = []
        for w in result.get("word_segments", []):
            if w.get("start") is None or w.get("end") is None:
                continue  # unplaceable word; the aligner interpolates around it
            words.append(
                Word(
                    text=w["word"],
                    start=float(w["start"]),
                    end=float(w["end"]),
                    confidence=float(w.get("score", 1.0)),
                )
            )
        return words
