"""The native guard overlay preserves configuration and fails closed."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from spindlebot.core.errors import SpindleBotError
from spindlebot.services.beets_guard import guarded_beet


def test_effective_plugins_and_paths_are_preserved_without_copying_secrets(monkeypatch):
    config = {
        "plugins": ["inline", "custom"],
        "pluginpath": ["/custom/plugins"],
        "disabled_plugins": ["other", "spindlebot_collision_guard"],
        "secret": "should-not-be-written",
    }
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps(config)))
    monkeypatch.setattr("spindlebot.services.beets_guard.subprocess.run", run)
    monkeypatch.setenv("PYTHONPATH", "existing-search-path")
    with guarded_beet("beet", "import") as (prefix, env):
        assert prefix[-1] == "spindlebot-guarded-import"
        overlay = Path(prefix[2])
        data = json.loads(overlay.read_text())
        assert data["plugins"] == ["inline", "custom", "spindlebot_collision_guard"]
        assert data["pluginpath"][-1] == "/custom/plugins"
        assert data["disabled_plugins"] == ["other"]
        assert "secret" not in overlay.read_text()
        assert env["PYTHONPATH"].endswith("existing-search-path")
        assert overlay.parent.stat().st_mode & 0o077 == 0
    assert not overlay.exists()
    run.assert_called_once_with(
        ["beet", "config", "--defaults"], capture_output=True, text=True
    )


def test_overlay_is_removed_even_when_the_beet_operation_raises(monkeypatch):
    monkeypatch.setattr(
        "spindlebot.services.beets_guard.subprocess.run",
        Mock(return_value=Mock(returncode=0, stdout="{}")),
    )
    with pytest.raises(RuntimeError):
        with guarded_beet("beet", "move") as (prefix, _):
            overlay = Path(prefix[2])
            raise RuntimeError("simulated interrupted operation")
    assert not overlay.parent.exists()


@pytest.mark.parametrize("stdout", ["", "[]", "plugins: [", "plugins: 1"])
def test_invalid_effective_configuration_refuses_execution(monkeypatch, stdout):
    monkeypatch.setattr(
        "spindlebot.services.beets_guard.subprocess.run",
        Mock(return_value=Mock(returncode=0, stdout=stdout)),
    )
    with pytest.raises(SpindleBotError):
        with guarded_beet("beet", "import"):
            pytest.fail("invalid configuration must not yield a runnable import")


def test_failed_configuration_command_does_not_leak_its_output(monkeypatch):
    monkeypatch.setattr(
        "spindlebot.services.beets_guard.subprocess.run",
        Mock(return_value=Mock(returncode=1, stdout="sensitive", stderr="secret")),
    )
    with pytest.raises(SpindleBotError) as error:
        with guarded_beet("beet", "import"):
            pytest.fail("failed configuration must not yield a runnable import")
    assert "sensitive" not in str(error.value) and "secret" not in str(error.value)
