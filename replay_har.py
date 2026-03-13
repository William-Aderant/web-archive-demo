"""
Replay a previously captured HAR archive in a headed Playwright browser.

Loads the site entirely from the local HAR file — no live network requests are
made.  This demonstrates how Playwright can act as a "ghost" of the live web,
serving every resource from the archive.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

HAR_PATH = Path("data/capture.har.zip")


async def replay(har_path: Path, url: str | None = None) -> None:
    """Open a headed browser and route all traffic through the local HAR."""
    if not har_path.exists():
        raise FileNotFoundError(
            f"{har_path} not found. Run capture.py first."
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )

        request_count = 0

        def on_request(request):
            nonlocal request_count
            request_count += 1
            log.info(
                "  [HAR] #%d  %s %s",
                request_count, request.method, request.url,
            )

        await context.route_from_har(str(har_path), update=False)

        page = await context.new_page()
        page.on("request", on_request)

        if url:
            target = url
        else:
            target = _first_url_from_har(har_path)

        log.info("Replaying %s from HAR archive", target)
        await page.goto(target, wait_until="domcontentloaded", timeout=30_000)
        log.info("Page title: %s", await page.title())
        log.info(
            "Total requests served from HAR (not network): %d", request_count
        )

        await page.evaluate("""() => {
            const banner = document.createElement('div');
            banner.innerHTML = `
                <div style="display:flex; align-items:center; gap:12px;">
                    <span style="font-size:24px;">&#x1f4fc;</span>
                    <div>
                        <strong>HAR REPLAY &mdash; OFFLINE ARCHIVE</strong><br>
                        <span style="font-size:13px; opacity:0.9;">
                            All content served from local HAR file &bull; Zero live network requests
                        </span>
                    </div>
                </div>
            `;
            Object.assign(banner.style, {
                position: 'fixed',
                top: '0',
                left: '0',
                right: '0',
                zIndex: '2147483647',
                background: 'linear-gradient(135deg, #dc2626, #991b1b)',
                color: 'white',
                padding: '12px 24px',
                fontFamily: 'system-ui, -apple-system, sans-serif',
                fontSize: '15px',
                boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
                borderBottom: '3px solid #fbbf24',
                textAlign: 'center',
                display: 'flex',
                justifyContent: 'center',
            });
            document.body.style.marginTop = '64px';
            document.body.prepend(banner);
        }""")

        log.info("Browser is open — close the window to exit.")
        await page.wait_for_event("close", timeout=0)

        await context.close()
        await browser.close()


def _first_url_from_har(har_path: Path) -> str:
    """Extract the first navigated URL from a HAR (zip or plain) archive."""
    import json
    import zipfile

    if zipfile.is_zipfile(har_path):
        with zipfile.ZipFile(har_path, "r") as zf:
            har_names = [n for n in zf.namelist() if n.endswith(".har")]
            if not har_names:
                raise FileNotFoundError("No .har inside zip")
            raw = zf.read(har_names[0])
            har = json.loads(raw)
    else:
        har = json.loads(har_path.read_text())

    entries = har.get("log", {}).get("entries", [])
    if not entries:
        raise ValueError("HAR file contains no entries")
    return entries[0]["request"]["url"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a HAR archive in a headed browser (no live network).",
    )
    parser.add_argument(
        "--har",
        type=Path,
        default=HAR_PATH,
        help=f"Path to HAR archive (default: {HAR_PATH})",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Override the URL to navigate to (default: first URL in HAR)",
    )
    args = parser.parse_args()

    asyncio.run(replay(args.har, args.url))


if __name__ == "__main__":
    main()
