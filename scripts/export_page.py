"""Export a static, self-contained snapshot of Pregame's live page.

The live page (`pregame/web/static/index.html`, served by `pregame/web/app.py`) polls
`GET /api/state` every 2 seconds and reads `state.briefs[<field>]` for its Brief
before/after panel; the app also exposes `GET /api/brief/{brief_id}` for a single brief.
Both only work against a running server with a database password, so this script builds a
plain HTML file that embeds the same data as JSON and answers those two `fetch()` calls from
memory -- the page's own rendering code runs completely unchanged.

READ ONLY: this script only ever reads from the database named by --db. It never calls
`pregame.loop.setup`/`pregame.db.reset_db`/`init_db`, never writes a document, and never prints
a connection string, host, username or key. It does not call any LLM or the `claude` CLI.

Usage:
    .venv/Scripts/python scripts/export_page.py --db pregame_demo --out docs/index.html
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # `pregame` isn't installed; run from any working directory like scripts/serve.py
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_STATIC_INDEX = REPO_ROOT / "pregame" / "web" / "static" / "index.html"
GITHUB_URL = "https://github.com/sairam782/PreGame"

# ---------------------------------------------------------------------------------------------------------------
# Secret scrubbing -- belt-and-suspenders on top of only ever reading fields loop.status()/api_brief already
# expose (which don't carry connection info). Any key that looks like a credential is dropped outright rather
# than guessed at, and any string that looks like a mongo connection string is redacted wherever it appears.
# ---------------------------------------------------------------------------------------------------------------
_SECRET_KEYS = {
    "uri", "mongodb_uri", "mongo_uri", "connection_string", "connectionstring",
    "password", "passwd", "pwd", "secret", "api_key", "apikey", "anthropic_api_key",
    "token", "auth", "authorization", "host", "hostname", "cluster", "cluster_host",
    "username", "user", "db_user",
}
_MONGO_URI_RE = re.compile(r"mongodb(?:\+srv)?://[^\s\"'<>]+", re.IGNORECASE)


def _scrub(value):
    """Recursively drop credential-shaped keys and redact any embedded mongo connection string."""
    if isinstance(value, dict):
        return {
            k: _scrub(v)
            for k, v in value.items()
            if str(k).strip().lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _MONGO_URI_RE.sub("[redacted]", value)
    return value


def assert_no_secrets(payload: dict) -> None:
    """Defence in depth: fail loudly rather than ever write a mongo connection string to disk."""
    blob = json.dumps(payload)
    if _MONGO_URI_RE.search(blob):
        raise RuntimeError("refusing to write output: a mongodb connection string survived scrubbing")


# ---------------------------------------------------------------------------------------------------------------
# Data collection -- exactly what the live page can fetch
# ---------------------------------------------------------------------------------------------------------------
def collect_snapshot(db) -> dict:
    """Collect loop.status(db) (== GET /api/state) plus every brief GET /api/brief/{id} can serve,
    plus the latest cabinet_runs document (== GET /api/cabinet).

    Brief ids are chosen the same way the page would ever learn about one: they're the `_id`s
    already present in `state["briefs"][field]` (see pregame/web/app.py's `api_state` /
    `api_brief`). Each brief is re-fetched and re-serialised through the exact same code path
    `api_brief` uses (`db.briefs.find_one` + `loop._json_safe`), not just copied out of `state`,
    so the snapshot matches what `/api/brief/{id}` would actually return.
    """
    from pregame import loop
    from pregame.cabinet import RUNS_COLLECTION

    state = loop.status(db)  # the same dict GET /api/state returns, already JSON-safe

    brief_ids: list[str] = []
    for field_briefs in (state.get("briefs") or {}).values():
        for brief in field_briefs or []:
            brief_id = brief.get("_id")
            if brief_id is not None and brief_id not in brief_ids:
                brief_ids.append(brief_id)

    briefs_by_id: dict[str, dict] = {}
    for brief_id in brief_ids:
        doc = db.briefs.find_one({"_id": brief_id})
        if doc:
            briefs_by_id[brief_id] = loop._json_safe(doc)

    cabinet_doc = db[RUNS_COLLECTION].find_one(sort=[("created_at", -1)])
    cabinet = loop._json_safe(cabinet_doc) if cabinet_doc else {}

    payload = {"state": state, "briefs_by_id": briefs_by_id, "cabinet": cabinet}
    return _scrub(payload)


# ---------------------------------------------------------------------------------------------------------------
# HTML assembly
# ---------------------------------------------------------------------------------------------------------------
_FETCH_SHIM_TEMPLATE = """
<script id="pregame-snapshot-data" type="application/json">{payload_json}</script>
<script>
(function () {{
  // Static snapshot shim: answers the page's own fetch('/api/state') and fetch('/api/brief/...')
  // calls from the embedded JSON above, so the rendering code below runs completely unchanged.
  // Polling every 2s is harmless here -- it just re-serves the same frozen snapshot, no network.
  var SNAPSHOT = JSON.parse(document.getElementById('pregame-snapshot-data').textContent);
  var STATE = SNAPSHOT.state;
  var BRIEFS_BY_ID = SNAPSHOT.briefs_by_id || {{}};
  var CABINET = SNAPSHOT.cabinet || {{}};

  function jsonResponse(body, ok) {{
    return Promise.resolve({{
      ok: ok,
      status: ok ? 200 : 404,
      json: function () {{ return Promise.resolve(body); }}
    }});
  }}

  window.fetch = function (input) {{
    var url = String(input);
    if (url.indexOf('/api/state') !== -1) {{
      return jsonResponse(STATE, true);
    }}
    if (url.indexOf('/api/cabinet') !== -1) {{
      return jsonResponse(CABINET, true);
    }}
    var m = url.match(/\\/api\\/brief\\/([^/?#]+)/);
    if (m) {{
      var id = decodeURIComponent(m[1]);
      var brief = BRIEFS_BY_ID[id];
      if (brief) return jsonResponse(brief, true);
      return jsonResponse({{ detail: 'no such brief: ' + id }}, false);
    }}
    return Promise.reject(new Error('this is a static snapshot; no network requests are made'));
  }};
}})();
</script>
"""

_BANNER_TEMPLATE = (
    '<div id="snapshot-banner" style="background:#2559d6;color:#fff;font-weight:700;'
    "padding:10px 16px;text-align:center;font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:0.95rem;line-height:1.4;\">"
    "{text}</div>"
)


def build_html(original_html: str, payload: dict, db_name: str, exported_at: str) -> str:
    """Return `original_html` with the snapshot banner + data/fetch-shim spliced in after <body>.

    index.html itself is never modified: its inline <script> (poll/render) is left byte-for-byte
    as-is, so the same rendering code that runs against a live server runs here unchanged. The
    shim is inserted before it in document order so `window.fetch` is patched before `poll()`
    (called at the very bottom of that script) ever runs.
    """
    banner_text = (
        "Snapshot of the stage replay of a live run on Claude Sonnet 5 / Haiku 4.5 / Opus 5.5 "
        f"— database {db_name}, exported {exported_at}. Code: {GITHUB_URL}"
    )
    banner_html = _BANNER_TEMPLATE.format(text=html.escape(banner_text))
    payload_json = json.dumps(payload, separators=(",", ":"))
    shim_html = _FETCH_SHIM_TEMPLATE.format(payload_json=payload_json)

    marker = "<body>"
    if marker not in original_html:
        raise ValueError("index.html has no <body> tag to splice the snapshot into")
    return original_html.replace(marker, marker + "\n" + banner_html + shim_html, 1)


# ---------------------------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------------------------
def export_page(db_name: str, out_path: Path, static_index_path: Path = DEFAULT_STATIC_INDEX) -> Path:
    from pregame.db import get_db

    default_db = get_db()  # resolves the URI from settings/.env/env var -- never printed
    db = default_db.client[db_name]  # same cluster/client, the database the caller asked for

    payload = collect_snapshot(db)
    assert_no_secrets(payload)

    original_html = static_index_path.read_text(encoding="utf-8")
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out_html = build_html(original_html, payload, db_name, exported_at)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out_html, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="pregame_demo", help="database name to read (default: pregame_demo)")
    parser.add_argument("--out", default="docs/index.html", help="output HTML path (default: docs/index.html)")
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path

    written = export_page(args.db, out_path)
    size = written.stat().st_size
    print(f"wrote {written} ({size:,} bytes) from database {args.db!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
