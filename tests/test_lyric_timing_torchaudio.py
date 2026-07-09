"""Integration test for the real torchaudio backend — never runs in CI.

Run manually from the AI venv against a real track:

    LYRIC_TIMING_IT_AUDIO=/path/to/song.flac \
    LYRIC_TIMING_IT_LRC=/path/to/song.lrc \
    ~/.local/share/spindlebot/ai-venv/bin/python -m pytest \
        tests/test_lyric_timing_torchaudio.py -v -s
"""

import os
from pathlib import Path

import pytest

AUDIO = os.environ.get("LYRIC_TIMING_IT_AUDIO")
LRC = os.environ.get("LYRIC_TIMING_IT_LRC")

pytestmark = pytest.mark.skipif(
    not (AUDIO and LRC),
    reason="set LYRIC_TIMING_IT_AUDIO and LYRIC_TIMING_IT_LRC to run",
)


def test_retime_real_track():
    from lyric_timing.aligner import align
    from lyric_timing.backends.torchaudio_backend import TorchaudioBackend
    from lyric_timing.cli import _audio_duration, _lyric_line_texts

    audio_path = Path(AUDIO)
    line_texts = _lyric_line_texts(Path(LRC).read_text(encoding="utf-8"))
    assert line_texts, "lrc fixture has no lyric lines"

    duration = _audio_duration(audio_path)
    timings = align(
        audio_path, line_texts, TorchaudioBackend(), duration=duration
    )

    times = [t.time for t in timings]
    assert len(timings) == len(line_texts)
    assert times == sorted(times)
    assert times[-1] > times[0], "timestamps did not spread out"
    if duration:
        assert all(0.0 <= t <= duration for t in times)

    confident = [t for t in timings if t.confidence >= 0.5]
    assert len(confident) >= len(timings) * 0.5, "less than half the lines confident"

    for t in timings:
        print(f"{t.time:8.2f}  conf={t.confidence:.2f}  {t.text}")
