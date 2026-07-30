"""CineplexBD Ticket API client.

Uses the TICKET API (cineplex-ticket-api) for actual purchasable dates,
not the web API which only shows scheduled showtimes.

Auth flow:
1. Use Playwright to load ticket site and do guest login (needs reCAPTCHA)
2. Capture the Bearer token AND device-key from the request headers
3. Use those for direct HTTP calls to /get-showdate etc.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

import httpx

logger = logging.getLogger("watcher.api")

TICKET_API = "https://cineplex-ticket-api.cineplexbd.com/api/v1"
WEB_API = "https://cineplex-web-api.cineplexbd.com/api/v1"
AUTH_CACHE_PATH = Path(__file__).parent.parent / "data" / "auth_cache.json"
# Guest logins are the fragile part of this client — they need a real browser
# and clear a reCAPTCHA. Hold tokens longer and lean on the 401 handler in
# _ticket_post to refresh, rather than paying for a browser launch every hour.
AUTH_CACHE_TTL = 6 * 3600
AUTH_ATTEMPTS = 3


class APIError(Exception):
    pass


def _load_cached_auth() -> tuple[str, str] | None:
    """Try loading a cached auth token. Returns (token, device_key) or None."""
    try:
        if AUTH_CACHE_PATH.exists():
            cache = json.loads(AUTH_CACHE_PATH.read_text())
            if time.time() - cache["timestamp"] < AUTH_CACHE_TTL:
                logger.info("Using cached ticket API token")
                return cache["token"], cache["device_key"]
    except Exception:
        pass
    return None


def _save_cached_auth(token: str, device_key: str) -> None:
    """Cache the auth token to disk."""
    AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_CACHE_PATH.write_text(json.dumps({
        "token": token,
        "device_key": device_key,
        "timestamp": time.time(),
    }))


async def _get_ticket_auth_via_browser(attempts: int = AUTH_ATTEMPTS) -> tuple[str, str]:
    """Use Playwright to do guest login and capture token + device-key.

    The login rides on a reCAPTCHA round trip whose timing varies, so a single
    attempt fails intermittently — often enough to break a scheduled run while
    the very next one succeeds. Retry before giving up.

    Returns (token, device_key) tuple.
    """
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await _attempt_ticket_auth()
        except Exception as e:
            last_error = e
            logger.warning(f"Guest login attempt {attempt}/{attempts} failed: {e}")
            if attempt < attempts:
                await asyncio.sleep(3 * attempt)

    raise APIError(f"Failed to get ticket API token via guest login: {last_error}")


async def _attempt_ticket_auth() -> tuple[str, str]:
    """One guest-login run in a fresh browser."""
    from playwright.async_api import async_playwright

    token = None
    device_key = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        def capture_request(request):
            nonlocal device_key
            if "cineplex-ticket-api" in request.url and not device_key:
                dk = request.headers.get("device-key")
                if dk:
                    device_key = dk

        async def capture_response(response):
            nonlocal token
            if "guest-login" in response.url and response.status == 200:
                try:
                    data = await response.json()
                    if data.get("status") == "success":
                        token = data["data"]["token"]
                except Exception:
                    pass

        page.on("request", capture_request)
        page.on("response", lambda r: asyncio.ensure_future(capture_response(r)))

        # networkidle never settles on this page (polling/analytics), so wait for
        # the DOM only and then look for the guest-login button explicitly.
        await page.goto("https://ticket.cineplexbd.com/", wait_until="domcontentloaded", timeout=60000)

        # The button's exact casing has changed before; try a few spellings
        # rather than failing outright on a cosmetic tweak.
        for selector in ("text=GUEST LOGIN", "text=Guest Login", "button:has-text('GUEST')"):
            try:
                guest_btn = await page.wait_for_selector(selector, timeout=15000)
            except Exception:
                continue
            if guest_btn:
                await guest_btn.click()
                break

        # The token may also arrive from an automatic guest-login on page load,
        # so poll regardless of whether a button was found or clicked.
        for _ in range(60):
            if token:
                break
            await page.wait_for_timeout(500)

        await browser.close()

    if not token:
        raise APIError("no token captured from guest-login response")

    # If we didn't capture device-key, generate one (SHA-256 hash)
    if not device_key:
        device_key = hashlib.sha256(secrets.token_bytes(32)).hexdigest()

    return token, device_key


class CineplexAPI:
    """CineplexBD API client using the ticket API for real availability."""

    def __init__(self):
        self.ticket_token: str | None = None
        self.device_key: str | None = None
        self.web_token: str | None = None
        self.client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )

    async def login(self, force: bool = False) -> str:
        """Get ticket API token, using cache if available."""
        if not force:
            cached = _load_cached_auth()
            if cached:
                self.ticket_token, self.device_key = cached
                return self.ticket_token

        print("[Auth] Getting ticket API token (browser guest login)...")
        self.ticket_token, self.device_key = await _get_ticket_auth_via_browser()
        _save_cached_auth(self.ticket_token, self.device_key)
        print("[Auth] Token acquired and cached!")
        return self.ticket_token

    async def ensure_auth(self) -> None:
        """Authenticate if needed. Call once per cycle to avoid one browser
        launch per target when the token is missing or expired."""
        if not self.ticket_token:
            await self.login()

    async def _ensure_auth(self) -> None:
        await self.ensure_auth()

    async def _ticket_post(self, endpoint: str, body: dict | None = None) -> dict | list:
        """Make an authenticated POST to the ticket API."""
        await self._ensure_auth()
        headers = {
            "Authorization": f"Bearer {self.ticket_token}",
            "appsource": "web",
            "device-key": self.device_key or "",
            "application": "application/json",
            "Origin": "https://ticket.cineplexbd.com",
            "Referer": "https://ticket.cineplexbd.com/",
        }
        resp = await self.client.post(
            f"{TICKET_API}/{endpoint}", json=body or {}, headers=headers
        )
        data = resp.json()

        if data.get("code") == 401:
            logger.info("Ticket token expired, re-authenticating...")
            await self.login(force=True)
            headers["Authorization"] = f"Bearer {self.ticket_token}"
            headers["device-key"] = self.device_key or ""
            resp = await self.client.post(
                f"{TICKET_API}/{endpoint}", json=body or {}, headers=headers
            )
            data = resp.json()

        if data.get("status") == "error":
            raise APIError(f"Ticket API /{endpoint}: {data.get('message')}")

        return data.get("data", data)

    async def _web_post(self, endpoint: str, body: dict | None = None) -> dict | list:
        """Make a POST to the web API (auto-login, no reCAPTCHA needed)."""
        import uuid
        from datetime import datetime

        if not self.web_token:
            user_id = uuid.uuid4().hex[:20] + str(int(datetime.now().timestamp() * 1000))
            headers = {
                "Origin": "https://www.cineplexbd.com",
                "Referer": "https://www.cineplexbd.com/",
            }
            resp = await self.client.post(
                f"{WEB_API}/login", json={"user_id": user_id}, headers=headers
            )
            data = resp.json()
            if data.get("status") == "success":
                self.web_token = data["data"]

        headers = {
            "Origin": "https://www.cineplexbd.com",
            "Referer": "https://www.cineplexbd.com/",
        }
        if self.web_token:
            headers["Authorization"] = f"Bearer {self.web_token}"

        resp = await self.client.post(
            f"{WEB_API}/{endpoint}", json=body or {}, headers=headers
        )
        data = resp.json()
        return data.get("data", data)

    # --- Ticket API methods (actual purchasable dates) ---

    async def get_locations(self) -> list[dict]:
        """Fetch locations from ticket API.

        Returns: [{id, code, locationTitle, address, totalScreen, district}, ...]
        """
        data = await self._ticket_post("get-location")
        return data if isinstance(data, list) else []

    async def get_showdates(self, location_id: int) -> list[dict]:
        """Fetch available show dates with purchasable tickets.

        Returns: [{locID, showDate, availableMovies: [{movie_id, movie_title, ...}]}, ...]
        """
        data = await self._ticket_post("get-showdate", {"location": location_id})
        return data if isinstance(data, list) else []

    async def get_movie_dates(self, location_id: int, movie_id: int) -> list[str]:
        """Get purchasable dates for a specific movie at a location.

        Returns list of date strings like ["2026-03-28", "2026-03-29"].
        """
        showdates = await self.get_showdates(location_id)
        dates = []
        for entry in showdates:
            for movie in entry.get("availableMovies", []):
                if movie.get("movie_id") == movie_id:
                    dates.append(entry["showDate"])
                    break
        return sorted(dates)

    # --- Web API methods (for browsing/setup) ---

    async def get_movies(self, location_id: int = 1) -> dict:
        """Fetch movie list from web API (running + upcoming).

        Returns: {running: [{id, movie_id, title, ...}], upcoming: [...]}
        """
        data = await self._web_post("movie-list", {"location": location_id})
        if isinstance(data, dict):
            return data
        return {"running": [], "upcoming": []}

    async def close(self) -> None:
        await self.client.aclose()
