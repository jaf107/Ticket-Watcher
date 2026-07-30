"""CineplexBD Ticket Watcher — CLI Entry Point.

Usage:
    python main.py discover [--visible]   Debug: discover API endpoints with browser
    python main.py list-locations         Show available cinema locations
    python main.py list-movies [LOC_ID]   Show movies at a location (default: 1)
    python main.py setup                  Interactive: pick location & movie
    python main.py watch                  Start monitoring for new dates
    python main.py dashboard [--port N]   Launch web dashboard
    python main.py test-notify            Test desktop + Telegram notifications
    python main.py status                 Show current config & state
"""

from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

# Load .env if present (before any other imports that read env vars)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from src.config_loader import load_config, save_config


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("watcher.log", encoding="utf-8"),
        ],
    )


async def cmd_discover(args) -> None:
    """Run API discovery with Playwright (for debugging)."""
    from src.discovery import discover_and_save

    print("Starting API discovery (debug mode)...")
    print("This opens a browser to capture API calls.\n")
    headless = not args.visible
    await discover_and_save(headless=headless)


async def cmd_list_locations(args) -> None:
    """List available cinema locations."""
    from src.api_client import CineplexAPI

    api = CineplexAPI()
    try:
        locations = await api.get_locations()
        if not locations:
            print("No locations found.")
            return

        print(f"\nCineplexBD Locations ({len(locations)}):\n")
        for loc in locations:
            title = loc.get("locationTitle") or loc.get("location_name", "Unknown")
            print(f"  [{loc['id']}] {title}")
            address = loc.get("address", "")
            if address:
                import re
                clean = re.sub(r"<[^>]+>", " ", address).strip()
                print(f"      {clean}")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await api.close()


async def cmd_list_movies(args) -> None:
    """List movies at a location."""
    from src.api_client import CineplexAPI

    api = CineplexAPI()
    try:
        loc_id = args.location_id or 1
        data = await api.get_movies(loc_id)

        running = data.get("running", [])
        upcoming = data.get("upcoming", [])

        if running:
            print(f"\nNow Showing ({len(running)}):\n")
            for m in running:
                print(f"  [{m['movie_id']}] {m['title']}")
                print(f"      {m.get('genre', '')} | {m.get('language', '')} | {m.get('category', '')}")
                print(f"      Cast: {m.get('actor', 'N/A')}")
                print()

        if upcoming:
            print(f"Coming Soon ({len(upcoming)}):\n")
            for m in upcoming:
                print(f"  [{m['movie_id']}] {m['title']}")
                print(f"      {m.get('genre', '')} | {m.get('language', '')} | Release: {m.get('release', 'TBA')}")
                print()

        if not running and not upcoming:
            print("No movies found for this location.")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await api.close()


def _prompt_selection(prompt: str, count: int) -> list[int]:
    """Ask for one or more 1-based indices, e.g. `1,3,5`. Returns 0-based."""
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            return []
        picks = []
        for part in raw.replace(" ", ",").split(","):
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                picks = []
                break
            if not 1 <= value <= count:
                picks = []
                break
            if value - 1 not in picks:
                picks.append(value - 1)
        if picks:
            return picks
        print(f"Invalid choice — enter numbers between 1 and {count}, separated by commas.")


