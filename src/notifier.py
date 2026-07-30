"""Notification dispatch: desktop toast + sound + Telegram."""

from __future__ import annotations
import asyncio
import logging
import shutil
import subprocess
import sys
import webbrowser
from collections.abc import Sequence

import httpx

# Platform-specific imports (graceful fallback on non-Windows)
try:
    import winsound
except ImportError:
    winsound = None  # type: ignore[assignment]

logger = logging.getLogger("watcher.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


async def send_telegram(
    bot_token: str, chat_ids: str | Sequence[str], message: str
) -> bool:
    """Send a Telegram message to every recipient. True only if all succeeded."""
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
    recipients = [str(c).strip() for c in chat_ids if str(c).strip()]

    if not bot_token or not recipients:
        logger.warning("Telegram not configured (missing bot_token or chat_id)")
        return False

    results = await asyncio.gather(
        *(_send_telegram_one(bot_token, chat_id, message) for chat_id in recipients)
    )
    sent = sum(results)
    if sent < len(recipients):
        logger.error(f"Telegram delivered to {sent}/{len(recipients)} recipients")
    else:
        logger.info(f"Telegram notification sent to {sent} recipient(s)")
    return all(results)


async def _send_telegram_one(bot_token: str, chat_id: str, message: str) -> bool:
    """Deliver one message to one chat."""
    url = TELEGRAM_API.format(token=bot_token)
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}

    # The first connect can be slow (TLS + DNS on a cold network), so allow a
    # generous timeout and retry once before giving up on an alert.
    for attempt in range(1, 3):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                logger.error(
                    f"Telegram API returned {resp.status_code} for chat {chat_id}: {resp.text}"
                )
                return False
        except Exception as e:
            logger.error(
                f"Telegram send to {chat_id} failed "
                f"(attempt {attempt}/2): {type(e).__name__}: {e}"
            )

    return False


CINEPLEX_URL = "https://www.cineplexbd.com/"


def _applescript_string(text: str) -> str:
    """Quote a Python string as an AppleScript literal."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped.replace("\n", " ") + '"'


def _notify_windows(title: str, message: str) -> bool:
    from winotify import Notification
    toast = Notification(
        app_id="CineplexBD Ticket Watcher",
        title=title,
        msg=message,
        duration="long",
    )
    toast.add_actions(label="Open CineplexBD", launch=CINEPLEX_URL)
    toast.show()
    return True


def _notify_macos(title: str, message: str) -> bool:
    """Notification Center alert. Sound is handled by play_alert_sound()."""
    if shutil.which("terminal-notifier"):
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message, "-open", CINEPLEX_URL],
            check=True,
            timeout=10,
            capture_output=True,
        )
        return True

    script = (
        f"display notification {_applescript_string(message)} "
        f"with title {_applescript_string(title)}"
    )
    subprocess.run(["osascript", "-e", script], check=True, timeout=10, capture_output=True)
    return True


def _notify_linux(title: str, message: str) -> bool:
    if not shutil.which("notify-send"):
        logger.warning("notify-send not found — skipping desktop notification")
        return False
    subprocess.run(["notify-send", title, message], check=True, timeout=10, capture_output=True)
    return True


def send_desktop_notification(title: str, message: str) -> bool:
    """Show a native desktop notification on Windows, macOS, or Linux."""
    backends = {
        "win32": _notify_windows,
        "darwin": _notify_macos,
    }
    backend = backends.get(sys.platform, _notify_linux)

    try:
        backend(title, message)
        logger.info("Desktop notification sent")
        return True
    except ImportError:
        logger.warning("winotify not installed — skipping desktop notification")
        return False
    except Exception as e:
        logger.error(f"Desktop notification failed: {e}")
        return False


def play_alert_sound() -> None:
    """Play an alert sound. No-op where no player is available."""
    from pathlib import Path

    sound_file = Path(__file__).parent.parent / "sounds" / "alert.wav"

    try:
        if winsound:
            if sound_file.exists():
                winsound.PlaySound(str(sound_file), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        elif sys.platform == "darwin":
            target = sound_file if sound_file.exists() else Path("/System/Library/Sounds/Glass.aiff")
            subprocess.Popen(["afplay", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sound_file.exists() and shutil.which("paplay"):
            subprocess.Popen(["paplay", str(sound_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.warning(f"Could not play sound: {e}")


def open_browser(url: str = "https://www.cineplexbd.com/") -> None:
    """Open the cinema site in the default browser."""
    try:
        webbrowser.open(url)
    except Exception as e:
        logger.warning(f"Could not open browser: {e}")


class Notifier:
    """Dispatches notifications to all enabled channels."""

    def __init__(
        self,
        desktop_enabled: bool = True,
        telegram_enabled: bool = False,
        telegram_token: str = "",
        telegram_chat_ids: Sequence[str] | str = (),
        open_browser_on_alert: bool = True,
    ):
        self.desktop_enabled = desktop_enabled
        self.telegram_enabled = telegram_enabled
        self.telegram_token = telegram_token
        if isinstance(telegram_chat_ids, str):
            telegram_chat_ids = [telegram_chat_ids] if telegram_chat_ids else []
        self.telegram_chat_ids = list(telegram_chat_ids)
        self.open_browser_on_alert = open_browser_on_alert

    @classmethod
    def from_config(cls, config) -> Notifier:
        """Create a Notifier from a Config object."""
        return cls(
            desktop_enabled=config.notifications.desktop.enabled,
            telegram_enabled=config.notifications.telegram.enabled,
            telegram_token=config.notifications.telegram.bot_token,
            telegram_chat_ids=config.notifications.telegram.recipients(),
        )

    async def notify_all(self, message: str, title: str = "New Tickets Available!") -> None:
        """Send notification to all enabled channels."""
        logger.info(f"ALERT: {title} - {message}")

        if self.desktop_enabled:
            send_desktop_notification(title, message)
            play_alert_sound()

        if self.telegram_enabled:
            telegram_msg = f"<b>{title}</b>\n\n{message}"
            await send_telegram(self.telegram_token, self.telegram_chat_ids, telegram_msg)

        if self.open_browser_on_alert:
            open_browser()
