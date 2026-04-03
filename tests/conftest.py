"""
pytest configuration.

Registers hyphenated pipeline scripts (music-pretag.py etc.) as importable
Python modules by loading them via importlib and injecting into sys.modules.
"""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _register(module_name: str, file_name: str) -> None:
    """Load file_name from repo root and register it as module_name."""
    path = REPO_ROOT / file_name
    if not path.exists():
        return
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.modules[module_name] = mod


_register("music_pretag", "music-pretag.py")
