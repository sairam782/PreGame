"""FastAPI app: a thin lens on the loop, not the product.

GET /            -> static/index.html
GET /api/state   -> loop.status(db)
GET /api/brief/{id} -> one brief (JSON-safe)
GET /api/cabinet -> latest cabinet_runs document (JSON-safe), or {} when none

`get_database` is a FastAPI dependency (not called at import time) so tests can override it
with a mongomock db via `app.dependency_overrides[get_database] = lambda: db` and never touch
the network.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from pregame import loop
from pregame.cabinet import RUNS_COLLECTION

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Pregame")

_db_singleton = None


def get_database():
    """Lazily create the real db connection. Overridden in tests to avoid any network I/O."""
    global _db_singleton
    if _db_singleton is None:
        from pregame.db import get_db

        _db_singleton = get_db()
    return _db_singleton


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/state")
def api_state(database=Depends(get_database)) -> dict:
    return loop.status(database)


@app.get("/api/brief/{brief_id}")
def api_brief(brief_id: str, database=Depends(get_database)) -> dict:
    brief = database.briefs.find_one({"_id": brief_id})
    if not brief:
        raise HTTPException(status_code=404, detail=f"no such brief: {brief_id}")
    return loop._json_safe(brief)


@app.get("/api/cabinet")
def api_cabinet(database=Depends(get_database)) -> dict:
    doc = database[RUNS_COLLECTION].find_one(sort=[("created_at", -1)])
    if not doc:
        return {}
    return loop._json_safe(doc)
