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
from fastapi.responses import HTMLResponse, JSONResponse
from pymongo.errors import PyMongoError

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


DB_UNAVAILABLE = ("Can't reach the database. Check MONGODB_URI (scripts/set_env.py writes it) and that this "
                  "machine's IP address is on the Atlas access list.")


@app.get("/api/state")
def api_state(database=Depends(get_database)):
    # Probe first: loop.status catches per-section errors, so an unreachable database would otherwise come back as an
    # empty-looking 200 (bug report, 26 Sep). A 503 lets the page say "can't reach the database", not "no activity".
    try:
        database.client.admin.command("ping")
    except PyMongoError:
        return JSONResponse(status_code=503, content={"error": "database_unavailable", "message": DB_UNAVAILABLE})
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


# ---------------------------------------------------------------------------------------------------------------
# "Run it live" button (stage demo). POST /api/demo/run starts the two rehearsed CLI steps in a background thread;
# GET /api/demo/status reports progress. Local-only (127.0.0.1), one run at a time, and only against a pregame* db.
# ---------------------------------------------------------------------------------------------------------------
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from fastapi import Request  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
STEP1_TIMEOUT_S = 240
STEP2_TIMEOUT_S = 90
FALLBACK_TIMEOUT_S = 120
FALLBACK_CASSETTE = "cassettes/room-brief.jsonl"
_URI_RE = re.compile(r"mongodb(\+srv)?://\S+", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

STEP_NAMES = ["Rebuild the self-improvement loop on MongoDB Atlas", "Claude writes a new retirement brief"]

_demo_lock = threading.Lock()
_demo: dict = {"running": False, "step": None, "started_at": None, "steps": []}


def redact(line: str) -> str:
    """Strip colour codes and hide any mongodb:// connection string (it can carry a password)."""
    return _URI_RE.sub("mongodb://[redacted]", _ANSI_RE.sub("", line))


def _server_db_name() -> str:
    from pregame.db import _resolve_db_name  # noqa: PLC0415

    return _resolve_db_name()


def _child_env(db_name: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not (k.upper().startswith("CLAUDE") or k.upper().startswith("ANTHROPIC"))}
    claude_dir = os.environ.get("PREGAME_CLAUDE_DIR")
    if claude_dir:
        env["PATH"] = claude_dir + os.pathsep + env.get("PATH", "")
    env.update({"PREGAME_DB": db_name, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(extra)
    return env


def _new_step(name: str) -> dict:
    return {"name": name, "state": "waiting", "seconds": None, "lines": [], "note": None, "_t0": None}


def _run_cmd(step: dict, args: list, env: dict, timeout: float) -> bool:
    """Run `python -m pregame.cli <args>`, streaming stdout lines into step; True on exit 0 within timeout."""
    if step["_t0"] is None:
        step["_t0"] = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pregame.cli", *args], cwd=str(REPO_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        step["lines"].append(redact(f"could not start: {exc}"))
        return False

    def _reader():
        for raw in proc.stdout:
            step["lines"].append(redact(raw.rstrip("\r\n")))
            del step["lines"][:-400]

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        step["lines"].append(f"stopped after {int(timeout)} s")
        reader.join(timeout=5)
        return False
    reader.join(timeout=5)
    return code == 0


def _finish(step: dict, state: str) -> None:
    step["state"] = state
    if step["_t0"] is not None:
        step["seconds"] = round(time.time() - step["_t0"], 1)
    step["_t0"] = None


def _demo_worker(db_name: str) -> None:
    s1, s2 = _demo["steps"]
    try:
        _demo["step"] = 1
        s1["state"] = "running"
        ok = _run_cmd(s1, ["demo"], _child_env(db_name, PREGAME_LLM_MODE="replay"), STEP1_TIMEOUT_S)
        _finish(s1, "done" if ok else "failed")
        if not ok:
            return
        _demo["step"] = 2
        s2["state"] = "running"
        live_env = _child_env(db_name, PREGAME_LLM_MODE="live", PREGAME_PROVIDER="claude-cli")
        if _run_cmd(s2, ["brief", "retirement"], live_env, STEP2_TIMEOUT_S):
            _finish(s2, "done")
            return
        live_secs = round(time.time() - (s2["_t0"] or time.time()), 1)
        s2["lines"].append(f"live call did not finish ({live_secs} s): using the recorded fallback")
        s2["note"] = "recorded fallback: a brief Claude wrote on this laptop earlier"
        fb_env = _child_env(db_name, PREGAME_LLM_MODE="replay", PREGAME_CASSETTE=FALLBACK_CASSETTE)
        ok = _run_cmd(s2, ["brief", "retirement"], fb_env, FALLBACK_TIMEOUT_S)
        _finish(s2, "fallback" if ok else "failed")
    except Exception as exc:  # noqa: BLE001
        for s in (s1, s2):
            if s["state"] == "running":
                s["lines"].append(redact(f"error: {exc}"))
                _finish(s, "failed")
    finally:
        _demo["running"] = False
        _demo["step"] = None


def _brief_head(lines: list, n: int = 14) -> list:
    for i, line in enumerate(lines):
        if line.startswith("brief ") and "blocked" not in line:
            return lines[i:i + n]
    return []


@app.post("/api/demo/run")
def api_demo_run(request: Request):
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        return JSONResponse(status_code=403, content={"error": "local_only",
                                                      "message": "The live run only starts from this laptop."})
    db_name = _server_db_name()
    if not str(db_name).startswith("pregame"):
        return JSONResponse(status_code=400, content={"error": "wrong_database",
                                                      "message": f"Refusing: database {db_name!r} is not a pregame* database."})
    with _demo_lock:
        if _demo["running"]:
            return JSONResponse(status_code=409, content={"error": "already_running",
                                                          "message": "A live run is already in progress."})
        _demo.update(running=True, step=0, started_at=time.time(), steps=[_new_step(n) for n in STEP_NAMES])
    threading.Thread(target=_demo_worker, args=(db_name,), daemon=True).start()
    return {"started": True, "db": db_name}


@app.get("/api/demo/status")
def api_demo_status() -> dict:
    now = time.time()
    steps = []
    for s in list(_demo["steps"]):
        secs = s["seconds"] if s["_t0"] is None else round(now - s["_t0"], 1)
        lines = list(s["lines"])
        steps.append({"name": s["name"], "state": s["state"], "seconds": secs, "lines": lines[-25:],
                      "note": s["note"], "brief_head": _brief_head(lines) if s["state"] in ("done", "fallback") else []})
    return {"running": _demo["running"], "step": _demo["step"], "steps": steps, "started_at": _demo["started_at"]}
