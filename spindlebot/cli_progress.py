"""
Terminal progress rendering — the presentation half of the progress system.

Consumes `ProgressEvent` (from core.progress) and renders a live bar + status
line. Kept out of services/core so the library stays side-effect-free; it writes
only to an injected stream (default stderr, never bare `print`), which also makes
it unit-testable.

- TTY: an in-place two-line region (bar line + current-item line), repainted in
  place via ANSI and throttled to a few paints/sec.
- Non-TTY (pipes, launchd logs): periodic newline-terminated lines with NO
  control characters, so logs stay readable.

Results always go to stdout (the CLI); progress always goes here, to stderr —
so `--json` output stays clean and pipeable.
"""
from __future__ import annotations

import shutil
import sys
import time
from typing import TextIO

from spindlebot.core.progress import ProgressEvent

_BAR_WIDTH = 24
_FILL = "█"    # █
_EMPTY = "░"   # ░
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _truncate_middle(text: str, width: int) -> str:
    if width <= 0 or len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    keep = width - 1  # room for the ellipsis
    head = keep // 2
    return text[:head] + "…" + text[-(keep - head):]


class ProgressReporter:
    """Render ProgressEvents to a stream. Construct, call update() per event,
    call close() when done."""

    def __init__(self, *, stream: TextIO | None = None, label: str = "",
                 enabled: bool = True, isatty: bool | None = None,
                 width: int | None = None, clock=time.monotonic,
                 min_interval: float = 0.1, plain_step: int = 10):
        self.stream = stream if stream is not None else sys.stderr
        self.label = label
        self.enabled = enabled
        self._isatty = self.stream.isatty() if isatty is None else isatty
        self._width = width
        self._clock = clock
        self._min_interval = min_interval
        self._plain_step = plain_step       # non-tty: emit every N percent
        self._start: float | None = None
        self._last_paint = 0.0
        self._last_pct = -1
        self._tty_started = False
        self._spin = 0

    # ── public ──────────────────────────────────────────────────────────────
    def update(self, ev: ProgressEvent) -> None:
        if not self.enabled:
            return
        now = self._clock()
        if self._start is None:
            self._start = now
        final = ev.total > 0 and ev.done >= ev.total
        if self._isatty:
            first = not self._tty_started
            if not (final or first) and (now - self._last_paint) < self._min_interval:
                return
            self._last_paint = now
            self._paint_tty(ev, now)
        else:
            self._paint_plain(ev, final)

    def close(self) -> None:
        """Leave the cursor on a fresh line so following output starts clean."""
        if self.enabled and self._isatty and self._tty_started:
            self.stream.flush()

    # ── rendering ─────────────────────────────────────────────────────────────
    def _term_width(self) -> int:
        return self._width if self._width is not None else shutil.get_terminal_size((80, 24)).columns

    def _eta(self, ev: ProgressEvent, now: float) -> str:
        if ev.total_bytes and ev.done_bytes and self._start is not None:
            elapsed = now - self._start
            if elapsed > 0:
                rate = ev.done_bytes / elapsed
                if rate > 0:
                    return f"  ETA {_fmt_eta((ev.total_bytes - ev.done_bytes) / rate)}"
        return ""

    def _headline(self, ev: ProgressEvent, now: float) -> str:
        prefix = f"{self.label} " if self.label else ""
        if ev.total > 0:
            frac = ev.done / ev.total
            filled = int(frac * _BAR_WIDTH)
            bar = _FILL * filled + _EMPTY * (_BAR_WIDTH - filled)
            byts = (f"  {_fmt_bytes(ev.done_bytes)}/{_fmt_bytes(ev.total_bytes)}"
                    if ev.total_bytes else "")
            return f"{prefix}[{bar}] {int(frac * 100):3d}%  {ev.done}/{ev.total}{byts}{self._eta(ev, now)}"
        ch = _SPINNER[self._spin % len(_SPINNER)]
        self._spin += 1
        return f"{prefix}{ch} {ev.done} processed"

    def _paint_tty(self, ev: ProgressEvent, now: float) -> None:
        # Reserve the last column: writing the full width lands the cursor on the
        # right margin, where most terminals defer the wrap ("pending wrap"),
        # which desyncs the \x1b[2F up-2-lines repaint and strands a frame in the
        # scrollback. Truncating to width-1 keeps us off the margin entirely.
        width = max(1, self._term_width() - 1)
        line1 = _truncate_middle(self._headline(ev, now), width)
        line2 = _truncate_middle(f"· {ev.current}" if ev.current else "", width)
        out = []
        if self._tty_started:
            out.append("\x1b[2F")            # up 2 lines to the region's top
        else:
            self._tty_started = True
        out.append(line1 + "\x1b[K\n")        # clear to end-of-line per row
        out.append(line2 + "\x1b[K\n")
        self.stream.write("".join(out))
        self.stream.flush()

    def _paint_plain(self, ev: ProgressEvent, final: bool) -> None:
        if ev.total <= 0:
            return  # indeterminate: stay quiet in logs rather than spam
        pct = int(ev.done / ev.total * 100)
        crossed = pct // self._plain_step != self._last_pct // self._plain_step
        if not (crossed or final):
            return
        self._last_pct = pct
        prefix = f"{self.label}: " if self.label else ""
        byts = (f"  {_fmt_bytes(ev.done_bytes)}/{_fmt_bytes(ev.total_bytes)}"
                if ev.total_bytes else "")
        self.stream.write(f"{prefix}{pct}% ({ev.done}/{ev.total}){byts}\n")
        self.stream.flush()