async def cmd_setup(args) -> None:
    """Interactive setup: pick any number of movie/location pairs."""
    from src.api_client import CineplexAPI
    from src.config_loader import LocationRef, MovieConfig, WatchConfig

    config = load_config()
    api = CineplexAPI()

    try:
        print("Fetching locations...")
        locations = await api.get_locations()
        if not locations:
            print("No locations found.")
            return

        print(f"\nAvailable locations ({len(locations)}):\n")
        for i, loc in enumerate(locations, 1):
            title = loc.get("locationTitle") or loc.get("location_name", "Unknown")
            print(f"  {i:2d}. [{loc['id']}] {title}")

        picks = _prompt_selection(
            f"\nSelect location(s) 1-{len(locations)} (comma-separated): ", len(locations)
        )
        if not picks:
            print("Nothing selected.")
            return

        selected_locs = [locations[i] for i in picks]
        for loc in selected_locs:
            print(f"Selected: {loc.get('locationTitle') or loc.get('location_name', '')}")

        # Collect movies per location so a movie is only paired with the
        # locations that actually show it.
        print("\nFetching movies...")
        catalog: dict[int, dict] = {}
        for loc in selected_locs:
            data = await api.get_movies(loc["id"])
            for movie in [*data.get("running", []), *data.get("upcoming", [])]:
                entry = catalog.setdefault(
                    int(movie["movie_id"]),
                    {"title": movie["title"], "locations": [], "meta": movie},
                )
                entry["locations"].append(loc)

        if not catalog:
            print("No movies found at the selected location(s).")
            return

        movies = list(catalog.values())
        print(f"\nAvailable movies ({len(movies)}):\n")
        for i, entry in enumerate(movies, 1):
            meta = entry["meta"]
            where = f"{len(entry['locations'])}/{len(selected_locs)} selected location(s)"
            print(f"  {i:2d}. {entry['title']} — {where}")
            print(f"      {meta.get('genre', '')} | {meta.get('language', '')}")

        picks = _prompt_selection(
            f"\nSelect movie(s) 1-{len(movies)} (comma-separated): ", len(movies)
        )
        if not picks:
            print("Nothing selected.")
            return

        watches = []
        for index in picks:
            entry = movies[index]
            movie_id = int(entry["meta"]["movie_id"])
            watches.append(
                WatchConfig(
                    movie=MovieConfig(id=movie_id, name=entry["title"]),
                    locations=[
                        LocationRef(
                            id=int(loc["id"]),
                            name=loc.get("locationTitle") or loc.get("location_name", ""),
                        )
                        for loc in entry["locations"]
                    ],
                )
            )

        config.watches = watches
        targets = config.targets()

        print(f"\nWatching {len(targets)} pair(s). Checking current showtimes...")
        for target in targets:
            try:
                dates = await api.get_movie_dates(target.location_id, target.movie_id)
            except Exception as e:
                print(f"  {target.label}: could not check ({e})")
                continue
            if dates:
                print(f"  {target.label}: {', '.join(dates)}")
            else:
                print(f"  {target.label}: no dates yet — you'll be alerted when they appear")

        save_config(config)
        print("\nConfig saved to config.yaml!")
        print("Run 'python main.py watch' to start monitoring.")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await api.close()


async def cmd_watch(args) -> None:
    """Start the monitoring loop."""
    from src.monitor import run_monitor

    config = load_config()

    if not config.targets():
        print("No movie/location pairs configured yet.")
        print("Run 'python main.py setup' first.")
        return

    print("CineplexBD Ticket Watcher")
    print("=" * 40)
    summary = await run_monitor(config, run_once=args.once)

    # A run that checked nothing must not report success, or a scheduled job
    # goes on looking green for weeks while silently watching nothing.
    if summary["successes"] == 0:
        print(
            f"\nFAILED: no target was checked successfully "
            f"({summary['failures']} failure(s))."
        )
        sys.exit(1)


async def cmd_test_notify(args) -> None:
    """Send test notifications."""
    from src.notifier import Notifier

    config = load_config()
    notifier = Notifier.from_config(config)

    targets = config.targets()
    watching = "\n".join(f"- {t.label}" for t in targets) or "- nothing configured yet"
    recipients = config.notifications.telegram.recipients()

    print(f"Sending test notifications to {len(recipients)} Telegram recipient(s)...")
    delivered = await notifier.notify_all(
        message=(
            "This is a test from CineplexBD Ticket Watcher!\n"
            "If you see this, notifications are working.\n\n"
            f"Watching {len(targets)} pair(s):\n{watching}"
        ),
        title="Test Notification",
    )

    if not delivered:
        print("\nFAILED: Telegram delivery did not succeed for every recipient.")
        sys.exit(1)

    print("Done! Check your desktop and Telegram.")


