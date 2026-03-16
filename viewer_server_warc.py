"""
Flask web server that replays a WARC archive in a browser viewer.

Supports both single-page and multi-page (batch) WARC files. When multiple
HTML pages are present, the viewer shows a sidebar to switch between them.
"""

import argparse
import gzip as gzip_mod
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, request as flask_request
from warcio.archiveiterator import ArchiveIterator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

DEFAULT_WARC = Path("data/capture.warc.gz")

app = Flask(__name__)


# ── WARC index ───────────────────────────────────────────────────────────────

class WarcIndex:
    """In-memory index built from a WARC file."""

    def __init__(self, warc_path: Path):
        self.entries: dict[str, dict] = {}
        self.path_index: dict[str, dict] = {}
        self.pages: list[dict] = []
        self.first_url: str = ""
        self.first_path: str = ""
        self.origin: str = ""
        self.capture_ts: str = ""
        self.total_size: int = 0
        self._load(warc_path)

    def _load(self, warc_path: Path) -> None:
        seen_html_paths: set[str] = set()
        count = 0

        with open(warc_path, "rb") as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type != "response":
                    continue

                url = record.rec_headers.get_header("WARC-Target-URI")
                if not url:
                    continue

                http_headers = record.http_headers
                status = int(http_headers.get_statuscode()) if http_headers else 200

                content_encoding = ""
                headers = {}
                mime = "application/octet-stream"
                if http_headers:
                    for name, value in http_headers.headers:
                        lower = name.lower()
                        if lower == "content-encoding":
                            content_encoding = value.lower()
                            continue
                        if lower in ("content-length", "content-security-policy",
                                     "x-frame-options", "transfer-encoding",
                                     "etag", "last-modified"):
                            continue
                        headers[name] = value

                    ct = (headers.get("Content-Type")
                          or headers.get("content-type")
                          or "application/octet-stream")
                    mime = ct.split(";")[0].strip()

                body = record.content_stream().read()

                if content_encoding == "gzip" and body:
                    try:
                        body = gzip_mod.decompress(body)
                    except Exception:
                        pass
                elif content_encoding == "br" and body:
                    try:
                        import brotli
                        body = brotli.decompress(body)
                    except Exception:
                        pass

                self.total_size += len(body)

                parsed = urlparse(url)
                path_key = parsed.path
                if parsed.query:
                    path_key += "?" + parsed.query

                warc_date = record.rec_headers.get_header("WARC-Date") or ""

                record_data = {
                    "status": status,
                    "headers": headers,
                    "body": body,
                    "mime": mime,
                    "url": url,
                    "warc_date": warc_date,
                }

                self.entries[url] = record_data
                self.path_index[path_key] = record_data

                is_page = (
                    "text/html" in mime
                    and status == 200
                    and path_key not in seen_html_paths
                )
                if is_page:
                    seen_html_paths.add(path_key)
                    title = ""
                    if body:
                        import re
                        m = re.search(
                            rb"<title[^>]*>(.*?)</title>",
                            body, re.IGNORECASE | re.DOTALL,
                        )
                        if m:
                            title = m.group(1).decode("utf-8", errors="replace").strip()

                    self.pages.append({
                        "url": url,
                        "path": path_key,
                        "title": title or url,
                        "origin": f"{parsed.scheme}://{parsed.netloc}",
                        "date": warc_date,
                        "size": len(body),
                    })

                if count == 0:
                    self.first_url = url
                    self.first_path = parsed.path
                    self.origin = f"{parsed.scheme}://{parsed.netloc}"
                    self.capture_ts = warc_date

                count += 1

        log.info(
            "Loaded %d response(s), %d page(s) from %s",
            count, len(self.pages), warc_path,
        )


warc_index: WarcIndex | None = None


