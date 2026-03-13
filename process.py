"""
Convert a Playwright HAR archive into a WACZ "time capsule".

Pipeline: HAR (.har.zip) → WARC (.warc.gz) → WACZ (.wacz)

The WACZ format is the archival standard used by Webrecorder / ReplayWeb.page
and is suitable for evidentiary or legal preservation workflows.
"""

import argparse
import base64
import io
import json
import logging
import sys
import types
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

# ── Shim for pkg_resources (removed in Python 3.14) ──
if "pkg_resources" not in sys.modules:
    _shim = types.ModuleType("pkg_resources")

    class _Dist:
        def __init__(self, name: str):
            self.version = pkg_version(name)

    _shim.get_distribution = _Dist          # type: ignore[attr-defined]
    sys.modules["pkg_resources"] = _shim

from wacz.main import create_wacz                              # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DATA_DIR = Path("data")
HAR_PATH = DATA_DIR / "capture.har.zip"
WARC_PATH = DATA_DIR / "capture.warc.gz"
WACZ_PATH = DATA_DIR / "capture.wacz"


def _parse_har_zip(har_zip: Path) -> tuple[dict, zipfile.ZipFile]:
    """Open a Playwright .har.zip and return (parsed HAR dict, open ZipFile)."""
    zf = zipfile.ZipFile(har_zip, "r")
    har_names = [n for n in zf.namelist() if n.endswith(".har")]
    if not har_names:
        zf.close()
        raise FileNotFoundError(f"No .har file found inside {har_zip}")
    har = json.loads(zf.read(har_names[0]))
    return har, zf


def _har_timestamp(iso: str) -> str:
    """Convert a HAR ISO-8601 timestamp to WARC-Date format."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(iso, fmt)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def har_to_warc(har_zip: Path, warc_path: Path) -> Path:
    """
    Convert a Playwright HAR zip into a gzipped WARC with full response bodies.

    Playwright stores response payloads as separate files inside the zip
    (referenced via the ``_file`` key in each entry's response content).
    We read those directly and write proper WARC response records.
    """
    log.info("Converting HAR → WARC: %s → %s", har_zip, warc_path)

    har, zf = _parse_har_zip(har_zip)
    entries = har.get("log", {}).get("entries", [])

    with open(warc_path, "wb") as out, zf:
        writer = WARCWriter(out, gzip=True)

        for entry in entries:
            req = entry["request"]
            resp = entry["response"]
            url = req["url"]
            content = resp.get("content", {})
            mime = content.get("mimeType", "application/octet-stream")

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

            log.info(
                "  %s  %s (%s bytes)", status_code, url[:90], len(payload),
            )

    log.info("WARC written (%s bytes)", warc_path.stat().st_size)
    return warc_path


def warc_to_wacz(warc_path: Path, wacz_path: Path) -> Path:
    """Package a WARC into a WACZ archive by calling create_wacz directly."""
    log.info("Converting WARC → WACZ: %s → %s", warc_path, wacz_path)

    ns = argparse.Namespace(
        inputs=[str(warc_path)],
        output=str(wacz_path),
        file=True,
        pages=None,
        extra_pages=None,
        detect_pages=True,
        copy_pages=False,
        text=False,
        hash_type=None,
        log_directory=None,
        split_seeds=False,
        ts=None,
        url=None,
        date=None,
        title=None,
        desc=None,
        signing_url=None,
        signing_token=None,
    )

    rc = create_wacz(ns)
    if rc != 0:
        raise RuntimeError(f"create_wacz returned exit code {rc}")

    log.info("WACZ written (%s bytes)", wacz_path.stat().st_size)
    return wacz_path


def process(har_zip: Path | None = None) -> Path:
    """Run the full HAR → WARC → WACZ pipeline."""
    har_zip = har_zip or HAR_PATH
    if not har_zip.exists():
        raise FileNotFoundError(
            f"{har_zip} not found. Run capture.py first."
        )

    har_to_warc(har_zip, WARC_PATH)
    warc_to_wacz(WARC_PATH, WACZ_PATH)
    log.info("Pipeline complete → %s", WACZ_PATH)
    return WACZ_PATH


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a HAR archive to WACZ (Time Capsule) format.",
    )
    parser.add_argument(
        "--har",
        type=Path,
        default=HAR_PATH,
        help=f"Path to the HAR zip (default: {HAR_PATH})",
    )
    args = parser.parse_args()
    process(args.har)


if __name__ == "__main__":
    main()