async def cmd_dashboard(args) -> None:
    """Launch the web dashboard."""
    import webbrowser
    from aiohttp import web
    from src.web_server import WatcherHub, create_app

    from src.web_server import dashboard_password

    config = load_config()
    hub = WatcherHub(config)
    app = create_app(hub)

    port = int(os.environ.get("PORT") or args.port or 5096)
    host = args.host or os.environ.get("HOST") or "localhost"
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    url = f"http://localhost:{port}"
    print(f"Dashboard running on {host}:{port}")

    if host not in ("localhost", "127.0.0.1") and not dashboard_password():
        print("WARNING: bound to a public interface with no DASHBOARD_PASSWORD set.")
        print("         Anyone who reaches this port can edit your config.")

    print("Press Ctrl+C to stop.\n")

    # Hosted deployments have nobody to click Start, so let them watch on boot.
    if args.autostart or os.environ.get("AUTO_START", "").lower() in ("1", "true", "yes"):
        await hub.start()
        print("Monitor auto-started.")

    if not args.no_browser and host in ("localhost", "127.0.0.1"):
        webbrowser.open(url)

    stop = asyncio.Event()

    def handle_signal(*_):
        stop.set()
        print("\nShutting down...")

    signal.signal(signal.SIGINT, handle_signal)
    await stop.wait()

    if hub.running:
        await hub.stop()
    await runner.cleanup()


async def cmd_status(args) -> None:
    """Show current watcher status."""
    from src.monitor import load_state, migrate_state, target_dates

    config = load_config()
    targets = config.targets()
    recipients = config.notifications.telegram.recipients()

    print("CineplexBD Ticket Watcher — Status")
    print("=" * 40)
    print(f"Interval: {config.monitoring.interval_seconds}s")
    print(f"Desktop:  {'ON' if config.notifications.desktop.enabled else 'OFF'}")
    telegram_state = "ON" if config.notifications.telegram.enabled else "OFF"
    print(f"Telegram: {telegram_state} ({len(recipients)} recipient(s))")

    if not targets:
        print("\nNo movie/location pairs configured. Run 'python main.py setup'.")
        return

    if not Path("data/state.json").exists():
        print(f"\nWatching {len(targets)} pair(s) — no state yet (haven't run watch):")
        for target in targets:
            print(f"  - {target.label}")
        return

    state = migrate_state(load_state(), config.legacy_target_key())
    print(f"\nWatching {len(targets)} pair(s):")
    for target in targets:
        scheduled = target_dates(state, target, "scheduled")
        on_sale = set(target_dates(state, target, "on_sale"))
        print(f"\n  {target.label}")
        if not scheduled and not on_sale:
            print("    (no dates seen yet)")
            continue
        for d in sorted(set(scheduled) | on_sale):
            print(f"    - {d}{'  [ON SALE]' if d in on_sale else ''}")
    print(f"\nLast check: {state.get('last_check') or 'never'}")


def main():
    parser = argparse.ArgumentParser(
        description="CineplexBD Ticket Watcher Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  list-locations  Show cinema locations
  list-movies     Show movies at a location
  setup           Interactive: pick location & movie
  watch           Start monitoring for new dates
  test-notify     Test notifications (desktop + Telegram)
  status          Show current config and state
  discover        Debug: capture API calls with browser
        """,
    )
    sub = parser.add_subparsers(dest="command")

    p_discover = sub.add_parser("discover", help="Debug: discover API endpoints")
    p_discover.add_argument("--visible", action="store_true", help="Show browser window")

    sub.add_parser("list-locations", help="List cinema locations")

    p_movies = sub.add_parser("list-movies", help="List movies")
    p_movies.add_argument("location_id", nargs="?", type=int, default=None, help="Location ID")

    sub.add_parser("setup", help="Interactive setup")
    p_watch = sub.add_parser("watch", help="Start monitoring")
    p_watch.add_argument("--once", action="store_true", help="Run a single check and exit (for CI/cron)")

    p_dash = sub.add_parser("dashboard", help="Launch web dashboard")
    p_dash.add_argument("--port", type=int, default=5096, help="Port (default: 5096, or $PORT)")
    p_dash.add_argument("--host", default=None, help="Bind address (default: localhost, or $HOST)")
    p_dash.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    p_dash.add_argument("--autostart", action="store_true", help="Start watching immediately")

    sub.add_parser("test-notify", help="Test notifications")
    sub.add_parser("status", help="Show status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config()
    setup_logging(config.logging.level)

    commands = {
        "discover": cmd_discover,
        "list-locations": cmd_list_locations,
        "list-movies": cmd_list_movies,
        "setup": cmd_setup,
        "watch": cmd_watch,
        "dashboard": cmd_dashboard,
        "test-notify": cmd_test_notify,
        "status": cmd_status,
    }

    handler = commands.get(args.command)
    if handler:
        asyncio.run(handler(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
