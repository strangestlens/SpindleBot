"""Real alignment backend: Demucs vocal isolation + chunked wav2vec2 CTC
forced alignment via torchaudio.

Replaces the earlier WhisperX approach, which ran ONE wav2vec2 forward pass
over the whole track: self-attention is quadratic in sequence length, so a
4-minute song wanted tens of GB of unified memory and stalled the entire
machine. Here emissions are computed in fixed windows with context padding —
memory is bounded by the window size, not the track — and the CTC alignment
itself is cheap dynamic programming on CPU.

Heavy imports (torch, torchaudio, demucs) happen lazily inside methods.
First run downloads models to ~/.cache (htdemucs ~300 MB, wav2vec2 ~360 MB).
"""

from __future__ import annotations

import contextlib
import logging
import sys
import tempfile
import unicodedata
from pathlib import Path

from lyric_timing.backends.base import Word

log = logging.getLogger(__name__)

# Emission windowing: alignment quality only needs a couple of seconds of
# acoustic context around each frame; 30 s windows keep peak memory trivial.
CHUNK_SECONDS = 30.0
CONTEXT_SECONDS = 2.0


def _auto_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _fold_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


class TorchaudioBackend:
    """Word-level forced alignment of known lyrics against the audio.

    isolate_vocals: run Demucs (htdemucs) first and align on the vocal stem —
    dramatically more accurate on dense mixes, slower. device: torch device
    string; auto-detected when None, with a CPU retry if the accelerated pass
    fails (MPS op coverage varies by torch version).
    """

    def __init__(self, *, isolate_vocals: bool = True, device: str | None = None):
        self.isolate_vocals = isolate_vocals
        self.device = device
        self._model: tuple | None = None  # (device, model, bundle)

    def word_timestamps(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> list[Word]:
        if language not in (None, "en"):
            log.warning(
                "language %r: aligning with the English acoustic model after "
                "ASCII folding; expect reduced accuracy",
                language,
            )
        device = self.device or _auto_device()

        with tempfile.TemporaryDirectory(prefix="lyric_timing_") as tmp:
            if self.isolate_vocals:
                audio_path = self._separate_vocals(audio_path, Path(tmp), device)
            try:
                return self._align(audio_path, transcript, device)
            except Exception:
                if device == "cpu":
                    raise
                log.warning("alignment failed on %s; retrying on cpu", device)
                self._model = None
                return self._align(audio_path, transcript, "cpu")

    def _separate_vocals(self, audio_path: Path, tmp: Path, device: str) -> Path:
        # demucs 4.0.1 (latest on PyPI) has no demucs.api module — that only
        # exists in unreleased 4.1 alphas — so drive the CLI entry point.
        from demucs.separate import main as demucs_main

        def run(dev: str) -> None:
            # demucs prints progress to stdout; route it to stderr so callers
            # (e.g. `retime --json`) keep a parseable stdout
            with contextlib.redirect_stdout(sys.stderr):
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

    def _get_model(self, device: str):
        import torchaudio

        if self._model is None or self._model[0] != device:
            bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
            model = bundle.get_model().to(device).eval()
            self._model = (device, model, bundle)
        return self._model[1], self._model[2]

    def _load_mono(self, audio_path: Path, sample_rate: int):
        import torchaudio

        waveform, in_rate = torchaudio.load(str(audio_path))
        waveform = waveform.mean(0, keepdim=True)
        if in_rate != sample_rate:
            waveform = torchaudio.functional.resample(waveform, in_rate, sample_rate)
        return waveform

    def _chunked_emissions(self, model, waveform, device: str, sample_rate: int):
        """Log-prob emissions for the whole track, computed window by window.

        Each window carries CONTEXT_SECONDS of padding on both sides; only the
        central frames are kept, so every retained frame has full acoustic
        context while peak memory stays bounded by the window size.
        """
        import torch

        chunk = int(CHUNK_SECONDS * sample_rate)
        ctx = int(CONTEXT_SECONDS * sample_rate)
        stride = 320  # wav2vec2 frame hop at 16 kHz (20 ms)
        n = waveform.size(1)

        parts = []
        with torch.inference_mode():
            for start in range(0, n, chunk):
                s = max(0, start - ctx)
                e = min(n, start + chunk + ctx)
                emission, _ = model(waveform[:, s:e].to(device))
                emission = torch.log_softmax(emission[0], dim=-1).cpu()
                lead = (start - s) // stride
                keep = min(chunk, n - start) // stride
                parts.append(emission[lead : lead + keep])
                if device == "mps":
                    torch.mps.empty_cache()
        return torch.cat(parts)

    def _align(self, audio_path: Path, transcript: str, device: str) -> list[Word]:
        import torch
        from torchaudio.functional import forced_align, merge_tokens

        model, bundle = self._get_model(device)
        sample_rate = bundle.sample_rate
        waveform = self._load_mono(audio_path, sample_rate)
        if waveform.size(1) < sample_rate:  # sub-second audio: nothing to align
            return []

        emission = self._chunked_emissions(model, waveform, device, sample_rate)

        labels = bundle.get_labels()  # ('-', '|', 'E', 'T', ...): blank 0, word sep 1
        separator = 1
        # neither the blank ('-') nor the separator ('|') may appear inside a
        # word's tokens — hyphens etc. in lyrics simply drop out
        dictionary = {c: i for i, c in enumerate(labels) if i > separator}

        # Map each transcript word to label tokens; words with no mappable
        # characters (numbers, symbols) are omitted — the aligner interpolates.
        words = transcript.split()
        targets: list[int] = []
        word_slices: list[tuple[int, int] | None] = []
        for word in words:
            tokens = [
                dictionary[c]
                for c in _fold_ascii(word).upper()
                if c in dictionary
            ]
            if not tokens:
                word_slices.append(None)
                continue
            if targets:
                targets.append(separator)
            word_slices.append((len(targets), len(targets) + len(tokens)))
            targets.extend(tokens)
        if not targets:
            return []

        alignment, scores = forced_align(
            emission.unsqueeze(0),
            torch.tensor([targets], dtype=torch.int32),
            blank=0,
        )
        # one span per target token, in target order
        spans = merge_tokens(alignment[0], scores[0].exp(), blank=0)
        seconds_per_frame = waveform.size(1) / emission.size(0) / sample_rate

        out: list[Word] = []
        for word, sl in zip(words, word_slices):
            if sl is None:
                continue
            word_spans = spans[sl[0] : sl[1]]
            out.append(
                Word(
                    text=word,
                    start=word_spans[0].start * seconds_per_frame,
                    end=word_spans[-1].end * seconds_per_frame,
                    confidence=float(
                        sum(s.score for s in word_spans) / len(word_spans)
                    ),
                )
            )
        return out
