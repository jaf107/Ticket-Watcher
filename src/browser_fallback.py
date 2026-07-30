"""Playwright-based fallback for when direct API calls fail."""

from __future__ import annotations
import asyncio
import logging

from playwright.async_api import async_playwright

from .config_loader import Config, Target

logger = logging.getLogger("watcher.fallback")


async def browser_check_dates(config: Config, target: Target | None = None) -> list[str]:
    """Use Playwright to check for available show dates.

    This loads the actual SPA in a headless browser and intercepts
    the API responses to extract date information. Used as a fallback
    when direct API calls fail (e.g., due to reCAPTCHA or changed endpoints).

    `target` selects which location to steer the page towards; without one the
    legacy top-level cinema config is used.
    """
    location_name = target.location_name if target else config.cinema.location
    dates: list[str] = []
    api_responses: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        # Capture API responses for date-related data
        async def capture_response(response):
            url = response.url
            if "cineplex" not in url or "api" not in url:
                return
            try:
                body = await response.json()
                api_responses.append({"url": url, "body": body})
            except Exception:
                pass

        page.on("response", lambda r: asyncio.ensure_future(capture_response(r)))

        # Navigate to showtime page
        logger.info("Browser fallback: loading showtime page...")
        try:
            await page.goto(
                "https://www.cineplexbd.com/show-time",
                wait_until="networkidle",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"Could not load show-time page: {e}")
            # Try alternative route
            await page.goto(
                "https://www.cineplexbd.com/movie-show-time",
                wait_until="networkidle",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)

        # Try to interact with the page to trigger date loading
        # Look for location/cinema/movie selectors and click them
        try:
            # Try selecting location if there's a dropdown
            selectors = await page.query_selector_all("select, [role='listbox'], .dropdown")
            for sel in selectors:
                text = await sel.inner_text()
                if location_name and location_name.lower() in text.lower():
                    await sel.click()
                    await page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"Could not interact with selectors: {e}")

        # Also try the ticket site
        try:
            await page.goto(
                "https://ticket.cineplexbd.com/",
                wait_until="networkidle",
                timeout=30000,
            )
            await page.wait_for_timeout(3000)
        except Exception as e:
            logger.warning(f"Could not load ticket site: {e}")

        await browser.close()

    # Extract dates from captured API responses
    for resp in api_responses:
        body = resp["body"]
        dates.extend(_extract_dates_from_response(body))

    # Deduplicate and sort
    dates = sorted(set(dates))
    logger.info(f"Browser fallback found {len(dates)} dates")
    return dates


def _extract_dates_from_response(data, depth: int = 0) -> list[str]:
    """Recursively extract date strings from an API response."""
    if depth > 5:
        return []

    dates = []

    if isinstance(data, dict):
        for key, value in data.items():
            key_lower = key.lower()
            if "date" in key_lower and isinstance(value, str) and len(value) >= 8:
                dates.append(value)
            elif isinstance(value, (dict, list)):
                dates.extend(_extract_dates_from_response(value, depth + 1))

    elif isinstance(data, list):
        for item in data:
            dates.extend(_extract_dates_from_response(item, depth + 1))

    return dates
