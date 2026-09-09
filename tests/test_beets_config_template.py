"""Guards on the shipped beets config template (`beets-config.yaml`).

This file is not loaded by the pipeline — beets reads the installed copy — so
nothing else would notice if it regressed. It is worth guarding anyway for two
reasons: a live Genius API key was committed here once and stayed in the tree
for months, and the path template encodes rules CLAUDE.md states as
non-negotiable (4b: unreadable album names use `album_dir`, never a retagged
`$album`).

Parsed as text rather than with PyYAML: `requirements.txt` deliberately keeps
the core light, and a template guard is not worth a new dependency.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = REPO_ROOT / "beets-config.yaml"


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestNoCommittedSecret:
    """A working Genius key sat in this file from 1d03da1 until it was found in
    Sept 2026. Blanking it does not un-expose it, but it must not come back."""

    def test_template_exists(self):
        assert TEMPLATE.is_file()

    def test_genius_api_key_is_empty(self):
        m = re.search(r"^\s*genius_api_key:\s*(.*)$", _template(), re.M)
        assert m, "genius_api_key key is gone — did the lyrics block move?"
        assert m.group(1).strip() in ('""', "''"), (
            "beets-config.yaml carries a non-empty genius_api_key. Keys belong in "
            "the installed copy at tools.beets_config, never in the repo."
        )

    def test_no_long_opaque_token_anywhere(self):
        # Any 32+ char run of key-ish characters on a value line. Catches a key
        # pasted under a different name, which the check above would miss.
        for line in _template().splitlines():
            if line.lstrip().startswith("#") or ":" not in line:
                continue
            value = line.split(":", 1)[1]
            assert not re.search(r"[A-Za-z0-9_\-]{32,}", value), (
                f"possible committed secret in beets-config.yaml: {line.strip()[:60]}"
            )


class TestPathTemplate:
    """CLAUDE.md 4b: an album name that sanitizes into noise gets a directory
    name via `album_dir`, and `$album` stays canonical because mbsync would
    revert a retag on the next metadata sync."""

    def test_uses_album_dir_override(self):
        assert "%ifdef{album_dir,,$album}" in _template(), (
            "the album_dir override is gone from the path template — an album "
            "whose name sanitizes to noise would file under the noise name"
        )

    def test_override_applies_to_both_multidisc_branches(self):
        m = re.search(r"^\s*default:\s*(.+)$", _template(), re.M)
        assert m
        assert m.group(1).count("%ifdef{album_dir,,$album}") == 2, (
            "both the multidisc and single-disc branches must honour album_dir"
        )

    def test_mbsync_enabled(self):
        # 4b's reasoning depends on mbsync being active; if it is ever dropped,
        # the "never retag $album" rule needs revisiting rather than silently
        # becoming stale advice.
        assert re.search(r"^\s*-\s*mbsync\s*$", _template(), re.M)


class TestSingleTemplate:
    """config.yaml and beets-config.yaml were duplicate copies that drifted
    apart. One template, or they diverge again."""

    def test_no_duplicate_beets_config_at_repo_root(self):
        dupes = [p.name for p in REPO_ROOT.glob("*.yaml")
                 if p.name != "beets-config.yaml"
                 and "directory:" in p.read_text(encoding="utf-8", errors="ignore")]
        assert not dupes, f"a second beets config template reappeared: {dupes}"


class TestMultidiscInline:
    """Known gotcha: the inline field must yield "" (not 0) for false, because
    beets templates treat the string "0" as truthy."""

    def test_false_branch_is_empty_string(self):
        m = re.search(r"^\s*multidisc:\s*(.+)$", _template(), re.M)
        assert m
        assert m.group(1).rstrip().endswith(('else ""\'', 'else ""')), (
            f"multidisc false branch must be an empty string, got: {m.group(1)}"
        )