def get_index() -> WarcIndex:
    global warc_index
    if warc_index is None:
        warc_index = WarcIndex(DEFAULT_WARC)
    return warc_index


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/_viewer")
def viewer():
    idx = get_index()

    page_idx = flask_request.args.get("page", "0")
    try:
        page_num = int(page_idx)
    except ValueError:
        page_num = 0
    page_num = max(0, min(page_num, len(idx.pages) - 1))

    current = idx.pages[page_num] if idx.pages else {
        "url": idx.first_url, "path": idx.first_path,
        "title": idx.first_url, "origin": idx.origin,
        "date": idx.capture_ts, "size": 0,
    }

    capture_date = ""
    if current.get("date"):
        try:
            dt = datetime.fromisoformat(current["date"].replace("Z", "+00:00"))
            capture_date = dt.strftime("%b %d, %Y at %H:%M:%S UTC")
        except ValueError:
            capture_date = current["date"]

    pages_html = ""
    for i, pg in enumerate(idx.pages):
        active = "active" if i == page_num else ""
        label = pg["title"][:55] + ("..." if len(pg["title"]) > 55 else "")
        domain = urlparse(pg["url"]).netloc
        pages_html += (
            f'<a class="page-item {active}" href="/_viewer?page={i}">'
            f'<span class="page-title">{label}</span>'
            f'<span class="page-domain">{domain}</span>'
            f'</a>\n'
        )

    multi = len(idx.pages) > 1

    return VIEWER_HTML.format(
        archived_url=current["url"],
        first_path=current["path"],
        capture_date=capture_date,
        resource_count=len(idx.entries),
        page_count=len(idx.pages),
        total_kb=idx.total_size // 1024,
        pages_html=pages_html,
        sidebar_class="has-sidebar" if multi else "",
        current_origin=current.get("origin", idx.origin),
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
            return redirect("/_viewer")
        log.warning("[MISS] %s", lookup)
        abort(404)

    body = entry["body"]
    mime = entry["mime"]

    origin = urlparse(entry["url"]).scheme + "://" + urlparse(entry["url"]).netloc
    rewritable = ("text/html", "text/css", "application/javascript",
                  "text/javascript")
    if any(t in mime for t in rewritable) and body:
        text = body.decode("utf-8", errors="replace")
        text = text.replace(origin, "")
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
        "page_count": len(idx.pages),
        "total_bytes": idx.total_size,
        "pages": idx.pages,
    })


# ── Viewer HTML template ────────────────────────────────────────────────────

VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>WARC Archive Viewer</title>
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
      z-index: 10;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: linear-gradient(135deg, #1d4ed8, #1e3a5f);
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
      background: #60a5fa;
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

    .main {{
      flex: 1;
      display: flex;
      overflow: hidden;
    }}

    .sidebar {{
      display: none;
      width: 280px;
      background: #1e293b;
      border-right: 1px solid #334155;
      overflow-y: auto;
      flex-shrink: 0;
    }}
    .has-sidebar .sidebar {{ display: flex; flex-direction: column; }}

    .sidebar-header {{
      padding: 12px 16px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #94a3b8;
      border-bottom: 1px solid #334155;
      flex-shrink: 0;
    }}
    .sidebar-list {{
      flex: 1;
      overflow-y: auto;
      padding: 4px 0;
    }}

    .page-item {{
      display: block;
      padding: 10px 16px;
      text-decoration: none;
      color: #cbd5e1;
      border-left: 3px solid transparent;
      transition: background 0.15s;
    }}
    .page-item:hover {{ background: #334155; }}
    .page-item.active {{
      background: #0f172a;
      border-left-color: #60a5fa;
      color: #fff;
    }}
    .page-title {{
      display: block;
      font-size: 13px;
      line-height: 1.3;
      margin-bottom: 2px;
    }}
    .page-domain {{
      display: block;
      font-size: 11px;
      color: #64748b;
    }}

    .viewer-frame {{
      flex: 1;
      border: none;
      background: #fff;
    }}
  </style>
</head>
<body class="{sidebar_class}">
  <div class="toolbar">
    <div class="badge"><span class="dot"></span> WARC Replay</div>
    <div class="url-bar">
      <span class="lock">&#x1f512;</span>
      <input type="text" value="{archived_url}" readonly>
    </div>
    <div class="meta">
      <span>{capture_date}</span>
      <span>{page_count} pages</span>
      <span>{resource_count} resources</span>
      <span>{total_kb} KB</span>
    </div>
  </div>
  <div class="main">
    <div class="sidebar">
      <div class="sidebar-header">Archived Pages ({page_count})</div>
      <div class="sidebar-list">
        {pages_html}
      </div>
    </div>
    <iframe class="viewer-frame" src="{first_path}"></iframe>
  </div>
</body>
</html>
"""


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Serve a WARC archive in a browser viewer.",
    )
    parser.add_argument(
        "--warc", type=Path, default=DEFAULT_WARC,
        help=f"Path to WARC file (default: {DEFAULT_WARC})",
    )
    parser.add_argument(
        "--port", type=int, default=5001,
        help="Port to listen on (default: 5001)",
    )
    args = parser.parse_args()

    if not args.warc.exists():
        log.error("%s not found. Run process.py or batch_capture.py first.", args.warc)
        raise SystemExit(1)

    warc_index = WarcIndex(args.warc)

    def get_index_override() -> WarcIndex:
        return warc_index

    import viewer_server_warc as _self
    _self.get_index = get_index_override

    log.info("Starting WARC viewer at http://localhost:%d/_viewer", args.port)
    app.run(host="127.0.0.1", port=args.port, debug=False)
