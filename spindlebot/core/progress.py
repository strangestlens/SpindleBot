"""
Progress events for long-running services — pure data, no I/O.

A service emits `ProgressEvent`s through an optional callback (the same shape as
the `echo` callback in `pipeline/runner.py`); the CLI turns them into a terminal
bar/line. Core stays side-effect-free — it never touches the terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProgressEvent:
    phase: str             # e.g. "scan" | "audio" | "sidecar" | "reconcile"
    done: int              # items completed so far
    total: int             # total items, or 0 when unknown (indeterminate)
    done_bytes: int = 0    # bytes processed so far (drives ETA)
    total_bytes: int = 0   # total bytes, or 0 when unknown
    current: str = ""      # label for the in-flight item (e.g. a rel path)


ProgressCallback = Callable[[ProgressEvent], None]


def emit(progress: ProgressCallback | None, **fields) -> None:
    """Fire a ProgressEvent if a callback is set. Progress is cosmetic — a
    misbehaving callback must never break the work, so exceptions are swallowed."""
    if progress is None:
        return
    try:
        progress(ProgressEvent(**fields))
    except Exception:
        pass
