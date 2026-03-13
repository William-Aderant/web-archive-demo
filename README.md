# Web Archive Demo

Compare Playwright's native HAR format with the professional WACZ/WARC archival
format for high-fidelity legal preservation (court rules, .gov sites).

## Architecture

```
capture.py          Playwright async → data/capture.har.zip
       │
process.py          HAR → WARC → WACZ  → data/capture.wacz
       │
replay_har.py       Headed browser replaying from HAR (no network)
viewer.html         ReplayWeb.page component viewing the WACZ
```

## Quick Start

```bash
# 1. Create a virtualenv (Python 3.12+)
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Capture a page (HAR archive)
python capture.py "https://www.uscourts.gov/rules-policies"

# 4. Convert to WACZ (archival "time capsule")
python process.py

# 5a. Replay from HAR in a headed browser
python replay_har.py

# 5b. Or view the WACZ in a browser
#     Serve the project directory and open viewer.html:
python -m http.server 8000
#     Then visit http://localhost:8000/viewer.html
```

## File Descriptions

| File | Purpose |
|---|---|
| `capture.py` | Async Playwright capture with infinite scroll, accordion/tab expansion, and anti-bot headers. Saves `data/capture.har.zip`. |
| `process.py` | Pipeline: extracts HAR from zip, converts to WARC via `har2warc`, then packages into WACZ via the `wacz` CLI. |
| `replay_har.py` | Launches a headed Chromium browser that serves every resource from the local HAR — zero live network traffic. |
| `viewer.html` | Standalone HTML page using the `<replay-web-page>` web component to browse the WACZ interactively. |

## Why Two Formats?

| | HAR (Playwright) | WACZ (Webrecorder) |
|---|---|---|
| **Created by** | Browser dev-tools layer | Archival community (IIPC) |
| **Content** | HTTP request/response pairs | Full WARC records + index + pages |
| **Replay** | Playwright `route_from_har` | ReplayWeb.page, pywb, any WARC reader |
| **Legal weight** | Lightweight proof-of-concept | Production-grade evidentiary format |
| **Interoperability** | Playwright-only | Any standards-compliant WARC tool |

## Anti-Bot / WAF Notes

Many `.gov` sites (including `uscourts.gov`) use aggressive CDN-level bot
detection (Akamai, Imperva, Cloudflare) that blocks headless browsers regardless
of headers or stealth flags. When this happens, `capture.py` will:

1. Log a clear warning: *"The page title suggests an anti-bot challenge"*
2. Still save the HAR (containing the block page) so the pipeline doesn't break

**Workarounds for protected sites:**

- Run in **headed mode** (`headless=False` in `capture.py`) from a residential IP
- Use a site that doesn't block automation for demo purposes (e.g. `https://example.com`)
- For production legal archiving, consider [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler) which generates WACZ directly

## Requirements

- Python 3.12+
- Chromium (installed via `playwright install chromium`)
- `pip install -r requirements.txt`
