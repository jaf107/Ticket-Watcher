"""Web dashboard server — aiohttp + SSE for real-time monitoring."""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .api_client import CineplexAPI
from .config_loader import (
    CinemaConfig, Config, LocationRef, MovieConfig, WatchConfig,
    load_config, save_config,
)
from .monitor import run_monitor, load_state, save_state, empty_state, target_dates

logger = logging.getLogger("watcher.web")

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"
SESSION_COOKIE = "tw_session"

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ticket Watcher — Sign in</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ctext y='26' font-size='26'%3E%F0%9F%8E%AC%3C/text%3E%3C/svg%3E">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
       background:#0c0c1d;color:#e2e8f0;min-height:100vh;
       display:flex;align-items:center;justify-content:center;padding:1.5rem}
  .card{background:#161630;border:1px solid #2a2a50;border-radius:12px;
        padding:2rem;width:100%;max-width:360px}
  .icon{width:48px;height:48px;background:linear-gradient(135deg,#7c3aed,#5b21b6);
        border-radius:14px;display:flex;align-items:center;justify-content:center;
        font-size:24px;margin-bottom:1.25rem}
  h1{font-size:1.15rem;margin-bottom:0.35rem}
  p{font-size:0.85rem;color:#94a3b8;margin-bottom:1.5rem}
  input{width:100%;padding:10px 12px;background:#0c0c1d;color:#e2e8f0;
        border:1px solid #2a2a50;border-radius:8px;font-size:0.9rem;font-family:inherit}
  input:focus{border-color:#7c3aed;outline:none}
  button{width:100%;margin-top:0.75rem;padding:10px;background:#7c3aed;color:#fff;
         border:none;border-radius:8px;font-size:0.9rem;font-weight:600;
         cursor:pointer;font-family:inherit}
  button:hover{background:#5b21b6}
  .err{margin-top:0.75rem;font-size:0.82rem;color:#ef4444}
</style></head><body>
<form class="card" method="POST" action="/login">
  <div class="icon">&#127916;</div>
  <h1>Ticket Watcher</h1>
  <p>Enter the dashboard password to continue.</p>
  <input type="password" name="password" placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>
  __ERROR__
</form></body></html>
"""


def dashboard_password() -> str:
    """Password gating the dashboard. Empty means auth is disabled."""
    return os.environ.get("DASHBOARD_PASSWORD", "").strip()


def _session_token(password: str) -> str:
    """Cookie value proving knowledge of the password.

    Derived rather than random so it survives restarts without server-side
    session storage, and changes the moment the password does.
    """
    return hmac.new(password.encode(), b"ticket-watcher-session", hashlib.sha256).hexdigest()


def is_authenticated(request: web.Request) -> bool:
    password = dashboard_password()
    if not password:
        return True
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return hmac.compare_digest(cookie, _session_token(password))


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Gate everything except login and the unauthenticated health check."""
    if request.path in ("/login", "/api/health") or is_authenticated(request):
        return await handler(request)

    if request.path.startswith("/api/"):
        return web.json_response({"ok": False, "message": "Not authenticated"}, status=401)
    raise web.HTTPFound("/login")


async def handle_login_page(request: web.Request) -> web.Response:
    if is_authenticated(request):
        raise web.HTTPFound("/")
    return web.Response(
        text=LOGIN_PAGE.replace("__ERROR__", ""),
        content_type="text/html",
    )


async def handle_login(request: web.Request) -> web.Response:
    password = dashboard_password()
    form = await request.post()
    supplied = str(form.get("password", ""))

    if not password or not hmac.compare_digest(supplied, password):
        # Uniform delay so a wrong password cannot be distinguished by timing.
        await asyncio.sleep(1)
        return web.Response(
            text=LOGIN_PAGE.replace("__ERROR__", '<div class="err">Wrong password.</div>'),
            content_type="text/html",
            status=401,
        )

    response = web.HTTPFound("/")
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(password),
        httponly=True,
        samesite="Lax",
        secure=request.url.scheme == "https",
        max_age=30 * 24 * 3600,
    )
    return response


class WatcherHub:
    """Bridges the monitor loop with SSE web clients."""

    def __init__(self, config: Config):
        self.config = config
        self.api = CineplexAPI()
        self.running = False
        self.task: asyncio.Task | None = None
        self.stop_event: asyncio.Event | None = None
        self.subscribers: list[asyncio.Queue] = []
        self.activity_log: list[dict] = []
        self.stats = {"checks": 0, "alerts": 0, "errors": 0, "started_at": None}
        self.current_dates: list[str] = []

        # Dates per target key, seeded from whatever the last run persisted.
        state = load_state()
        targets = config.targets()
        self.dates_by_target: dict[str, list[str]] = {
            t.key: target_dates(state, t) for t in targets
        }
        self.current_dates = self.dates_by_target.get(targets[0].key, []) if targets else []

    async def on_event(self, event: dict) -> None:
        """Callback from the monitor loop — broadcast to all SSE clients."""
        etype = event.get("type")

        if etype == "check" or etype == "alert":
            self.stats["checks"] = event.get("count", self.stats["checks"])
            key = event.get("target")
            if key:
                self.dates_by_target[key] = event.get("dates", [])
            self.current_dates = event.get("dates", self.current_dates)
            if etype == "alert":
                self.stats["alerts"] += 1

        elif etype == "error":
            self.stats["errors"] += 1

        elif etype == "stopped":
            self.running = False
            self.stats["started_at"] = None

        # Keep last 100 log entries
        self.activity_log.append(event)
        if len(self.activity_log) > 100:
            self.activity_log = self.activity_log[-100:]

        await self.broadcast(event)

    async def start(self) -> bool:
        """Start the monitor loop as a background task."""
        if self.running:
            return False

        self.running = True
        self.stop_event = asyncio.Event()
        self.stats["started_at"] = datetime.now(timezone.utc).isoformat()
        self.stats["checks"] = 0
        self.stats["errors"] = 0

        self.task = asyncio.create_task(self._run_monitor())
        return True

    async def _run_monitor(self) -> None:
        """Wrapper that catches exceptions and resets running state."""
        try:
            await run_monitor(
                self.config,
                on_event=self.on_event,
                stop_event=self.stop_event,
                api=self.api,
            )
        except Exception as e:
            logger.exception("Monitor task crashed")
            await self.on_event({"type": "stopped", "reason": f"Crashed: {e}"})
        finally:
            self.running = False

    async def stop(self) -> bool:
        """Stop the monitor loop."""
        if not self.running or not self.stop_event:
            return False

        self.stop_event.set()
        if self.task:
            try:
                await asyncio.wait_for(self.task, timeout=10)
            except asyncio.TimeoutError:
                self.task.cancel()
        self.running = False
        return True

    async def update_config(self, location_id: int, location_name: str,
                            movie_id: int, movie_name: str) -> None:
        """Update movie/location config and restart monitor if running."""
        was_running = self.running
        if was_running:
            await self.stop()

        # The dashboard manages a single pair, so it replaces the watch list
        # outright. Multi-target setups are configured via config.yaml or
        # `python main.py setup`.
        self.config.watches = [
            WatchConfig(
                movie=MovieConfig(id=movie_id, name=movie_name),
                locations=[LocationRef(id=location_id, name=location_name)],
            )
        ]
        self.config.cinema.location_id = location_id
        self.config.cinema.location = location_name
        self.config.movie.id = movie_id
        self.config.movie.name = movie_name
        save_config(self.config)

        # Reset state for fresh detection
        self.current_dates = []
        save_state(empty_state())

        await self.broadcast({
            "type": "config_changed",
            "movie": movie_name,
            "movie_id": movie_id,
            "location": location_name,
            "location_id": location_id,
        })

        if was_running:
            await self.start()

    async def _apply(self, changed: dict) -> None:
        """Persist config and restart the monitor so changes take effect."""
        was_running = self.running
        if was_running:
            await self.stop()

        save_config(self.config)
        await self.broadcast(changed)

        if was_running:
            await self.start()

    async def add_target(self, movie_id: int, movie_name: str,
                         location_id: int, location_name: str) -> bool:
        """Add one movie/location pair. False if it was already being watched."""
        key = f"{movie_id}@{location_id}"
        if any(t.key == key for t in self.config.targets()):
            return False

        for watch in self.config.watches:
            if watch.movie.id is not None and int(watch.movie.id) == movie_id:
                watch.locations.append(LocationRef(id=location_id, name=location_name))
                break
        else:
            self.config.watches.append(
                WatchConfig(
                    movie=MovieConfig(id=movie_id, name=movie_name),
                    locations=[LocationRef(id=location_id, name=location_name)],
                )
            )

        # A brand new target has no baseline, so its first check scans silently
        # instead of reporting every existing date as new.
        self.dates_by_target.setdefault(key, [])
        await self._apply({
            "type": "targets_changed",
            "message": f"Now watching {movie_name} at {location_name}",
        })
        return True

    async def remove_target(self, key: str) -> bool:
        """Stop watching one pair and drop its stored dates."""
        if not any(t.key == key for t in self.config.targets()):
            return False

        movie_id, _, location_id = key.partition("@")
        for watch in self.config.watches:
            if watch.movie.id is None or str(watch.movie.id) != movie_id:
                continue
            watch.locations = [l for l in watch.locations if str(l.id) != location_id]
        self.config.watches = [w for w in self.config.watches if w.locations]

        # Legacy single-pair configs have no `watches` to prune; clearing the
        # old fields is what actually removes the target for them.
        if not self.config.watches and self.config.legacy_target_key() == key:
            self.config.movie = MovieConfig()
            self.config.cinema = CinemaConfig()

        self.dates_by_target.pop(key, None)
        state = load_state()
        state.get("targets", {}).pop(key, None)
        save_state(state)

        await self._apply({"type": "targets_changed", "message": f"Stopped watching {key}"})
        return True

    async def add_recipient(self, chat_id: str) -> bool:
        """Add a Telegram recipient. False if already present."""
        chat_id = str(chat_id).strip()
        telegram = self.config.notifications.telegram
        if not chat_id or chat_id in telegram.recipients():
            return False

        if not telegram.chat_id:
            telegram.chat_id = chat_id
        else:
            telegram.chat_ids.append(chat_id)

        await self._apply({"type": "recipients_changed", "message": f"Added recipient {chat_id}"})
        return True

    async def remove_recipient(self, chat_id: str) -> bool:
        chat_id = str(chat_id).strip()
        telegram = self.config.notifications.telegram
        if chat_id not in telegram.recipients():
            return False

        if telegram.chat_id == chat_id:
            # Promote the next recipient so `chat_id` never sits empty while
            # others remain — env overrides and CI both key off it.
            telegram.chat_id = telegram.chat_ids.pop(0) if telegram.chat_ids else ""
        else:
            telegram.chat_ids = [c for c in telegram.chat_ids if c != chat_id]

        await self._apply({"type": "recipients_changed", "message": f"Removed recipient {chat_id}"})
        return True

    async def update_settings(self, interval: int | None = None,
                              desktop: bool | None = None,
                              telegram: bool | None = None) -> None:
        if interval is not None:
            self.config.monitoring.interval_seconds = max(int(interval), 30)
        if desktop is not None:
            self.config.notifications.desktop.enabled = bool(desktop)
        if telegram is not None:
            self.config.notifications.telegram.enabled = bool(telegram)

        await self._apply({"type": "settings_changed", "message": "Settings updated"})

    async def send_test_notification(self) -> int:
        """Fire a test alert. Returns the number of Telegram recipients tried."""
        from .notifier import Notifier

        targets = self.config.targets()
        watching = "\n".join(f"- {t.label}" for t in targets) or "- nothing configured yet"
        recipients = self.config.notifications.telegram.recipients()

        await Notifier.from_config(self.config).notify_all(
            message=(
                "This is a test from CineplexBD Ticket Watcher!\n"
                "If you see this, notifications are working.\n\n"
                f"Watching {len(targets)} pair(s):\n{watching}"
            ),
            title="Test Notification",
        )
        return len(recipients)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    async def broadcast(self, event: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def get_status(self) -> dict:
        targets = self.config.targets()
        first = targets[0] if targets else None
        return {
            "running": self.running,
            "targets": [
                {
                    "key": t.key,
                    "movie": t.movie_name,
                    "movie_id": t.movie_id,
                    "location": t.location_name,
                    "location_id": t.location_id,
                    "dates": self.dates_by_target.get(t.key, []),
                }
                for t in targets
            ],
            "settings": {
                "interval_seconds": self.config.monitoring.interval_seconds,
                "desktop_enabled": self.config.notifications.desktop.enabled,
                "telegram_enabled": self.config.notifications.telegram.enabled,
                # The token itself never leaves the server.
                "has_bot_token": bool(self.config.notifications.telegram.bot_token),
            },
            "recipients": self.config.notifications.telegram.recipients(),
            "movie": first.movie_name if first else "",
            "movie_id": first.movie_id if first else None,
            "location": first.location_name if first else "",
            "location_id": first.location_id if first else None,
            "interval": self.config.monitoring.interval_seconds,
            "dates": self.current_dates,
            "stats": self.stats,
            "log": self.activity_log[-50:],
        }

    async def close(self) -> None:
        if self.running:
            await self.stop()
        await self.api.close()


def format_sse(data: dict) -> bytes:
    return f"data: {json.dumps(data, default=str)}\n\n".encode()


# --- Route handlers ---

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(
        body=DASHBOARD_PATH.read_bytes(),
        content_type="text/html",
        charset="utf-8",
    )


async def handle_status(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    return web.json_response(hub.get_status())


async def handle_start(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    started = await hub.start()
    if started:
        return web.json_response({"ok": True, "message": "Monitor started"})
    return web.json_response({"ok": False, "message": "Already running"})


async def handle_stop(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    stopped = await hub.stop()
    if stopped:
        return web.json_response({"ok": True, "message": "Monitor stopped"})
    return web.json_response({"ok": False, "message": "Not running"})


async def handle_locations(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        locations = await hub.api.get_locations()
        return web.json_response({"ok": True, "locations": locations})
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)


async def handle_movies(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    location_id = int(request.query.get("location_id", "1"))
    try:
        data = await hub.api.get_movies(location_id)
        return web.json_response({"ok": True, **data})
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)


async def handle_config(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        location_id = int(body["location_id"])
        location_name = body["location_name"]
        movie_id = int(body["movie_id"])
        movie_name = body["movie_name"]

        await hub.update_config(location_id, location_name, movie_id, movie_name)
        return web.json_response({"ok": True, "message": "Config updated"})
    except (KeyError, ValueError) as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)


async def handle_add_target(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        added = await hub.add_target(
            movie_id=int(body["movie_id"]),
            movie_name=body.get("movie_name", ""),
            location_id=int(body["location_id"]),
            location_name=body.get("location_name", ""),
        )
    except (KeyError, ValueError, TypeError) as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)

    if not added:
        return web.json_response({"ok": False, "message": "Already watching that pair"})
    return web.json_response({"ok": True, "message": "Target added", **hub.get_status()})


async def handle_remove_target(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        removed = await hub.remove_target(str(body["key"]))
    except KeyError as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)

    if not removed:
        return web.json_response({"ok": False, "message": "No such target"}, status=404)
    return web.json_response({"ok": True, "message": "Target removed", **hub.get_status()})


async def handle_contacts(request: web.Request) -> web.Response:
    """Chats that have messaged the bot, so recipients can be picked not typed."""
    hub: WatcherHub = request.app["hub"]
    from .notifier import get_telegram_contacts

    try:
        contacts = await get_telegram_contacts(
            hub.config.notifications.telegram.bot_token
        )
    except ValueError as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=502)

    existing = set(hub.config.notifications.telegram.recipients())
    for contact in contacts:
        contact["already_added"] = contact["chat_id"] in existing
    return web.json_response({"ok": True, "contacts": contacts})


async def handle_add_recipient(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        added = await hub.add_recipient(str(body["chat_id"]))
    except KeyError as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)

    if not added:
        return web.json_response({"ok": False, "message": "Already a recipient"})
    return web.json_response({"ok": True, "message": "Recipient added", **hub.get_status()})


async def handle_remove_recipient(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        removed = await hub.remove_recipient(str(body["chat_id"]))
    except KeyError as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)

    if not removed:
        return web.json_response({"ok": False, "message": "Not a recipient"}, status=404)
    return web.json_response({"ok": True, "message": "Recipient removed", **hub.get_status()})


async def handle_settings(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        body = await request.json()
        await hub.update_settings(
            interval=body.get("interval_seconds"),
            desktop=body.get("desktop_enabled"),
            telegram=body.get("telegram_enabled"),
        )
    except (ValueError, TypeError) as e:
        return web.json_response({"ok": False, "message": f"Invalid data: {e}"}, status=400)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)

    return web.json_response({"ok": True, "message": "Settings saved", **hub.get_status()})


async def handle_test_notify(request: web.Request) -> web.Response:
    hub: WatcherHub = request.app["hub"]
    try:
        count = await hub.send_test_notification()
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=500)
    return web.json_response({
        "ok": True,
        "message": f"Test sent to {count} Telegram recipient(s) — check the log for failures",
    })


async def handle_health(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe for the external watchdog.

    Deliberately exposes no config: whether the loop is running, how many pairs
    it covers, and when it last completed a check — enough to alert on, nothing
    worth hiding.
    """
    hub: WatcherHub = request.app["hub"]
    state = load_state()
    return web.json_response({
        "ok": True,
        "running": hub.running,
        "targets": len(hub.config.targets()),
        "checks": hub.stats.get("checks", 0),
        "last_check": state.get("last_check"),
    })


async def handle_events(request: web.Request) -> web.StreamResponse:
    hub: WatcherHub = request.app["hub"]

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )
    await response.prepare(request)

    await response.write(format_sse({
        "type": "init",
        **hub.get_status(),
    }))

    queue = hub.subscribe()
    try:
        while True:
            event = await queue.get()
            await response.write(format_sse(event))
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(queue)

    return response


def create_app(hub: WatcherHub) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["hub"] = hub

    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/login", handle_login)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_get("/api/locations", handle_locations)
    app.router.add_get("/api/movies", handle_movies)
    app.router.add_post("/api/config", handle_config)
    app.router.add_post("/api/targets/add", handle_add_target)
    app.router.add_post("/api/targets/remove", handle_remove_target)
    app.router.add_get("/api/telegram/contacts", handle_contacts)
    app.router.add_post("/api/recipients/add", handle_add_recipient)
    app.router.add_post("/api/recipients/remove", handle_remove_recipient)
    app.router.add_post("/api/settings", handle_settings)
    app.router.add_post("/api/test-notify", handle_test_notify)

    return app
