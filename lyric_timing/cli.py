"""CLI for the lyric-timing subsystem: `python -m lyric_timing <command>`.

The only module allowed to print. Heavy backends are imported lazily so
`audit` (and everything test-covered) runs on a bare Python.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from pathlib import Path

from lyric_timing.detector import AuditResult, audit_lrc
from lyric_timing.lrc import Line, format_lrc, parse_lrc

try:
    # dot-less extensions, e.g. {"flac", "mp3", ...}
    from spindlebot.disc import AUDIO_EXTENSIONS
except ImportError:  # running outside the pipeline checkout (e.g. ai-venv)
    AUDIO_EXTENSIONS = frozenset({"flac", "mp3", "m4a", "ogg", "opus", "wav", "aiff"})


def _sibling_audio(lrc_path: Path) -> Path | None:
    for ext in sorted(AUDIO_EXTENSIONS):
        candidate = lrc_path.with_suffix(f".{ext}")
        if candidate.exists():
            return candidate
    return None


def _audio_duration(audio_path: Path) -> float | None:
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(str(audio_path))
    except Exception:
        return None
    if f is not None and f.info is not None and getattr(f.info, "length", None):
        return float(f.info.length)
    return None


def _collect_lrc_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(p.rglob("*.lrc")))
        else:
            out.append(p)
    return out


def _result_dict(r: AuditResult) -> dict:
    return {
        "path": str(r.path),
        "suspicious": r.suspicious,
        "reasons": list(r.reasons),
        "stats": r.stats,
    }


def cmd_audit(args: argparse.Namespace) -> int:
    lrc_paths = _collect_lrc_paths([Path(p).expanduser() for p in args.paths])
    results: list[AuditResult] = []
    for lrc_path in lrc_paths:
        if not lrc_path.exists():
            print(f"error: no such file: {lrc_path}", file=sys.stderr)
            return 2
        audio = _sibling_audio(lrc_path)
        duration = _audio_duration(audio) if audio else None
        results.append(audit_lrc(lrc_path, duration=duration))

    suspicious = [r for r in results if r.suspicious]
    if args.json:
        print(json.dumps([_result_dict(r) for r in results], indent=2))
    else:
        for r in suspicious:
            print(f"{r.path}: {', '.join(r.reasons)}")
        print(f"{len(suspicious)} of {len(results)} suspicious")
    return 0


_METADATA_TAG_RE = re.compile(r"^\[[a-zA-Z]+:.*\]$")


def _lyric_line_texts(lrc_text: str) -> list[str]:
    """Lyric lines in file order: timed lines if present, else plain text
    lines (minus LRC metadata tags like [ar:...])."""
    lines = parse_lrc(lrc_text)
    if lines:
        return [ln.text for ln in lines if ln.text]
    return [
        raw.strip()
        for raw in lrc_text.splitlines()
        if raw.strip() and not _METADATA_TAG_RE.match(raw.strip())
    ]


def _make_backend(args: argparse.Namespace, duration: float | None):
    # Cap torch's unified-memory appetite BEFORE torch is ever imported: at
    # the cap it raises OOM (the backend retries on CPU) rather than pushing
    # the whole machine into swap. setdefault so callers can override.
    os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.5")
    os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.4")
    if args.backend == "mock":
        from lyric_timing.backends.mock import MockBackend

        return MockBackend(duration=duration or 180.0)
    try:
        from lyric_timing.backends.torchaudio_backend import TorchaudioBackend
    except ImportError as exc:
        print(
            f"error: the '{args.backend}' backend needs the AI dependencies "
            f"(torch/torchaudio/demucs): {exc}\n"
            "Install them with ./setup-ai.sh, then run via the AI venv:\n"
            "  ~/.local/share/spindlebot/ai-venv/bin/python -m lyric_timing retime ...",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return TorchaudioBackend(isolate_vocals=not args.no_vocal_sep)


def cmd_retime(args: argparse.Namespace) -> int:
    audio_path = Path(args.audio).expanduser()
    lrc_path = Path(args.lrc).expanduser()
    for p in (audio_path, lrc_path):
        if not p.exists():
            print(f"error: no such file: {p}", file=sys.stderr)
            return 2

    lrc_text = lrc_path.read_text(encoding="utf-8")
    line_texts = _lyric_line_texts(lrc_text)
    if not line_texts:
        print(f"error: no lyric lines found in {lrc_path}", file=sys.stderr)
        return 2

    duration = _audio_duration(audio_path)
    backend = _make_backend(args, duration)

    from lyric_timing.aligner import align

    # stdout carries ONLY our result (JSON or the new .lrc); anything the
    # backend stack prints while aligning (demucs progress etc.) goes to
    # stderr so consumers can parse stdout.
    with contextlib.redirect_stdout(sys.stderr):
        timings = align(
            audio_path, line_texts, backend, language=args.language, duration=duration
        )

    if args.json:
        print(
            json.dumps(
                [
                    {"time": t.time, "text": t.text, "confidence": t.confidence}
                    for t in timings
                ],
                indent=2,
            )
        )
        return 0

    new_lines = [Line(time=t.time, text=t.text) for t in timings]
    # deliberate empty-text markers (e.g. an end-of-lyrics timestamp bounding
    # an instrumental outro) aren't aligned; they keep their manual times
    new_lines += [ln for ln in parse_lrc(lrc_text) if not ln.text]
    new_lrc = format_lrc(new_lines)
    if args.overwrite:
        lrc_path.write_text(new_lrc, encoding="utf-8")
        print(f"wrote {lrc_path}", file=sys.stderr)
    else:
        print(new_lrc, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyric_timing", description="AI lyric-timing tools"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser(
        "audit", help="flag .lrc files whose timing likely needs correction"
    )
    audit.add_argument("paths", nargs="+", help=".lrc files or directories to scan")
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=cmd_audit)

    retime = sub.add_parser(
        "retime", help="re-time an .lrc against its audio via forced alignment"
    )
    retime.add_argument("audio", help="audio file the lyrics belong to")
    retime.add_argument("lrc", help=".lrc file whose text to keep and re-time")
    retime.add_argument(
        "--overwrite", action="store_true", help="write result back to the .lrc"
    )
    retime.add_argument(
        "--json", action="store_true", help="emit [{time,text,confidence}] JSON"
    )
    retime.add_argument(
        "--no-vocal-sep",
        action="store_true",
        help="skip Demucs vocal isolation (faster, less accurate)",
    )
    retime.add_argument(
        "--backend", choices=["torchaudio", "mock"], default="torchaudio"
    )
    retime.add_argument("--language", default=None, help="ISO 639-1 code, e.g. en")
    retime.set_defaults(func=cmd_retime)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
