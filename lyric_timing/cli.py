"""CLI for the lyric-timing subsystem: `python -m lyric_timing <command>`.

The only module allowed to print. Heavy backends are imported lazily so
`audit` (and everything test-covered) runs on a bare Python.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lyric_timing.detector import AuditResult, audit_lrc

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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
