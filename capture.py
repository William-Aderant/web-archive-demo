"""
High-fidelity web capture using Playwright.

Navigates to a target URL, performs infinite scrolling and interactive element
expansion (accordions, tabs), then saves a full HAR archive to data/capture.har.zip.
Designed for legal/court-rules archiving where completeness matters.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeout,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
HAR_PATH = DATA_DIR / "capture.har.zip"

GOV_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}


async def infinite_scroll(page: Page, *, pause_ms: int = 800, max_rounds: int = 50) -> int:
    """Scroll to the bottom of the page repeatedly until no new content loads."""
    previous_height = 0
    rounds = 0

    while rounds < max_rounds:
        current_height = await page.evaluate("document.body.scrollHeight")
        if current_height == previous_height:
            break
        previous_height = current_height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)
        rounds += 1

    log.info("Scrolled %d round(s), final page height: %dpx", rounds, previous_height)
    return rounds


async def expand_interactive_elements(page: Page) -> int:
    """Find and click accordion / tab / collapsible elements to reveal hidden content."""
    selectors = [
        # ARIA-based accordions
        '[role="button"][aria-expanded="false"]',
        'button[aria-expanded="false"]',
        # Common accordion markup
        ".accordion-button.collapsed",
        ".accordion-header:not(.active)",
        "details:not([open]) > summary",
        # Tab interfaces
        '[role="tab"][aria-selected="false"]',
        ".nav-link:not(.active)",
        ".tab:not(.active)",
        # Generic collapsible toggles
        ".collapsible-toggle:not(.open)",
        ".expand-toggle",
    ]

    clicked = 0
    for selector in selectors:
        elements = await page.query_selector_all(selector)
        for el in elements:
            try:
                if await el.is_visible():
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    clicked += 1
                    await page.wait_for_timeout(300)
            except Exception as exc:
                log.debug("Skipping element (%s): %s", selector, exc)

    log.info("Expanded %d interactive element(s)", clicked)
    return clicked


async def capture(url: str) -> Path:
    """Launch a browser, navigate to *url*, interact, and save a HAR archive."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context: BrowserContext = await browser.new_context(
            record_har_path=str(HAR_PATH),
            record_har_mode="full",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            extra_http_headers=GOV_HEADERS,
        )

        page = await context.new_page()

        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        log.info("Navigating to %s", url)
        try:
            await page.goto(url, wait_until="networkidle", timeout=60_000)
        except PlaywrightTimeout:
            log.warning(
                "networkidle timed out — falling back to domcontentloaded"
            )
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        title = await page.title()
        log.info("Page loaded: %s", title)

        blocked_signals = ("access denied", "403 forbidden", "just a moment")
        if any(s in title.lower() for s in blocked_signals):
            log.warning(
                "The page title (%r) suggests an anti-bot challenge. "
                "The HAR will still be saved, but it may only contain the "
                "block page. Consider capturing from a non-headless browser "
                "or a different network.",
                title,
            )

        await infinite_scroll(page)
        await expand_interactive_elements(page)

        log.info("Waiting for final network idle…")
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeout:
            log.info("Final network idle timed out — proceeding with save")

        await context.close()
        await browser.close()

    log.info("HAR archive saved to %s", HAR_PATH)
    return HAR_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture a high-fidelity HAR archive of a web page.",
    )
    parser.add_argument("url", help="URL to capture")
    args = parser.parse_args()

    asyncio.run(capture(args.url))


if __name__ == "__main__":
    main()
