"""Tests for the terminal progress reporter (spindlebot.cli_progress)."""
from __future__ import annotations

import io

import pytest

from spindlebot.cli_progress import ProgressReporter, _truncate_middle
from spindlebot.core.progress import ProgressEvent


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _ev(done, total, **kw):
    return ProgressEvent(phase=kw.pop("phase", "audio"), done=done, total=total, **kw)


# ── helpers ───────────────────────────────────────────────────────────────────

def test_truncate_middle():
    assert _truncate_middle("short", 80) == "short"
    out = _truncate_middle("a-very-long-filename-that-overflows.flac", 20)
    assert len(out) == 20 and "…" in out
    assert out.startswith("a-very") and out.endswith(".flac")


# ── non-TTY (logs) ────────────────────────────────────────────────────────────

def test_plain_mode_has_no_control_chars_and_steps(tmp_path):
    buf = io.StringIO()
    r = ProgressReporter(stream=buf, label="inventory DwRugged",
                         isatty=False, plain_step=25)
    for done in range(0, 11):                       # 0..10 of 10  → 0,10,...,100%
        r.update(_ev(done, 10, done_bytes=done, total_bytes=10))
    r.close()
    out = buf.getvalue()
    assert "\x1b" not in out and "\r" not in out     # logs stay clean
    assert "inventory DwRugged" in out
    # only step crossings (25%) + final emit, not every single event
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) <= 6
    assert lines[-1].startswith("inventory DwRugged: 100%")


def test_plain_indeterminate_stays_quiet(tmp_path):
    buf = io.StringIO()
    r = ProgressReporter(stream=buf, isatty=False)
    for done in range(5):
        r.update(_ev(done, 0))                        # total=0 → indeterminate
    assert buf.getvalue() == ""


# ── TTY (interactive) ─────────────────────────────────────────────────────────

def test_tty_first_paint_and_throttle():
    buf = io.StringIO()
    clock = FakeClock()
    r = ProgressReporter(stream=buf, isatty=True, width=80, clock=clock,
                         min_interval=0.5)
    r.update(_ev(1, 10))            # first paint always happens
    assert buf.getvalue().count("\x1b[K") == 2      # two cleared lines
    before = buf.getvalue()
    r.update(_ev(2, 10))            # same clock → throttled, no new paint
    assert buf.getvalue() == before
    clock.t = 1.0
    r.update(_ev(3, 10))            # interval elapsed → repaints
    assert "\x1b[2F" in buf.getvalue()              # moved up to repaint in place


def test_tty_final_always_paints_even_within_throttle():
    buf = io.StringIO()
    clock = FakeClock()
    r = ProgressReporter(stream=buf, isatty=True, width=80, clock=clock,
                         min_interval=999)
    r.update(_ev(1, 2))
    r.update(_ev(2, 2))            # final, must paint despite huge interval
    # two paints → the second includes the cursor-up repaint
    assert buf.getvalue().count("\x1b[2F") == 1
    assert "100%" in buf.getvalue()


def test_tty_renders_bar_bytes_eta_and_current():
    buf = io.StringIO()
    clock = FakeClock()
    r = ProgressReporter(stream=buf, label="inventory", isatty=True, width=120,
                         clock=clock)
    r.update(_ev(1, 10, done_bytes=100, total_bytes=1000))   # t=0 → start clock
    clock.t = 10.0                 # 10s elapsed, 500/1000 bytes → rate 50/s, ETA 10s
    r.update(_ev(5, 10, done_bytes=500, total_bytes=1000,
                 current="Artist/Album/05 Song.flac"))
    out = buf.getvalue()
    assert "[" in out and ("█" in out or "░" in out)
    assert "50%" in out
    assert "ETA" in out
    assert "Artist/Album/05 Song.flac" in out


def test_tty_truncates_within_width_reserving_the_margin():
    buf = io.StringIO()
    r = ProgressReporter(stream=buf, isatty=True, width=30,
                         clock=FakeClock())
    r.update(_ev(1, 10, current="some/really/long/path/that/cannot/fit.flac"))
    for line in buf.getvalue().split("\n"):
        visible = line.replace("\x1b[2F", "").replace("\x1b[K", "")
        # stays strictly inside the terminal — never writes the last column
        assert len(visible) <= 29


def test_indeterminate_tty_shows_spinner():
    buf = io.StringIO()
    r = ProgressReporter(stream=buf, isatty=True, width=80, clock=FakeClock())
    r.update(_ev(7, 0, phase="reconcile"))
    assert "7 processed" in buf.getvalue()


def test_disabled_reporter_writes_nothing():
    buf = io.StringIO()
    r = ProgressReporter(stream=buf, isatty=True, enabled=False)
    r.update(_ev(1, 10))
    r.close()
    assert buf.getvalue() == ""
