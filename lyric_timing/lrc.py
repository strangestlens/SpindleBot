"""LRC parsing/formatting, mirroring lrc-editor's format exactly.

The output format must stay byte-compatible with the lrc-editor script's
``format_lrc`` (``[MM:SS.ss]text``, sorted by time, trailing newline) so
retimed results drop straight into the editor and its drafts.

Unlike the editor, ``parse_lrc`` preserves *file order* instead of sorting by
time: the detector needs to see non-monotonic timestamps, and the aligner
needs the lyric lines in textual order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_LRC_RE = re.compile(r"\[(\d+):(\d+\.\d+)\](.*)")


@dataclass(frozen=True)
class Line:
    time: float
    text: str


def parse_lrc(text: str) -> list[Line]:
    """Parse LRC text into lines, preserving file order.

    Lines that don't carry a ``[mm:ss.xx]`` timestamp (metadata tags, blanks,
    plain text) are skipped, matching the editor's behaviour.
    """
    lines: list[Line] = []
    for raw in text.splitlines():
        m = _LRC_RE.match(raw.strip())
        if m:
            mins, secs, line_text = m.groups()
            t = int(mins) * 60 + float(secs)
            lines.append(Line(time=round(t, 2), text=line_text.strip()))
    return lines


def parse_lrc_file(path: Path) -> list[Line]:
    return parse_lrc(path.read_text(encoding="utf-8"))


def format_lrc(lines: list[Line]) -> str:
    """Format lines as LRC — byte-identical to lrc-editor's format_lrc."""
    out = []
    for ln in sorted(lines, key=lambda x: x.time):
        mins = int(ln.time) // 60
        secs = ln.time - mins * 60
        out.append(f"[{mins:02d}:{secs:05.2f}]{ln.text}")
    return "\n".join(out) + "\n"
