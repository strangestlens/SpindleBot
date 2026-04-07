"""
Notification stage — send a message via macOS and/or Telegram.

Designed to be called directly from the runner or from the CLI:
    spindlebot notify "Rip complete" "Dark Side of the Moon"

Both channels are fire-and-forget: a failure in one does not affect the other,
and neither channel failure propagates to the caller.
"""
from __future__ import annotations

import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class NotifyResult:
    macos_sent: bool = False
    telegram_sent: bool = False
    macos_error: str = ""
    telegram_error: str = ""

    @property
    def any_sent(self) -> bool:
        return self.macos_sent or self.telegram_sent


def notify(title: str, message: str, cfg) -> NotifyResult:
    """
    Send title + message via all enabled notification channels.

    cfg is a SpindleBotConfig. Channels are enabled/disabled independently
    so a missing Telegram token silently skips that channel without affecting
    macOS notifications, and vice versa.
    """
    result = NotifyResult()

    if cfg.notifications.macos_notify:
        result.macos_sent, result.macos_error = _send_macos(title, message)

    if cfg.notifications.telegram_enabled:
        token = cfg.secrets.telegram.bot_token
        chat_id = cfg.secrets.telegram.chat_id
        if token and chat_id:
            result.telegram_sent, result.telegram_error = _send_telegram(
                title, message, token, chat_id
            )

    return result


def _send_macos(title: str, message: str) -> tuple[bool, str]:
    """Send a macOS notification via osascript. Returns (success, error_str)."""
    # Escape double quotes in user-supplied strings so the AppleScript is valid.
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}" '
        f'sound name "Glass"'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return False, proc.stderr.decode().strip()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _send_telegram(title: str, message: str, token: str, chat_id: str) -> tuple[bool, str]:
    """Send a Telegram message via the Bot API. Returns (success, error_str)."""
    text = f"🎵 *{title}*\n{message}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, ""
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, str(exc)
