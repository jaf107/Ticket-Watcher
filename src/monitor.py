"""Main monitoring loop — checks for new show dates and alerts."""

from __future__ import annotations
import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from .api_client import CineplexAPI, APIError
from .config_loader import Config, Target
from .notifier import Notifier

logger = logging.getLogger("watcher.monitor")

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_PATH = DATA_DIR / "state.json"
STATE_VERSION = 2


def load_state() -> dict:
    """Load persisted watcher state from disk."""
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load state, starting fresh: {e}")
    return empty_state()


def empty_state() -> dict:
    return {"version": STATE_VERSION, "targets": {}, "last_check": None}


def save_state(state: dict) -> None:
    """Persist watcher state to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def migrate_state(state: dict, fallback_key: str | None) -> dict:
    """Upgrade a pre-multi-target state file to the per-target layout.

    The old format held a single flat `previous_dates` list, which belonged to
    whatever movie+location the config pointed at. Filing it under that target's
    key preserves the baseline — dropping it would make every known date look
    new and fire a bogus alert on the next check.
    """
    if state.get("version") == STATE_VERSION and "targets" in state:
        return state

    old_dates = state.get("previous_dates") or state.get("known_dates") or []
    migrated = empty_state()
    migrated["last_check"] = state.get("last_check")

    if fallback_key and old_dates:
        migrated["targets"][fallback_key] = {
            "previous_dates": sorted(old_dates),
            "last_check": state.get("last_check"),
        }
        logger.info(f"Migrated {len(old_dates)} known dates to target {fallback_key}")

    return migrated


def target_dates(state: dict, target: Target) -> list[str]:
    """Dates already seen for one target."""
    return state.get("targets", {}).get(target.key, {}).get("previous_dates", [])


def set_target_dates(state: dict, target: Target, dates: list[str], when: str) -> None:
    state.setdefault("targets", {})[target.key] = {
        "previous_dates": sorted(dates),
        "last_check": when,
        "movie_id": target.movie_id,
        "location_id": target.location_id,
        "label": target.label,
    }


async def resolve_target_names(api: CineplexAPI, targets: list[Target]) -> None:
    """Fill in missing movie/location names so alerts stay readable.

    Targets built from env vars (the CI path) carry only IDs, which would make
    every notification read "Movie 1716 @ Location 3".
    """
    if all(t.movie_name and t.location_name for t in targets):
        return

    try:
        locations = {int(loc["id"]): (loc.get("locationTitle") or loc.get("location_name") or "")
                     for loc in await api.get_locations()}
    except Exception as e:
        logger.debug(f"Could not resolve location names: {e}")
        locations = {}

    movies: dict[int, str] = {}
    for location_id in {t.location_id for t in targets if not t.movie_name}:
        try:
            data = await api.get_movies(location_id)
        except Exception as e:
            logger.debug(f"Could not resolve movie names at location {location_id}: {e}")
            continue
        for movie in [*data.get("running", []), *data.get("upcoming", [])]:
            movies.setdefault(int(movie["movie_id"]), movie.get("title", ""))

    for target in targets:
        if not target.location_name:
            target.location_name = locations.get(target.location_id, "")
        if not target.movie_name:
            target.movie_name = movies.get(target.movie_id, "")


async def run_monitor(
    config: Config,
    on_event: Callable[[dict], Awaitable[None]] | None = None,
    stop_event: asyncio.Event | None = None,
    api: CineplexAPI | None = None,
    run_once: bool = False,
) -> None:
    """Main monitoring loop.

    Args:
        config: Watcher configuration.
        on_event: Optional async callback for each event (used by dashboard).
        stop_event: Optional event to signal shutdown (used by dashboard).
        api: Optional shared API instance (used by dashboard to avoid duplicate auth).
        run_once: Run a single check cycle and exit (for CI/cron).
    """
    targets = config.targets()

    if not targets:
        print("ERROR: No movie/location pairs configured.")
        print("Run 'python main.py setup' first.")
        return

    owns_api = api is None
    api = api or CineplexAPI()
    notifier = Notifier.from_config(config)

    # Once a config moves to `watches`, the legacy movie/cinema keys are gone
    # and there is nothing to match the old flat state against. A lone target
    # is unambiguous, so adopt the old baseline rather than rescanning.
    fallback_key = config.legacy_target_key()
    if fallback_key is None and len(targets) == 1:
        fallback_key = targets[0].key

    state = migrate_state(load_state(), fallback_key)
    save_state(state)

    _stop = stop_event or asyncio.Event()
    interval = max(config.monitoring.interval_seconds, 30)
    max_errors = config.monitoring.max_consecutive_errors
    consecutive_errors = 0
    check_count = 0

    async def emit(event: dict) -> None:
        """Emit event to callback and print to console."""
        if on_event:
            await on_event(event)

    # Only register signal handler if no external stop_event (CLI mode)
    if stop_event is None:
        def handle_signal(*_):
            _stop.set()
            print("\nShutting down gracefully...")
        signal.signal(signal.SIGINT, handle_signal)

    await resolve_target_names(api, targets)

    print(f"Watching {len(targets)} movie/location pair(s):")
    for target in targets:
        seen = len(target_dates(state, target))
        print(f"  - {target.label} ({seen} dates seen)")
    print(f"Interval: {interval}s")
    print("Press Ctrl+C to stop.\n")

    await emit({
        "type": "status",
        "running": True,
        "targets": [
            {
                "key": t.key,
                "movie": t.movie_name,
                "movie_id": t.movie_id,
                "location": t.location_name,
                "location_id": t.location_id,
                "dates": target_dates(state, t),
            }
            for t in targets
        ],
        # Legacy single-target fields, still read by the dashboard.
        "movie": targets[0].movie_name,
        "location": targets[0].location_name,
        "interval": interval,
        "previous_dates": target_dates(state, targets[0]),
    })

    start_time = datetime.now(timezone.utc)
    max_seconds = config.monitoring.max_duration_minutes * 60 if config.monitoring.max_duration_minutes > 0 else 0

    while not _stop.is_set():
        check_count += 1
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%H:%M:%S")

        if max_seconds > 0 and (now - start_time).total_seconds() >= max_seconds:
            print(f"[{timestamp}] Max duration reached. Stopping.")
            await emit({"type": "stopped", "reason": "Max duration reached"})
            break

        print(f"[{timestamp}] Check #{check_count}")
        cycle_had_success = False
        last_error: Exception | None = None

        for target in targets:
            try:
                current_dates = await api.get_movie_dates(target.location_id, target.movie_id)
                cycle_had_success = True
                await _handle_dates(
                    target, current_dates, state, notifier, emit,
                    check_count, timestamp, now, fallback=False,
                )
            except APIError as e:
                last_error = e
                print(f"  {target.label}: API error: {e}")
                await emit({
                    "type": "error",
                    "target": target.key,
                    "message": str(e),
                    "timestamp": timestamp,
                })
            except Exception as e:
                last_error = e
                print(f"  {target.label}: unexpected error: {e}")
                logger.exception(f"Unexpected error checking {target.label}")
                await emit({
                    "type": "error",
                    "target": target.key,
                    "message": str(e),
                    "timestamp": timestamp,
                })

        state["last_check"] = now.isoformat()
        save_state(state)

        if cycle_had_success:
            consecutive_errors = 0
        else:
            consecutive_errors += 1
            print(f"  All targets failed ({consecutive_errors}/{max_errors})")

            if consecutive_errors >= max_errors:
                recovered = await _try_browser_fallback(
                    config, targets, state, notifier, emit, check_count, timestamp, now,
                )
                if recovered:
                    consecutive_errors = 0
                else:
                    reason = f"Too many errors: {last_error}"
                    await notifier.notify_all(
                        f"Ticket watcher stopped after {consecutive_errors} failed checks.\n"
                        f"Last error: {last_error}"
                    )
                    await emit({"type": "stopped", "reason": reason})
                    break

        if not _stop.is_set():
            if run_once:
                break
            try:
                await asyncio.wait_for(_stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass  # Normal — interval elapsed, continue loop

    if owns_api:
        await api.close()
    save_state(state)
    await emit({"type": "stopped", "reason": "Shutdown"})
    print(f"\nStopped after {check_count} checks. State saved.")


async def _handle_dates(
    target: Target,
    current_dates: list[str],
    state: dict,
    notifier: Notifier,
    emit: Callable[[dict], Awaitable[None]],
    check_count: int,
    timestamp: str,
    now: datetime,
    fallback: bool,
) -> None:
    """Compare one target's dates against its baseline and alert on new ones."""
    previous = set(target_dates(state, target))
    current = set(current_dates)
    new_dates = sorted(current - previous)
    removed_dates = sorted(previous - current)

    event = {
        "count": check_count,
        "target": target.key,
        "movie": target.movie_name,
        "movie_id": target.movie_id,
        "location": target.location_name,
        "location_id": target.location_id,
        "dates": sorted(current),
        "new_dates": [],
        "removed_dates": removed_dates,
        "timestamp": timestamp,
    }
    if fallback:
        event["fallback"] = True

    if not previous:
        print(f"  {target.label}: initial scan, {len(current)} dates: {sorted(current)}")
        await emit({**event, "type": "check", "initial": True})
    elif new_dates:
        suffix = " (via fallback)" if fallback else ""
        msg = (
            f"New screening dates for {target.movie_name or target.movie_id}{suffix}!\n"
            f"New dates: {', '.join(new_dates)}\n"
            f"All available: {', '.join(sorted(current))}\n"
            f"Location: {target.location_name or target.location_id}"
        )
        print(f"  {target.label}: NEW DATES FOUND: {new_dates}")
        await notifier.notify_all(msg)
        await emit({**event, "type": "alert", "new_dates": new_dates, "message": msg})
    else:
        print(f"  {target.label}: no new dates ({len(current)} available)")
        await emit({**event, "type": "check"})

    if removed_dates:
        logger.info(f"{target.label}: dates no longer available: {removed_dates}")

    set_target_dates(state, target, sorted(current), now.isoformat())


async def _try_browser_fallback(
    config: Config,
    targets: list[Target],
    state: dict,
    notifier: Notifier,
    emit: Callable[[dict], Awaitable[None]],
    check_count: int,
    timestamp: str,
    now: datetime,
) -> bool:
    """Scrape dates with Playwright when the API keeps failing.

    The scraper extracts every date it can find on the site rather than
    querying a specific movie, so it can only stand in for a single target.
    With several configured there is no way to attribute what it finds.
    """
    if not config.monitoring.fallback_to_browser:
        return False

    if len(targets) > 1:
        print("  Browser fallback skipped: it cannot attribute dates across multiple targets.")
        logger.warning("Browser fallback unavailable with multiple targets configured")
        return False

    print("  Switching to browser fallback...")
    try:
        from .browser_fallback import browser_check_dates
        current_dates = await browser_check_dates(config, targets[0])
        await _handle_dates(
            targets[0], current_dates, state, notifier, emit,
            check_count, timestamp, now, fallback=True,
        )
        save_state(state)
        return True
    except Exception as e:
        print(f"  Browser fallback also failed: {e}")
        logger.warning(f"Browser fallback failed: {e}")
        return False
