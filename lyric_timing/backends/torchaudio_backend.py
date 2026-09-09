"""Real alignment backend: Demucs vocal isolation + chunked wav2vec2 CTC
forced alignment via torchaudio.

Replaces the earlier WhisperX approach, which ran ONE wav2vec2 forward pass
over the whole track: self-attention is quadratic in sequence length, so a
4-minute song wanted tens of GB of unified memory and stalled the entire
machine. Here emissions are computed in fixed windows with context padding —
memory is bounded by the window size, not the track — and the CTC alignment
itself is cheap dynamic programming on CPU.

Heavy imports (torch, torchaudio, demucs) happen lazily inside methods.
First run downloads models to ~/.cache (htdemucs ~300 MB, wav2vec2 ~360 MB,
MMS_FA ~1.2 GB if selected).
"""

from __future__ import annotations

import contextlib
import logging
import sys
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from lyric_timing.activity import intervals_from_rms
from lyric_timing.backends.base import BackendResult, Word

log = logging.getLogger(__name__)

# Emission windowing: alignment quality only needs a couple of seconds of
# acoustic context around each frame; 30 s windows keep peak memory trivial.
CHUNK_SECONDS = 30.0
CONTEXT_SECONDS = 2.0

# Vocal-activity envelope: 100 ms is fine enough to catch the gap between two
# sung phrases and coarse enough to ignore syllable-level dips.
ACTIVITY_HOP_SECONDS = 0.1
ACTIVITY_SAMPLE_RATE = 16000

# The star token's emission column, in log-domain: 0.0 means probability 1 at
# every frame, i.e. a free wildcard (this is what torchaudio's own
# `get_model(with_star=True)` appends). Charging the wildcard anything at all
# measurably hurt: at 0.1 and above the CTC blank path is cheaper than a star
# everywhere, so the star stops absorbing unwritten audio and the improvement
# disappears.
#
# A per-frame cost also cannot fix the one thing it looks like it should. When a
# block is sung more times than it is written, which repetition the written
# lines land on is a genuine tie, and the tie is invariant to this constant:
# the unwritten repetition is absorbed by the leading star if the lines go late
# or by a following star if they go early, so the total star-covered audio — and
# hence the cost — is identical either way. Measured across 0.0 to 0.1: the
# placement never moves. Breaking that tie needs an explicit preference for the
# earliest acoustically supported placement, which is not expressible as an
# emission value.
#
# The column deliberately leaves each frame's distribution summing to 2.0 rather
# than 1.0 — the same thing torchaudio's own `get_model(with_star=True)` does.
# Renormalizing afterwards is not a fix: the star is a constant at every frame,
# so logsumexp is a per-frame constant that cancels in the argmax. Measured over
# 336 words: zero frame positions change and every reported confidence is
# exactly halved, which would push correctly-aligned lines below both the
# editor's orange threshold and DEFAULT_MIN_CONFIDENCE for no benefit.
STAR_LOG_PROB = 0.0

# Fraction of words with no mappable characters that means "wrong script"
# rather than "a few numerals" — worth telling the user about.
UNMAPPABLE_WARN_RATIO = 0.2

# wav2vec2_en: English ASR model, uppercase labels with '|' between words,
# returns logits. Clearly the more accurate of the two on English singing
# (benchmarked: 0.81 s vs 2.15 s mean error over an album of hand-timed
# lyrics), so it is the default.
# mms_fa: multilingual forced-alignment model, lowercase romanized vocabulary,
# no word separator, returns log-probabilities. Worse on English, but the
# right choice for lyrics the English model has no orthography for.
MODELS = ("wav2vec2_en", "mms_fa")
DEFAULT_MODEL = "wav2vec2_en"


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


def build_targets(
    transcript: str,
    dictionary: Mapping[str, int],
    *,
    star: int | None = None,
    separator: int | None = None,
) -> tuple[list[int], list[str], list[tuple[int, int] | None]]:
    """Flatten a line-per-row transcript into a CTC target sequence.

    `dictionary` maps lowercased characters to token ids; `separator` is the
    model's word-boundary token, if it has one (MMS_FA does not).

    Returns (targets, words, slices): `words` are the transcript's words in
    order and `slices` the half-open span of `targets` each one owns, or None
    for a word with no mappable characters (numerals, symbols — the aligner
    interpolates those lines).

    When `star` is given, a star token is placed before the first line,
    between every pair of lines, and after the last. A star matches arbitrary
    audio, so audio the written lyrics do not account for — a chorus sung more
    times than it is written, an unwritten intro, ad-libs — has somewhere to
    go other than being threaded through the neighbouring lines' tokens.
    """
    targets: list[int] = []
    words: list[str] = []
    slices: list[tuple[int, int] | None] = []

    def start_token_group() -> None:
        if separator is not None and targets:
            targets.append(separator)

    for line in transcript.splitlines():
        line_words = line.split()
        if not line_words:
            continue
        if star is not None:
            start_token_group()
            targets.append(star)
        for word in line_words:
            tokens = [
                dictionary[c] for c in _fold_ascii(word).lower() if c in dictionary
            ]
            words.append(word)
            if not tokens:
                slices.append(None)
                continue
            start_token_group()
            slices.append((len(targets), len(targets) + len(tokens)))
            targets.extend(tokens)

    if not any(s is not None for s in slices):
        return [], words, slices
    if star is not None:
        start_token_group()
        targets.append(star)
    return targets, words, slices


