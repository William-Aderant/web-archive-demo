"""
Flask web server that replays a Playwright HAR archive in a browser viewer.

Parses data/capture.har.zip at startup, builds an in-memory index keyed by
URL path+query, and serves archived resources at their original paths.
The viewer page lives at /_viewer to stay out of the way.
"""

import base64
import gzip
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, request as flask_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

HAR_PATH = Path("data/capture.har.zip")

app = Flask(__name__)


# ── HAR index ────────────────────────────────────────────────────────────────

class HarIndex:
    """In-memory index built from a Playwright HAR zip."""

    def __init__(self, har_zip: Path):
        self.entries: dict[str, dict] = {}
        self.path_index: dict[str, dict] = {}
        self.first_url: str = ""
        self.first_path: str = ""
        self.origin: str = ""
        self.capture_ts: str = ""
        self.total_size: int = 0
        self._load(har_zip)

    def _load(self, har_zip: Path) -> None:
        with zipfile.ZipFile(har_zip, "r") as zf:
            har_names = [n for n in zf.namelist() if n.endswith(".har")]
            if not har_names:
                raise FileNotFoundError(f"No .har inside {har_zip}")
            har = json.loads(zf.read(har_names[0]))

            for i, entry in enumerate(har["log"]["entries"]):
                url = entry["request"]["url"]
                resp = entry["response"]
                content = resp.get("content", {})
                mime = content.get("mimeType", "application/octet-stream")

                body = b""
                ref_file = content.get("_file", "")
                if ref_file and ref_file in zf.namelist():
                    body = zf.read(ref_file)
                elif content.get("text"):
                    text = content["text"]
                    if content.get("encoding") == "base64":
                        body = base64.b64decode(text)
                    else:
                        body = text.encode("utf-8")

                encoding = ""
                headers = {}
                for h in resp.get("headers", []):
                    name_lower = h["name"].lower()
                    if name_lower == "content-encoding":
                        encoding = h["value"].lower()
                        continue
                    if name_lower in ("content-length", "content-security-policy",
                                      "x-frame-options", "transfer-encoding",
                                      "etag", "last-modified"):
                        continue
                    headers[h["name"]] = h["value"]

                if encoding == "gzip" and body:
                    try:
                        body = gzip.decompress(body)
                    except Exception:
                        pass

                self.total_size += len(body)

                parsed = urlparse(url)
                path_key = parsed.path
                if parsed.query:
                    path_key += "?" + parsed.query

                record = {
                    "status": resp["status"],
                    "headers": headers,
                    "body": body,
                    "mime": mime.split(";")[0].strip(),
                    "url": url,
                }

                self.entries[url] = record
                self.path_index[path_key] = record

                if i == 0:
                    self.first_url = url
                    self.first_path = parsed.path
                    self.origin = f"{parsed.scheme}://{parsed.netloc}"
                    self.capture_ts = entry.get("startedDateTime", "")

        log.info("Loaded %d entries from %s", len(self.entries), har_zip)


har_index: HarIndex | None = None


def get_index() -> HarIndex:
    global har_index
    if har_index is None:
        har_index = HarIndex(HAR_PATH)
    return har_index


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/_viewer")
def viewer():
    idx = get_index()

    capture_date = ""
    if idx.capture_ts:
        try:
            dt = datetime.fromisoformat(idx.capture_ts.replace("Z", "+00:00"))
            capture_date = dt.strftime("%b %d, %Y at %H:%M:%S UTC")
        except ValueError:
            capture_date = idx.capture_ts

    return VIEWER_HTML.format(
        archived_url=idx.first_url,
        first_path=idx.first_path,
        capture_date=capture_date,
        resource_count=len(idx.entries),
        total_kb=idx.total_size // 1024,
    )


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_archived(path: str):
    """Serve archived content at the original URL path."""
    idx = get_index()

    lookup = "/" + path
    query = flask_request.query_string.decode("utf-8")
    if query:
        lookup += "?" + query

    entry = idx.path_index.get(lookup)
    if not entry:
        if not path:
            return viewer()
        log.warning("[MISS] %s", lookup)
        abort(404)

    body = entry["body"]
    mime = entry["mime"]

    rewritable = ("text/html", "text/css", "application/javascript",
                  "text/javascript")
    if any(t in mime for t in rewritable) and body:
        text = body.decode("utf-8", errors="replace")
        text = text.replace(idx.origin, "")
        body = text.encode("utf-8")

    log.info("[SERVE] %s (%d bytes)", entry["url"], len(body))

    resp_headers = dict(entry["headers"])
    resp_headers["Cache-Control"] = "no-store"

    return Response(
        body,
        status=entry["status"],
        content_type=mime,
        headers=resp_headers,
    )


@app.route("/_api/meta")
def meta():
    idx = get_index()
    return jsonify({
        "url": idx.first_url,
        "origin": idx.origin,
        "capture_ts": idx.capture_ts,
        "resource_count": len(idx.entries),
        "total_bytes": idx.total_size,
    })


# ── Viewer HTML template ────────────────────────────────────────────────────

VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>HAR Archive Viewer</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ height: 100%; font-family: system-ui, -apple-system, sans-serif; }}
    body {{
      display: flex;
      flex-direction: column;
      background: #0f172a;
      color: #e2e8f0;
    }}

    .toolbar {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 16px;
      background: #1e293b;
      border-bottom: 1px solid #334155;
      flex-shrink: 0;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: linear-gradient(135deg, #dc2626, #991b1b);
      color: #fff;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 5px 10px;
      border-radius: 4px;
      white-space: nowrap;
      border: 1px solid rgba(255,255,255,0.15);
    }}
    .badge .dot {{
      width: 7px; height: 7px;
      background: #fbbf24;
      border-radius: 50%;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.4; }}
    }}

    .url-bar {{
      flex: 1;
      display: flex;
      align-items: center;
      background: #0f172a;
      border: 1px solid #475569;
      border-radius: 6px;
      padding: 0 12px;
      height: 34px;
      overflow: hidden;
    }}
    .url-bar .lock {{
      color: #94a3b8;
      margin-right: 8px;
      font-size: 13px;
    }}
    .url-bar input {{
      flex: 1;
      background: none;
      border: none;
      color: #cbd5e1;
      font-family: 'SF Mono', Menlo, Consolas, monospace;
      font-size: 13px;
      outline: none;
      cursor: default;
    }}

    .meta {{
      display: flex;
      align-items: center;
      gap: 16px;
      font-size: 12px;
      color: #94a3b8;
      white-space: nowrap;
    }}
    .meta span {{
      display: flex;
      align-items: center;
      gap: 4px;
    }}

    .viewer-frame {{
      flex: 1;
      border: none;
      background: #fff;
    }}
  </style>
</head>
<body>
  <div class="toolbar">
    <div class="badge"><span class="dot"></span> HAR Replay</div>
    <div class="url-bar">
      <span class="lock">&#x1f512;</span>
      <input type="text" value="{archived_url}" readonly>
    </div>
    <div class="meta">
      <span>{capture_date}</span>
      <span>{resource_count} resources</span>
      <span>{total_kb} KB</span>
    </div>
  </div>
  <iframe class="viewer-frame" src="{first_path}"></iframe>
</body>
</html>
"""


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not HAR_PATH.exists():
        log.error("%s not found. Run capture.py first.", HAR_PATH)
        raise SystemExit(1)

    get_index()
    log.info("Starting viewer at http://localhost:5000/_viewer")
    app.run(host="127.0.0.1", port=5000, debug=False)
