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

    def test_duplicates_dir_default(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertIsInstance(cfg.core.duplicates_dir, Path)
        self.assertTrue(str(cfg.core.duplicates_dir).endswith("Duplicates"))
        self.assertFalse(str(cfg.core.duplicates_dir).startswith("~"))

    def test_duplicates_dir_from_config_file(self):
        config = MINIMAL_CONFIG.replace(
            'archive_dir = "/tmp/AllDiscs"',
            'archive_dir = "/tmp/AllDiscs"\n    duplicates_dir = "/tmp/Dupes"',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(str(cfg.core.duplicates_dir), "/tmp/Dupes")

    def test_duplicates_dir_env_override(self):
        # SPINDLEBOT_DUPLICATES_DIR wins over the config.toml value.
        config = MINIMAL_CONFIG.replace(
            'archive_dir = "/tmp/AllDiscs"',
            'archive_dir = "/tmp/AllDiscs"\n    duplicates_dir = "/tmp/Dupes"',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        with mock.patch.dict(os.environ, {"SPINDLEBOT_DUPLICATES_DIR": "/tmp/FromEnv"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(str(cfg.core.duplicates_dir), "/tmp/FromEnv")

    def test_processing_dir_default(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertIsInstance(cfg.core.processing_dir, Path)
        self.assertTrue(str(cfg.core.processing_dir).endswith("Processing"))
        self.assertFalse(str(cfg.core.processing_dir).startswith("~"))

    def test_processing_dir_from_config_file(self):
        config = MINIMAL_CONFIG.replace(
            'import_dir  = "/tmp/Staging"',
            'import_dir  = "/tmp/Staging"\n    processing_dir = "/tmp/Proc"',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(str(cfg.core.processing_dir), "/tmp/Proc")

    def test_processing_dir_env_override(self):
        config = MINIMAL_CONFIG.replace(
            'import_dir  = "/tmp/Staging"',
            'import_dir  = "/tmp/Staging"\n    processing_dir = "/tmp/Proc"',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        with mock.patch.dict(os.environ, {"SPINDLEBOT_PROCESSING_DIR": "/tmp/FromEnv"}):
            cfg = _load_with_dir(self.tmp)
        self.assertEqual(str(cfg.core.processing_dir), "/tmp/FromEnv")

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

    def test_auto_sync_on_import_defaults_false(self):
        _write(self.tmp, "config.toml", MINIMAL_CONFIG)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertFalse(cfg.core.auto_sync_on_import)

    def test_auto_sync_on_import_from_config_file(self):
        config = MINIMAL_CONFIG.replace(
            'archive_dir = "/tmp/AllDiscs"',
            'archive_dir = "/tmp/AllDiscs"\n    auto_sync_on_import = true',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertTrue(cfg.core.auto_sync_on_import)

    def test_auto_sync_on_import_env_override(self):
        # Env var wins over the config.toml value (and parses truthy strings).
        config = MINIMAL_CONFIG.replace(
            'archive_dir = "/tmp/AllDiscs"',
            'archive_dir = "/tmp/AllDiscs"\n    auto_sync_on_import = false',
        )
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        with mock.patch.dict(os.environ, {"SPINDLEBOT_AUTO_SYNC_ON_IMPORT": "1"}):
            cfg = _load_with_dir(self.tmp)
        self.assertTrue(cfg.core.auto_sync_on_import)

        # An explicit falsey env value also overrides a true config value.
        config_true = MINIMAL_CONFIG.replace(
            'archive_dir = "/tmp/AllDiscs"',
            'archive_dir = "/tmp/AllDiscs"\n    auto_sync_on_import = true',
        )
        _write(self.tmp, "config.toml", config_true)
        with mock.patch.dict(os.environ, {"SPINDLEBOT_AUTO_SYNC_ON_IMPORT": "off"}):
            cfg = _load_with_dir(self.tmp)
        self.assertFalse(cfg.core.auto_sync_on_import)

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
            name = "RetentionDrive"
            kind = "local_drive"
            root_path = "/Volumes/RetentionDrive/Music/Library"
            is_retention = true
        """
        _write(self.tmp, "config.toml", config)
        _write(self.tmp, "secrets.toml", MINIMAL_SECRETS)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(len(cfg.locations), 1)
        loc = cfg.locations[0]
        self.assertEqual(loc.name, "RetentionDrive")
        self.assertEqual(loc.kind, "local_drive")
        self.assertEqual(loc.root_path, "/Volumes/RetentionDrive/Music/Library")
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

    def test_retention_path_skips_rclone_destinations(self):
        # An enabled rclone destination listed FIRST must not become the
        # auto-sync mount probe — `Path("b2:...").exists()` is always false, so
        # auto-sync would silently never fire. The first enabled local_drive
        # wins; rclone-only (or none enabled) resolves to None.
        from spindlebot.cli import _retention_path

        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "Backblaze"
            type    = "rclone"
            path    = "b2:my-bucket/Library"
            enabled = true

            [[destinations]]
            name    = "RetentionDrive"
            type    = "local_drive"
            path    = "/Volumes/RetentionDrive/Music/Library"
            enabled = true
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)
        self.assertEqual(
            _retention_path(cfg.destinations),
            Path("/Volumes/RetentionDrive/Music/Library"),
        )

    def test_config_shell_destination_path_prefers_local_drive(self):
        # music-sync-retention_drive.sh uses SPINDLEBOT_DESTINATION_PATH as REMOTE for
        # its own `[ ! -d "$REMOTE" ]` mount check — an enabled rclone
        # destination listed first must not be exported, or the script would
        # silently no-op even with the drive mounted.
        import contextlib
        import io

        from spindlebot.cli import cmd_config_shell

        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "Backblaze"
            type    = "rclone"
            path    = "b2:my-bucket/Library"
            enabled = true

            [[destinations]]
            name    = "RetentionDrive"
            type    = "local_drive"
            path    = "/Volumes/RetentionDrive/Music/Library"
            enabled = true
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_config_shell(cfg)
        self.assertIn(
            "export SPINDLEBOT_DESTINATION_PATH='/Volumes/RetentionDrive/Music/Library'",
            out.getvalue(),
        )
        # Name + watch volume come from the SAME (first enabled local_drive)
        # destination — the sync agent and its launchd trigger key off these,
        # so nothing is hardcoded to a particular drive.
        self.assertIn(
            "export SPINDLEBOT_DESTINATION_NAME='RetentionDrive'",
            out.getvalue(),
        )
        self.assertIn(
            "export SPINDLEBOT_WATCH_VOLUME='/Volumes/RetentionDrive'",
            out.getvalue(),
        )
        self.assertNotIn("b2:my-bucket", out.getvalue())

    def test_retention_path_none_without_local_drive(self):
        from spindlebot.cli import _retention_path

        config = MINIMAL_CONFIG + textwrap.dedent("""\
            [[destinations]]
            name    = "Backblaze"
            type    = "rclone"
            path    = "b2:my-bucket/Library"
            enabled = true

            [[destinations]]
            name    = "Unmounted"
            type    = "local_drive"
            path    = "/Volumes/Off/Music"
            enabled = false
        """)
        _write(self.tmp, "config.toml", config)
        cfg = _load_with_dir(self.tmp)
        self.assertIsNone(_retention_path(cfg.destinations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