class TorchaudioBackend:
    """Word-level forced alignment of known lyrics against the audio.

    isolate_vocals: run Demucs (htdemucs) first and align on the vocal stem —
    dramatically more accurate on dense mixes, slower. device: torch device
    string; auto-detected when None, with a CPU retry if the accelerated pass
    fails (MPS op coverage varies by torch version). use_star: allow unwritten
    audio to be absorbed by a wildcard token (see `build_targets`).
    """

    def __init__(
        self,
        *,
        isolate_vocals: bool = True,
        device: str | None = None,
        use_star: bool = True,
        model: str = DEFAULT_MODEL,
    ):
        if model not in MODELS:
            raise ValueError(f"unknown model {model!r}; expected one of {MODELS}")
        self.isolate_vocals = isolate_vocals
        self.device = device
        self.use_star = use_star
        self.model = model
        self._model: tuple | None = None  # (device, model, bundle)

    def align_audio(
        self, audio_path: Path, transcript: str, *, language: str | None = None
    ) -> BackendResult:
        if language not in (None, "en") and self.model == "wav2vec2_en":
            log.warning(
                "language %r: aligning with the English acoustic model after "
                "ASCII folding; the multilingual model (model='mms_fa') is a "
                "better fit for non-English lyrics",
                language,
            )
        device = self.device or _auto_device()

        with tempfile.TemporaryDirectory(prefix="lyric_timing_") as tmp:
            # vocal activity is only meaningful on the isolated stem: on a
            # full mix every instrumental bar would read as "singing"
            activity = None
            if self.isolate_vocals:
                audio_path = self._separate_vocals(audio_path, Path(tmp), device)
                activity = self._vocal_activity(audio_path)
            try:
                words = self._align(audio_path, transcript, device)
            except Exception:
                if device == "cpu":
                    raise
                log.warning("alignment failed on %s; retrying on cpu", device)
                self._model = None
                words = self._align(audio_path, transcript, "cpu")
            return BackendResult(words=words, vocal_activity=activity)

    def _vocal_activity(self, stem_path: Path) -> list[tuple[float, float]]:
        """Frame-wise RMS of the vocal stem -> intervals of actual singing."""
        waveform = self._load_mono(stem_path, ACTIVITY_SAMPLE_RATE)
        hop = int(ACTIVITY_HOP_SECONDS * ACTIVITY_SAMPLE_RATE)
        samples = waveform[0]
        usable = (samples.numel() // hop) * hop
        if usable < hop:
            return []
        frames = samples[:usable].unfold(0, hop, hop)
        rms = frames.pow(2).mean(1).sqrt()
        return intervals_from_rms(rms.tolist(), ACTIVITY_HOP_SECONDS)

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
            if self.model == "mms_fa":
                bundle = torchaudio.pipelines.MMS_FA
                # with_star=False: the bundle's built-in star column is a free
                # wildcard (log-prob 0); we append our own penalised column so
                # the cost is tunable. This model already returns log-probs.
                net = bundle.get_model(with_star=False)
            else:
                bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
                net = bundle.get_model()
            self._model = (device, net.to(device).eval(), bundle)
        return self._model[1], self._model[2]

    def _vocabulary(self, bundle) -> tuple[dict[str, int], int | None, int]:
        """(lowercase char -> token id, word-separator token, star index).

        MMS_FA's labels are lowercase and have no word separator; the English
        ASR model's are uppercase with '|' at index 1. Neither the blank nor
        the separator may appear inside a word's tokens, so a hyphen in the
        lyrics simply drops out.
        """
        if self.model == "mms_fa":
            labels, separator = bundle.get_labels(star=None), None
        else:
            labels, separator = bundle.get_labels(), 1
        reserved = {0, separator}
        dictionary = {c.lower(): i for i, c in enumerate(labels) if i not in reserved}
        return dictionary, separator, len(labels)

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

        # MMS_FA returns log-probabilities; the ASR bundles return logits.
        needs_log_softmax = self.model != "mms_fa"
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
                if needs_log_softmax:
                    emission = torch.log_softmax(emission, dim=-1)
                lead = (start - s) // stride
                keep = min(chunk, n - start) // stride
                parts.append(emission[0, lead : lead + keep].cpu())
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

        dictionary, separator, star_index = self._vocabulary(bundle)
        star = star_index if self.use_star else None
        targets, words, word_slices = build_targets(
            transcript, dictionary, star=star, separator=separator
        )
        if not targets:
            return []
        unmappable = sum(1 for s in word_slices if s is None)
        if unmappable > len(words) * UNMAPPABLE_WARN_RATIO:
            log.warning(
                "%d of %d words have no characters in the alignment vocabulary "
                "(non-roman script?); those lines will be interpolated",
                unmappable,
                len(words),
            )

        if star is not None:
            star_column = torch.full(
                (emission.size(0), 1), STAR_LOG_PROB, dtype=emission.dtype
            )
            emission = torch.cat([emission, star_column], dim=1)

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
