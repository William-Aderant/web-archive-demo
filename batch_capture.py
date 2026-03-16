"""
Batch capture: sample URLs from a CSV, capture each with Playwright,
and produce a single combined WARC file containing all pages.
"""

import asyncio
import base64
import csv
import io
import json
import logging
import random
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
COMBINED_WARC = DATA_DIR / "batch.warc.gz"
PAGE_TIMEOUT = 30_000
DEFAULT_SAMPLE = 30

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


def sample_urls(csv_path: Path, n: int = DEFAULT_SAMPLE) -> list[str]:
    """Read the CSV and return n random page URLs."""
    urls = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            url = row.get("Page URL", "").strip()
            if url and url.startswith("http"):
                urls.append(url)

    if len(urls) <= n:
        return urls

    return random.sample(urls, n)


def _har_timestamp(iso: str) -> str:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(iso, fmt)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_har_to_warc(har_zip: Path, writer: WARCWriter) -> int:
    """Read a Playwright HAR zip and write its entries to an open WARCWriter."""
    count = 0
    with zipfile.ZipFile(har_zip, "r") as zf:
        har_names = [n for n in zf.namelist() if n.endswith(".har")]
        if not har_names:
            return 0
        har = json.loads(zf.read(har_names[0]))

        for entry in har.get("log", {}).get("entries", []):
            url = entry["request"]["url"]
            resp = entry["response"]
            content = resp.get("content", {})

            payload = b""
            ref_file = content.get("_file", "")
            if ref_file and ref_file in zf.namelist():
                payload = zf.read(ref_file)
            elif content.get("text"):
                text = content["text"]
                if content.get("encoding") == "base64":
                    payload = base64.b64decode(text)
                else:
                    payload = text.encode("utf-8")

            status_code = str(resp["status"])
            status_text = resp.get("statusText", "")
            status_line = f"{status_code} {status_text}".strip()

            resp_headers = [
                (h["name"], h["value"])
                for h in resp.get("headers", [])
                if not h["name"].startswith(":")
            ]

            warc_date = _har_timestamp(entry.get("startedDateTime", ""))

            http_headers = StatusAndHeaders(
                status_line, resp_headers, protocol="HTTP/1.1",
            )

            record = writer.create_warc_record(
                uri=url,
                record_type="response",
                payload=io.BytesIO(payload),
                length=len(payload),
                http_headers=http_headers,
                warc_headers_dict={"WARC-Date": warc_date},
            )
            writer.write_record(record)
            count += 1

    return count


async def capture_one(url: str, har_path: Path, browser) -> bool:
    """Capture a single URL to a HAR file. Returns True on success."""
    try:
        context = await browser.new_context(
            record_har_path=str(har_path),
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

        try:
            await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        except PlaywrightTimeout:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            except PlaywrightTimeout:
                log.warning("  SKIP %s — timed out", url)
                await context.close()
                return False

        title = await page.title()
        blocked = ("access denied", "403 forbidden", "just a moment")
        if any(s in title.lower() for s in blocked):
            log.warning("  BLOCKED %s — %s", url, title)

        await context.close()
        return True

    except Exception as exc:
        log.warning("  ERROR %s — %s", url, exc)
        return False


async def batch_capture(csv_path: Path, n: int = DEFAULT_SAMPLE) -> Path:
    """Sample URLs, capture each, combine into a single WARC."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    urls = sample_urls(csv_path, n)
    log.info("Sampled %d URLs from %s", len(urls), csv_path)

    tmp_dir = DATA_DIR / "tmp_hars"
    tmp_dir.mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        results = []
        for i, url in enumerate(urls, 1):
            har_path = tmp_dir / f"{i:03d}.har.zip"
            log.info("[%d/%d] Capturing %s", i, len(urls), url)
            ok = await capture_one(url, har_path, browser)
            if ok and har_path.exists():
                results.append((url, har_path))

        await browser.close()

    log.info("Captured %d/%d URLs successfully", len(results), len(urls))

    total_records = 0
    with open(COMBINED_WARC, "wb") as out:
        writer = WARCWriter(out, gzip=True)
        for url, har_path in results:
            n = append_har_to_warc(har_path, writer)
            total_records += n
            log.info("  %s → %d WARC records", url[:70], n)

    log.info("Combined WARC: %s (%d records)", COMBINED_WARC, total_records)

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return COMBINED_WARC


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Batch capture URLs from a CSV into a combined WARC.",
    )
    parser.add_argument(
        "csv", type=Path,
        help="Path to Versionista CSV with 'Page URL' column",
    )
    parser.add_argument(
        "-n", "--sample-size", type=int, default=DEFAULT_SAMPLE,
        help=f"Number of URLs to sample (default: {DEFAULT_SAMPLE})",
    )
    args = parser.parse_args()

    asyncio.run(batch_capture(args.csv, args.sample_size))


if __name__ == "__main__":
    main()
