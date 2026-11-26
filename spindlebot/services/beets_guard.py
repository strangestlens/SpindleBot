"""Prepare a private beets overlay which preserves existing plugins and paths."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

from spindlebot.core.errors import SpindleBotError

_GUARD = "spindlebot_collision_guard"
_PLUGIN_DIR = Path(__file__).parents[1] / "beets_plugins"


def _sequence(value, *, split: bool) -> list[str]:
    if isinstance(value, str):
        return value.split() if split else [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise SpindleBotError("beets plugins/pluginpath must be a string or a string list")


@contextmanager
def guarded_beet(beet: str | Path, command: str):
    """Yield (argv prefix, environment) with the collision adapter enabled.

    `beet config --defaults` gives the effective configuration, including
    includes and defaults. Only plugin names/search paths are copied into the
    overlay; captured config (which may contain credentials) is never logged.
    The native config remains the base, including all plugins and path rules.
    """
    proc = subprocess.run(
        [str(beet), "config", "--defaults"], capture_output=True, text=True
    )
    if proc.returncode:
        raise SpindleBotError(
            "could not read effective beets configuration for collision protection"
        )
    try:
        config = yaml.safe_load(proc.stdout)
        if not isinstance(config, dict):
            raise ValueError("not a mapping")
        plugins = _sequence(config.get("plugins", []), split=True)
        paths = _sequence(config.get("pluginpath", []), split=False)
    except (yaml.YAMLError, ValueError) as exc:
        raise SpindleBotError(
            "invalid effective beets configuration for collision protection"
        ) from exc
    if _GUARD not in plugins:
        plugins.append(_GUARD)
    paths = [str(_PLUGIN_DIR), *[p for p in paths if p != str(_PLUGIN_DIR)]]
    disabled = _sequence(config.get("disabled_plugins", []), split=True)
    overlay = {
        "plugins": plugins,
        "pluginpath": paths,
        "disabled_plugins": [p for p in disabled if p != _GUARD],
    }
    env = os.environ.copy()
    root = str(Path(__file__).parents[2])
    env["PYTHONPATH"] = root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    # TemporaryDirectory is private (0700); the overlay carries no credentials.
    with tempfile.TemporaryDirectory(prefix="spindlebot-beets-guard-") as temporary:
        path = Path(temporary) / "config.json"
        path.write_text(json.dumps(overlay), encoding="utf-8")
        yield [str(beet), "-c", str(path), f"spindlebot-guarded-{command}"], env
