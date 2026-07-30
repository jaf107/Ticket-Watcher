"""Web dashboard server — aiohttp + SSE for real-time monitoring."""

from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .api_client import CineplexAPI
from .config_loader import (
    Config, LocationRef, MovieConfig, WatchConfig, load_config, save_config,
)
from .monitor import run_monitor, load_state, save_state, empty_state, target_dates

logger = logging.getLogger("watcher.web")

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


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

        # Load initial state from disk. The dashboard shows one target at a
        # time, so it displays the first configured pair.
        state = load_state()
        targets = config.targets()
        self.current_dates = target_dates(state, targets[0]) if targets else []

    async def on_event(self, event: dict) -> None:
        """Callback from the monitor loop — broadcast to all SSE clients."""
        etype = event.get("type")

        if etype == "check" or etype == "alert":
            self.stats["checks"] = event.get("count", self.stats["checks"])
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
                }
                for t in targets
            ],
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
    app = web.Application()
    app["hub"] = hub

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/events", handle_events)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_get("/api/locations", handle_locations)
    app.router.add_get("/api/movies", handle_movies)
    app.router.add_post("/api/config", handle_config)

    return app
