"""
SpindleBot configuration loader.

Config:   ~/.config/spindlebot/config.toml   (paths, tools, behaviour)
Secrets:  ~/.config/spindlebot/secrets.toml  (tokens, API keys — never commit)

The config directory can be overridden with $SPINDLEBOT_CONFIG_DIR.
Individual secrets can also be overridden via environment variables:
  SPINDLEBOT_TELEGRAM_TOKEN, SPINDLEBOT_TELEGRAM_CHAT_ID, SPINDLEBOT_GENIUS_KEY

Run `python -m spindlebot check` to validate your installation.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── TOML parser — stdlib in 3.11+, tomli on 3.10 and below ──────────────────
try:
    import tomllib  # type: ignore[import]
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[import,no-redef]
    except ImportError:
        sys.exit(
            "tomli is required for Python < 3.11.\n"
            "Install it:  pip install tomli --break-system-packages"
        )

# ── Config directory ─────────────────────────────────────────────────────────
CONFIG_DIR = Path(
    os.environ.get("SPINDLEBOT_CONFIG_DIR", "~/.config/spindlebot")
).expanduser()


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class CoreConfig:
    import_dir: Path    # active import area (rips/downloads land here); formerly "staging"
    pending_dir: Path   # processed albums awaiting distribution; formerly "library"
    log_dir: Path
    archive_dir: Path
    db_path: Path       # SpindleBot's own SQLite system-of-record DB


@dataclass
class ToolsConfig:
    beet: Path
    python: Path
    beets_config: Path
    beets_db: Path
    mpv: Optional[Path] = None


@dataclass
class NotificationsConfig:
    macos_notify: bool = True
    telegram_enabled: bool = True


@dataclass
class LyricsConfig:
    sources: list = field(default_factory=lambda: ["lrclib"])
    request_delay_seconds: float = 0.3


@dataclass
class ArtConfig:
    sources: list = field(default_factory=lambda: ["caa", "itunes"])
    min_size_px: int = 500


@dataclass
class DestinationConfig:
    name: str
    type: str          # "local_drive" | "rclone"
    path: str
    enabled: bool = True


@dataclass
class TelegramSecrets:
    bot_token: str = ""
    chat_id: str = ""


@dataclass
class GeniusSecrets:
    api_key: str = ""


@dataclass
class Secrets:
    telegram: TelegramSecrets = field(default_factory=TelegramSecrets)
    genius: GeniusSecrets = field(default_factory=GeniusSecrets)


@dataclass
class SpindleBotConfig:
    core: CoreConfig
    tools: ToolsConfig
    notifications: NotificationsConfig
    lyrics: LyricsConfig
    art: ArtConfig
    destinations: list  # list[DestinationConfig]
    secrets: Secrets
    pipeline_dir: Path  # auto-detected from module location


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_toml(path: Path) -> dict:
    """Load a TOML file; return empty dict if it doesn't exist."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _expand(val: str) -> Path:
    """Expand ~ and env vars, return a Path."""
    return Path(os.path.expandvars(val)).expanduser()


# ── Public API ────────────────────────────────────────────────────────────────

def load() -> SpindleBotConfig:
    """
    Load config.toml + secrets.toml from CONFIG_DIR.
    Falls back to sensible defaults for every value so the pipeline keeps
    working even before setup.sh has been run.
    """
    raw = _load_toml(CONFIG_DIR / "config.toml")
    sec = _load_toml(CONFIG_DIR / "secrets.toml")

    # ── Core ─────────────────────────────────────────────────────────────────
    # New keys take precedence; fall back to the old staging_dir/library_dir keys
    # so an un-migrated config.toml keeps working, then to the relocated defaults.
    c = raw.get("core", {})
    core = CoreConfig(
        import_dir=_expand(
            c.get("import_dir",
                  c.get("staging_dir", "~/Library/Application Support/SpindleBot/Import"))
        ),
        pending_dir=_expand(
            c.get("pending_dir",
                  c.get("library_dir", "~/Library/Application Support/SpindleBot/Pending"))
        ),
        log_dir=_expand(c.get("log_dir", "~/.config/beets")),
        archive_dir=_expand(c.get("archive_dir", "~/Music/All Discs")),
        db_path=_expand(c.get("db_path", "~/.config/spindlebot/spindlebot.db")),
    )

    # ── Tools ─────────────────────────────────────────────────────────────────
    t = raw.get("tools", {})
    core_beet = t.get("beet", "/opt/homebrew/bin/beet")
    core_py   = t.get("python", sys.executable)
    tools = ToolsConfig(
        beet=_expand(core_beet),
        python=_expand(core_py),
        beets_config=_expand(t.get("beets_config", "~/.config/beets/config.yaml")),
        beets_db=_expand(t.get("beets_db", "~/.config/beets/library.db")),
        mpv=_expand(t["mpv"]) if t.get("mpv") else None,
    )

    # ── Notifications ─────────────────────────────────────────────────────────
    n = raw.get("notifications", {})
    notifications = NotificationsConfig(
        macos_notify=n.get("macos_notify", True),
        telegram_enabled=n.get("telegram_enabled", True),
    )

    # ── Lyrics ────────────────────────────────────────────────────────────────
    ly = raw.get("lyrics", {})
    lyrics = LyricsConfig(
        sources=ly.get("sources", ["lrclib"]),
        request_delay_seconds=float(ly.get("request_delay_seconds", 0.3)),
    )

    # ── Art ───────────────────────────────────────────────────────────────────
    ar = raw.get("art", {})
    art = ArtConfig(
        sources=ar.get("sources", ["caa", "itunes"]),
        min_size_px=int(ar.get("min_size_px", 500)),
    )

    # ── Destinations ──────────────────────────────────────────────────────────
    destinations = [
        DestinationConfig(
            name=d["name"],
            type=d["type"],
            path=d["path"],
            enabled=d.get("enabled", True),
        )
        for d in raw.get("destinations", [])
    ]

    # ── Secrets (config file < env var override) ──────────────────────────────
    st = sec.get("telegram", {})
    sg = sec.get("genius", {})
    secrets = Secrets(
        telegram=TelegramSecrets(
            bot_token=os.environ.get(
                "SPINDLEBOT_TELEGRAM_TOKEN", st.get("bot_token", "")
            ),
            chat_id=os.environ.get(
                "SPINDLEBOT_TELEGRAM_CHAT_ID", st.get("chat_id", "")
            ),
        ),
        genius=GeniusSecrets(
            api_key=os.environ.get(
                "SPINDLEBOT_GENIUS_KEY", sg.get("api_key", "")
            ),
        ),
    )

    # ── Pipeline dir (auto-detected) ──────────────────────────────────────────
    # config.py lives at <pipeline_dir>/spindlebot/config.py
    pipeline_dir = Path(__file__).parent.parent.resolve()

    return SpindleBotConfig(
        core=core,
        tools=tools,
        notifications=notifications,
        lyrics=lyrics,
        art=art,
        destinations=destinations,
        secrets=secrets,
        pipeline_dir=pipeline_dir,
    )
