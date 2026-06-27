"""
Version vectors (vector clocks) for masterless lyric sync — pure, no I/O.

A vclock maps a location id -> a monotonically increasing counter. It captures
causal history so divergence is decided by what each side has *seen*, never by a
wall clock (mtime is a data-loss trap on FAT/exFAT and across machines).

Conventions:
- A vclock is a plain dict[str, int]; a missing key means 0.
- Canonical form drops zero entries, so equality is just dict equality.
- Every function is pure: inputs are never mutated; new dicts are returned.

Nothing consumes this yet — it is the substrate the Phase-4 lyric sync builds on.
"""
from __future__ import annotations

import json

Vclock = dict[str, int]


def normalize(v: Vclock) -> Vclock:
    """Canonical form: drop zero/negative counters."""
    return {k: int(c) for k, c in v.items() if int(c) > 0}


def bump(v: Vclock, key: str, by: int = 1) -> Vclock:
    """Return a copy of `v` with `key`'s counter advanced by `by` (default 1)."""
    out = normalize(v)
    out[key] = out.get(key, 0) + by
    return out


def merge(a: Vclock, b: Vclock) -> Vclock:
    """Componentwise max — the least vclock that descends from both."""
    out: Vclock = {}
    for k in set(a) | set(b):
        out[k] = max(int(a.get(k, 0)), int(b.get(k, 0)))
    return normalize(out)


def dominates(a: Vclock, b: Vclock) -> bool:
    """True if `a` is causally at-least-as-new as `b` (a[k] >= b[k] for all k)."""
    return all(int(a.get(k, 0)) >= int(b.get(k, 0)) for k in set(a) | set(b))


def strictly_dominates(a: Vclock, b: Vclock) -> bool:
    """True if `a` descends from `b` and is not equal to it (a is strictly newer)."""
    return dominates(a, b) and normalize(a) != normalize(b)


def concurrent(a: Vclock, b: Vclock) -> bool:
    """True if neither side dominates — a genuine conflict, not a fast-forward."""
    return not dominates(a, b) and not dominates(b, a)


def equal(a: Vclock, b: Vclock) -> bool:
    return normalize(a) == normalize(b)


def to_json(v: Vclock) -> str:
    """Deterministic JSON (sorted keys) of the canonical form, for storage."""
    return json.dumps(normalize(v), sort_keys=True, separators=(",", ":"))


def from_json(s: str | None) -> Vclock:
    """Parse a stored vclock; None/empty -> empty vclock."""
    if not s:
        return {}
    return normalize(json.loads(s))
