"""
Tests for spindlebot.pipeline.stages.notify.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


from spindlebot.pipeline.stages.notify import _send_macos, _send_telegram, notify


# ── helpers ───────────────────────────────────────────────────────────────────


def _cfg(*, macos=True, telegram=True, token="tok", chat_id="chat"):
    """Build a minimal config mock."""
    cfg = MagicMock()
    cfg.notifications.macos_notify = macos
    cfg.notifications.telegram_enabled = telegram
    cfg.secrets.telegram.bot_token = token
    cfg.secrets.telegram.chat_id = chat_id
    return cfg


# ── _send_macos ───────────────────────────────────────────────────────────────


class TestSendMacOS:

    def test_success(self):
        proc = MagicMock(returncode=0, stderr=b"")
        with patch("spindlebot.pipeline.stages.notify.subprocess.run", return_value=proc):
            ok, err = _send_macos("Title", "Message")
        assert ok is True
        assert err == ""

    def test_osascript_failure_returns_false(self):
        proc = MagicMock(returncode=1, stderr=b"execution error")
        with patch("spindlebot.pipeline.stages.notify.subprocess.run", return_value=proc):
            ok, err = _send_macos("Title", "Message")
        assert ok is False
        assert "execution error" in err

    def test_exception_returns_false(self):
        with patch("spindlebot.pipeline.stages.notify.subprocess.run",
                   side_effect=FileNotFoundError("osascript not found")):
            ok, err = _send_macos("Title", "Message")
        assert ok is False
        assert "osascript not found" in err

    def test_quotes_escaped_in_title(self):
        """Double quotes in title must not break the AppleScript string."""
        captured = {}
        def _capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stderr=b"")
        with patch("spindlebot.pipeline.stages.notify.subprocess.run", side_effect=_capture):
            _send_macos('She said "hello"', "Message")
        script = captured["cmd"][2]
        # The raw quote must not appear unescaped inside the AppleScript string
        assert 'with title "She said \\"hello\\""' in script

    def test_quotes_escaped_in_message(self):
        captured = {}
        def _capture(cmd, **kwargs):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stderr=b"")
        with patch("spindlebot.pipeline.stages.notify.subprocess.run", side_effect=_capture):
            _send_macos("Title", 'It\'s a "test"')
        script = captured["cmd"][2]
        assert '\\"test\\"' in script


# ── _send_telegram ────────────────────────────────────────────────────────────


class TestSendTelegram:

    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("spindlebot.pipeline.stages.notify.urllib.request.urlopen",
                   return_value=mock_resp):
            ok, err = _send_telegram("Title", "Message", "token", "chat")
        assert ok is True
        assert err == ""

    def test_http_error_returns_false(self):
        import urllib.error
        with patch("spindlebot.pipeline.stages.notify.urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 403, "Forbidden", {}, None)):
            ok, err = _send_telegram("Title", "Message", "token", "chat")
        assert ok is False
        assert "403" in err

    def test_network_error_returns_false(self):
        with patch("spindlebot.pipeline.stages.notify.urllib.request.urlopen",
                   side_effect=OSError("network unreachable")):
            ok, err = _send_telegram("Title", "Message", "token", "chat")
        assert ok is False
        assert "network unreachable" in err


# ── notify (orchestration) ────────────────────────────────────────────────────


class TestNotify:

    def test_both_channels_called_when_enabled(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(True, "")) as m_macos, \
             patch("spindlebot.pipeline.stages.notify._send_telegram",
                   return_value=(True, "")) as m_telegram:
            result = notify("T", "M", _cfg(macos=True, telegram=True))
        m_macos.assert_called_once_with("T", "M")
        m_telegram.assert_called_once()
        assert result.macos_sent
        assert result.telegram_sent

    def test_macos_skipped_when_disabled(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos") as m_macos, \
             patch("spindlebot.pipeline.stages.notify._send_telegram",
                   return_value=(True, "")):
            notify("T", "M", _cfg(macos=False))
        m_macos.assert_not_called()

    def test_telegram_skipped_when_disabled(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(True, "")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram") as m_tg:
            notify("T", "M", _cfg(telegram=False))
        m_tg.assert_not_called()

    def test_telegram_skipped_when_token_missing(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(True, "")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram") as m_tg:
            notify("T", "M", _cfg(token="", chat_id="chat"))
        m_tg.assert_not_called()

    def test_telegram_skipped_when_chat_id_missing(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(True, "")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram") as m_tg:
            notify("T", "M", _cfg(token="tok", chat_id=""))
        m_tg.assert_not_called()

    def test_macos_failure_does_not_prevent_telegram(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(False, "osascript failed")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram",
                   return_value=(True, "")) as m_tg:
            result = notify("T", "M", _cfg())
        m_tg.assert_called_once()
        assert result.telegram_sent
        assert not result.macos_sent

    def test_any_sent_false_when_both_fail(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(False, "err")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram",
                   return_value=(False, "err")):
            result = notify("T", "M", _cfg())
        assert not result.any_sent

    def test_any_sent_true_when_one_succeeds(self):
        with patch("spindlebot.pipeline.stages.notify._send_macos",
                   return_value=(True, "")), \
             patch("spindlebot.pipeline.stages.notify._send_telegram",
                   return_value=(False, "err")):
            result = notify("T", "M", _cfg())
        assert result.any_sent
