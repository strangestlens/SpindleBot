"""
Tests for spindlebot.config — loading, defaults, env var overrides, validation.

Run with:
    cd /path/to/music-pipeline
    python -m pytest tests/ -v          # if pytest is installed
    python -m unittest discover tests   # with stdlib only
"""

import os
import sys
import textwrap
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure the package is importable when running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from spindlebot.config import (
    load,
    CoreConfig,
    ToolsConfig,
    NotificationsConfig,
    LyricsConfig,
    ArtConfig,
    Secrets,
    SpindleBotConfig,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(tmp: Path, name: str, content: str) -> Path:
    """Write a TOML file to a temp directory."""
    p = tmp / name
    p.write_text(textwrap.dedent(content))
    return p


def _load_with_dir(tmp: Path) -> SpindleBotConfig:
    """Load config using tmp as the config dir."""
    with mock.patch("spindlebot.config.CONFIG_DIR", tmp):
        return load()


# ── minimal valid config ──────────────────────────────────────────────────────

MINIMAL_CONFIG = """\
    [core]
    pending_dir = "/tmp/Library"
    import_dir  = "/tmp/Staging"
    log_dir     = "/tmp/logs"
    archive_dir = "/tmp/AllDiscs"

    [tools]
    beet         = "/usr/bin/true"
    python       = "/usr/bin/true"
    beets_config = "/tmp/beets/config.yaml"
    beets_db     = "/tmp/beets/library.db"
"""

MINIMAL_SECRETS = """\
    [telegram]
    bot_token = ""
    chat_id   = ""

    [genius]
    api_key = ""
"""


class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_dir.name)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_loads_minimal_config(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertIsInstance(cfg, SpindleBotConfig)

    def test_core_paths_are_path_objects(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertIsInstance(cfg.core.pending_dir, Path)
        self.assertIsInstance(cfg.core.import_dir, Path)
        self.assertIsInstance(cfg.core.log_dir, Path)
        self.assertIsInstance(cfg.core.archive_dir, Path)

    def test_tool_paths_are_path_objects(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertIsInstance(cfg.tools.beet, Path)
        self.assertIsInstance(cfg.tools.python, Path)

    def test_home_tilde_expansion(self):
        config = MINIMAL_CONFIG.replace("/tmp/Library", "~/Music/Library")
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertFalse(str(cfg.core.pending_dir).startswith("~"))
        self.assertTrue(str(cfg.core.pending_dir).startswith(str(Path.home())))

    def test_missing_config_file_uses_defaults(self):
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        # No config.toml — should not raise, should use sensible defaults
        cfg = _load_with_dir(self.tmp)
        self.assertIsNotNone(cfg.core.pending_dir)
        self.assertIsNotNone(cfg.core.import_dir)

    def test_db_path_defaults_to_spindlebot_db(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertTrue(str(cfg.core.db_path).endswith("spindlebot.db"))

    def test_min_copies_defaults_to_one(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.core.min_copies, 1)

    def test_min_copies_override_and_floor(self):
        config = """\
            [core]
            pending_dir = "/tmp/Pending"
            import_dir  = "/tmp/Import"
            min_copies = 2
        """
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        self.assertEqual(_load_with_dir(self.tmp).core.min_copies, 2)

        floored = config.replace("min_copies = 2", "min_copies = 0")
        _write(self.tmp, "config.toml", floored)
        # min_copies can never drop below 1 — a 0 floor would defeat retention.
        self.assertEqual(_load_with_dir(self.tmp).core.min_copies, 1)

    def test_legacy_staging_library_keys_still_honored(self):
        # Pre-rename config.toml files use staging_dir/library_dir — they must
        # still map to import_dir/pending_dir until users migrate.
        legacy = """\
            [core]
            library_dir = "/tmp/OldLibrary"
            staging_dir = "/tmp/OldStaging"
        """
        _write(self.tmp, "config.toml", legacy)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(str(cfg.core.pending_dir), "/tmp/OldLibrary")
        self.assertEqual(str(cfg.core.import_dir), "/tmp/OldStaging")

    def test_missing_secrets_file_uses_empty_defaults(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        # No secrets.toml — should not raise, tokens should be empty strings
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.secrets.telegram.bot_token, "")
        self.assertEqual(cfg.secrets.genius.api_key, "")

    def test_locations_block_parsed(self):
        config = """\
            [core]
            pending_dir = "/tmp/Pending"
            import_dir  = "/tmp/Import"

            [[locations]]
            name = "DwRugged"
            kind = "local_drive"
            root_path = "/Volumes/DwRugged/Music/Library"
            is_retention = true
        """
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(len(cfg.locations), 1)
        loc = cfg.locations[0]
        self.assertEqual(loc.name, "DwRugged")
        self.assertEqual(loc.kind, "local_drive")
        self.assertEqual(loc.root_path, "/Volumes/DwRugged/Music/Library")
        self.assertTrue(loc.is_retention)

    def test_locations_default_to_empty_list(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.locations, [])


class TestDefaults(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_dir.name)
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        self.cfg = _load_with_dir(self.tmp)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_notifications_default_to_enabled(self):
        self.assertTrue(self.cfg.notifications.macos_notify)
        self.assertTrue(self.cfg.notifications.telegram_enabled)

    def test_lyrics_delay_default(self):
        self.assertAlmostEqual(self.cfg.lyrics.request_delay_seconds, 0.3, places=5)

    def test_art_sources_not_empty(self):
        self.assertIsInstance(self.cfg.art.sources, list)
        self.assertGreater(len(self.cfg.art.sources), 0)

    def test_destinations_is_list(self):
        self.assertIsInstance(self.cfg.destinations, list)

    def test_pipeline_dir_is_set(self):
        self.assertTrue(self.cfg.pipeline_dir.exists())
        self.assertTrue(self.cfg.pipeline_dir.is_dir())


class TestEnvVarOverrides(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_dir.name)
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_telegram_token_from_env(self):
        with mock.patch.dict(os.environ, {"SPINDLEBOT_TELEGRAM_TOKEN": "abc123"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.secrets.telegram.bot_token, "abc123")

    def test_telegram_chat_id_from_env(self):
        with mock.patch.dict(os.environ, {"SPINDLEBOT_TELEGRAM_CHAT_ID": "999"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.secrets.telegram.chat_id, "999")

    def test_genius_key_from_env(self):
        with mock.patch.dict(os.environ, {"SPINDLEBOT_GENIUS_KEY": "mykey"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.secrets.genius.api_key, "mykey")

    def test_env_overrides_file_value(self):
        secrets = MINIMAL_SECRETS.replace('bot_token = ""', 'bot_token = "from_file"')
        _write(self.tmp, "secrets.toml", secrets)
        with mock.patch.dict(os.environ, {"SPINDLEBOT_TELEGRAM_TOKEN": "from_env"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(cfg.secrets.telegram.bot_token, "from_env")


class TestDestinations(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_dir.name)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)

    def tearDown(self):
        self._tmp_dir.cleanup()

    def test_single_destination(self):
        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "TestDrive"
            type    = "local_drive"
            path    = "/Volumes/TestDrive/Music"
            enabled = true
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(len(cfg.destinations), 1)
        self.assertEqual(cfg.destinations[0].name, "TestDrive")
        self.assertTrue(cfg.destinations[0].enabled)

    def test_disabled_destination(self):
        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "Backblaze"
            type    = "rclone"
            path    = "b2:my-bucket"
            enabled = false
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)
        self.assertFalse(cfg.destinations[0].enabled)

    def test_multiple_destinations(self):
        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "Drive1"
            type    = "local_drive"
            path    = "/Volumes/Drive1"
            enabled = true

            [[destinations]]
            name    = "Drive2"
            type    = "local_drive"
            path    = "/Volumes/Drive2"
            enabled = false
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(len(cfg.destinations), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
