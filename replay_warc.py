"""
Replay a WARC archive in a headed Playwright browser.

Parses all response records from the WARC, builds a URL→response lookup,
and intercepts every browser request to serve content from the archive.
"""

import argparse
import asyncio
import gzip
import logging
from pathlib import Path

from playwright.async_api import async_playwright, Route
from warcio.archiveiterator import ArchiveIterator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

WARC_PATH = Path("data/capture.warc.gz")


def _load_warc_index(warc_path: Path) -> dict[str, dict]:
    """
    Parse the WARC and build a dict mapping URL → response metadata.

    Returns {url: {"status": int, "headers": dict, "body": bytes, "mime": str}}
    """
    index: dict[str, dict] = {}

    with open(warc_path, "rb") as fh:
        for record in ArchiveIterator(fh):
            if record.rec_type != "response":
                continue

            url = record.rec_headers.get_header("WARC-Target-URI")
            if not url:
                continue

            http_headers = record.http_headers
            status = int(http_headers.get_statuscode()) if http_headers else 200

            headers = {}
            content_encoding = ""
            if http_headers:
                for name, value in http_headers.headers:
                    lower = name.lower()
                    if lower == "content-encoding":
                        content_encoding = value.lower()
                        continue
                    if lower == "content-length":
                        continue
                    headers[name] = value

            body = record.content_stream().read()

            if content_encoding == "gzip" and body:
                try:
                    body = gzip.decompress(body)
                except Exception:
                    pass
            elif content_encoding == "br" and body:
                try:
                    import brotli
                    body = brotli.decompress(body)
                except Exception:
                    pass

            mime = headers.get("Content-Type",
                               headers.get("content-type",
                                           "application/octet-stream"))

            index[url] = {
                "status": status,
                "headers": headers,
                "body": body,
                "mime": mime.split(";")[0].strip(),
            }

    return index


async def replay(warc_path: Path) -> None:
    """Open a headed browser and route all traffic through the WARC archive."""
    if not warc_path.exists():
        raise FileNotFoundError(
            f"{warc_path} not found. Run process.py first."
        )

    log.info("Loading WARC index from %s", warc_path)
    index = _load_warc_index(warc_path)
    log.info("Indexed %d response(s) from WARC", len(index))

    if not index:
        raise ValueError("WARC contains no response records")

    first_url = next(iter(index))
    request_count = 0

    async def intercept(route: Route) -> None:
        nonlocal request_count
        url = route.request.url

        entry = index.get(url)
        if entry:
            request_count += 1
            log.info(
                "  [WARC] #%d  %s %s (%s bytes)",
                request_count, route.request.method, url[:90], len(entry["body"]),
            )
            await route.fulfill(
                status=entry["status"],
                headers=entry["headers"],
                body=entry["body"],
                content_type=entry["mime"],
            )
        else:
            log.warning("  [MISS] %s — not in WARC, aborting", url[:90])
            await route.abort()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )

        await context.route("**/*", intercept)

        page = await context.new_page()

        log.info("Replaying %s from WARC archive", first_url)
        await page.goto(first_url, wait_until="domcontentloaded", timeout=30_000)
        log.info("Page title: %s", await page.title())
        log.info(
            "Total requests served from WARC (not network): %d", request_count
        )

        await page.evaluate("""() => {
            const banner = document.createElement('div');
            banner.innerHTML = `
                <div style="display:flex; align-items:center; gap:12px;">
                    <span style="font-size:24px;">&#x1f3db;&#xfe0f;</span>
                    <div>
                        <strong>WARC REPLAY &mdash; OFFLINE ARCHIVE</strong><br>
                        <span style="font-size:13px; opacity:0.9;">
                            All content served from local WARC file &bull; Zero live network requests
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
                background: 'linear-gradient(135deg, #1d4ed8, #1e3a5f)',
                color: 'white',
                padding: '12px 24px',
                fontFamily: 'system-ui, -apple-system, sans-serif',
                fontSize: '15px',
                boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
                borderBottom: '3px solid #60a5fa',
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a WARC archive in a headed browser (no live network).",
    )
    parser.add_argument(
        "--warc",
        type=Path,
        default=WARC_PATH,
        help=f"Path to WARC archive (default: {WARC_PATH})",
    )
    args = parser.parse_args()

    asyncio.run(replay(args.warc))


if __name__ == "__main__":
    main()
