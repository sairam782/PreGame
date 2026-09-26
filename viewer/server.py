#!/usr/bin/env python3
"""Pregame viewer: a read-only local server for the demo.

It shows what the Pregame harness's agents are doing (the hash-chained ledger and the proposals, versions, briefs
around it), the findings, and the synthetic banker cabinet. It NEVER writes to MongoDB: every database call goes
through `ReadOnlyMongo`, which only offers find, count_documents, aggregate (refusing $out/$merge) and
list_collection_names (plus list_database_names, to find the pregame_* databases).

Standard library plus pymongo (imported lazily, so --offline needs no packages). Every path is relative to this
file's folder. The API is specified in CONTRACT.md.

    python server.py                 # serve from MongoDB Atlas (config in viewer.env)
    python server.py --offline       # serve the snapshot in fixtures/
    python server.py --snapshot      # write every endpoint's JSON into fixtures/, then exit
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import decimal
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
FIXTURES_DIR = HERE / "fixtures"
DATA_DIR = HERE / "data"
ENV_FILE = HERE / "viewer.env"

DEFAULT_PORT = 8877
PORT_TRIES = 10  # if the port is busy, the next 10 are tried
DEFAULT_PREGAME_DB = "pregame_demo"
_DB_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="viewer-db")  # parallel reads for one request
FIELDS = ("retirement", "families", "business_owners")
OTHER_DBS = ("cabinet", "cabinet_truth", "harness")
GENESIS_HASH = "GENESIS"
KIND_ORDER = {"feed": 0, "note": 1, "prep": 2, "harness_prep": 3}
CONFIG_KIND_ORDER = {"policy": 0, "rules": 1, "tools": 2, "guardrails": 3}
PROPOSAL_STATUSES = ("pending", "evaluating", "awaiting_owner", "committed", "rejected", "stale")


# ---------------------------------------------------------------------------------------------------------------
# Config and secrets
# ---------------------------------------------------------------------------------------------------------------
def read_env_file(path: Path) -> dict:
    """KEY=VALUE lines; blank lines and # comments ignored; optional `export ` and surrounding quotes stripped."""
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def load_config(env_file: Path = ENV_FILE, environ: Optional[dict] = None) -> dict:
    """viewer.env in the folder, overridden by environment variables that are set and non-empty."""
    environ = os.environ if environ is None else environ
    cfg = {
        "MONGODB_URI": "",
        "PREGAME_DB": DEFAULT_PREGAME_DB,
        "PREGAME_LLM_MODE": "",
        "PREGAME_PROVIDER": "",
        "VIEWER_PORT": str(DEFAULT_PORT),
        "VIEWER_DB_TIMEOUT_MS": "6000",
    }
    cfg.update({k: v for k, v in read_env_file(env_file).items() if k in cfg})
    for key in list(cfg):
        if environ.get(key):
            cfg[key] = environ[key]
    return cfg


class Secrets:
    """The connection string and its password must never reach a response, a fixture or the console.

    `hard` tokens (the URI, the password) make a response fail closed; `soft` tokens (user name, cluster hosts)
    are only redacted from error messages.
    """

    def __init__(self, uri: str):
        self.hard: list[str] = []
        self.soft: list[str] = []
        uri = (uri or "").strip()
        if not uri:
            return
        self.hard.append(uri)
        m = re.match(r"^[A-Za-z0-9+.-]+://(?:([^@/]*)@)?([^/?#]*)", uri)
        if m:
            userinfo, hosts = m.group(1) or "", m.group(2) or ""
            if userinfo:
                user, _, password = userinfo.partition(":")
                for token in {password, urllib.parse.unquote(password)}:
                    if len(token) >= 4:
                        self.hard.append(token)
                for token in {user, urllib.parse.unquote(user)}:
                    if len(token) >= 3:
                        self.soft.append(token)
            for host in hosts.split(","):
                host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
                if host:
                    self.soft.append(host)
                    # SRV records resolve to shard hosts that share the cluster's domain suffix.
                    parts = host.split(".", 1)
                    if len(parts) == 2 and parts[1].count(".") >= 1:
                        self.soft.append(parts[1])
        self.hard = sorted(set(self.hard), key=len, reverse=True)
        self.soft = sorted(set(self.soft), key=len, reverse=True)

    def leaks(self, text: str) -> bool:
        return any(token and token in text for token in self.hard) or "mongodb+srv://" in text \
            or bool(re.search(r"mongodb://[^\s\"']*@", text))

    def redact(self, text: str) -> str:
        text = str(text)
        for token in self.hard + self.soft:
            if token:
                text = text.replace(token, "***")
        return re.sub(r"mongodb(\+srv)?://\S+", "mongodb://***", text)


# ---------------------------------------------------------------------------------------------------------------
# JSON conversion
# ---------------------------------------------------------------------------------------------------------------
def iso(value: Any) -> Optional[str]:
    """A datetime as a UTC ISO string ending in Z (naive datetimes are UTC, as pymongo returns them)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def jsonable(o: Any, _depth: int = 0) -> Any:
    """Convert BSON/Python values to plain JSON: ObjectId -> str, datetime -> ISO Z, bytes -> 'base64:...'."""
    if _depth > 60:
        return str(o)
    if o is None or isinstance(o, (bool, str)):
        return o
    if isinstance(o, int):
        return int(o)
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dt.datetime) or isinstance(o, dt.date):
        return iso(o)
    if isinstance(o, dict):
        return {str(k): jsonable(v, _depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set, frozenset)):
        return [jsonable(v, _depth + 1) for v in o]
    if isinstance(o, (bytes, bytearray, memoryview)):
        raw = bytes(o)
        encoded = base64.b64encode(raw[:4096]).decode("ascii")
        return "base64:" + encoded + ("" if len(raw) <= 4096 else f"...({len(raw)} bytes)")
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, decimal.Decimal):
        return str(o)
    if isinstance(o, re.Pattern):
        return o.pattern
    name = type(o).__name__
    if name == "ObjectId":
        return str(o)
    if name == "Decimal128":
        return str(o.to_decimal())
    if name == "Timestamp":
        return iso(o.as_datetime())
    if name == "Regex":
        return str(o.pattern)
    if name == "DBRef":
        return {"$ref": o.collection, "$id": jsonable(o.id, _depth + 1)}
    if name in ("MinKey", "MaxKey"):
        return name
    return str(o)


def clean(doc: Any) -> Any:
    """jsonable, with the top-level Mongo `_id` returned as `id` (a string). An existing `id` field wins."""
    out = jsonable(doc)
    if isinstance(out, dict) and "_id" in out:
        _id = out.pop("_id")
        if "id" not in out:
            as_text = json.dumps(_id, sort_keys=True) if isinstance(_id, (dict, list)) else str(_id)
            out = {"id": as_text, **out}
    return out


def trim_for_browser(value: Any, _depth: int = 0) -> Any:
    """Keep the generic DB browser readable: long number arrays (embeddings) and very long strings are shortened."""
    if isinstance(value, dict):
        return {k: trim_for_browser(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 32 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            return value[:8] + [f"... {len(value) - 8} more numbers"]
        return [trim_for_browser(v, _depth + 1) for v in value]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + f"... ({len(value)} chars)"
    return value


# ---------------------------------------------------------------------------------------------------------------
# Errors and the read-only database wrapper
# ---------------------------------------------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, error: str, **extra: Any):
        super().__init__(error)
        self.status = status
        self.payload = {"error": error, **extra}


class DBUnavailable(ApiError):
    def __init__(self, error: str, detail: str = "", hint: str = ""):
        extra = {}
        if detail:
            extra["detail"] = detail
        extra["hint"] = hint or ("Check viewer.env, add this laptop's IP address to Atlas Network Access, "
                                 "or start with --offline to serve the snapshot.")
        super().__init__(503, error, **extra)


_WRITE_STAGES = ("$out", "$merge")


def assert_read_only_pipeline(pipeline: Any) -> None:
    """Refuse any aggregation that could write ($out, $merge), at any depth (e.g. inside $facet or $lookup)."""
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _WRITE_STAGES:
                    raise ApiError(400, f"aggregation stage {key} is not allowed: the viewer is read-only")
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
    walk(pipeline)


class ReadOnlyMongo:
    """The only door to MongoDB. It offers exactly four read operations; there is no write method to call."""

    def __init__(self, uri: str, timeout_ms: int, secrets: Secrets):
        self._uri = uri
        self._timeout_ms = max(200, int(timeout_ms))
        self._secrets = secrets
        self._client = None
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._down_error: Optional[DBUnavailable] = None

    def _get_client(self):
        if not self._uri:
            raise DBUnavailable("MONGODB_URI is not set",
                                hint="Copy viewer.env.example to viewer.env and fill in MONGODB_URI, "
                                     "or start with --offline to serve the snapshot.")
        if self._down_error is not None and time.monotonic() < self._down_until:
            raise self._down_error
        with self._lock:
            if self._client is None:
                try:
                    from pymongo import MongoClient  # lazy: --offline needs no packages
                except ImportError:
                    raise DBUnavailable("pymongo is not installed",
                                        hint="Run the start script (it installs requirements.txt), "
                                             "or start with --offline to serve the snapshot.")
                try:
                    self._client = MongoClient(
                        self._uri,
                        appname="pregame-viewer",
                        tz_aware=True,
                        serverSelectionTimeoutMS=self._timeout_ms,
                        connectTimeoutMS=self._timeout_ms,
                        socketTimeoutMS=max(20000, self._timeout_ms),
                        retryWrites=False,
                    )
                except Exception as exc:  # bad URI, DNS failure for +srv, missing dnspython
                    raise self._fail(exc)
        return self._client

    def _fail(self, exc: BaseException) -> DBUnavailable:
        detail = f"{type(exc).__name__}: {self._secrets.redact(str(exc))[:400]}"
        err = DBUnavailable("database unavailable", detail=detail)
        name = type(exc).__name__
        code = getattr(exc, "code", None)
        if name in ("ServerSelectionTimeoutError", "ConnectionFailure", "AutoReconnect", "NetworkTimeout",
                    "ConfigurationError", "InvalidURI") or code in (13, 18, 8000):
            # Fail fast for a few seconds instead of making every 2 s poll wait for a server-selection timeout.
            self._down_error = err
            self._down_until = time.monotonic() + 8.0
        return err

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        client = self._get_client()
        try:
            result = fn(client)
        except ApiError:
            raise
        except Exception as exc:
            if type(exc).__module__.startswith(("pymongo", "bson")):
                raise self._fail(exc)
            raise
        self._down_error = None
        return result

    # -- the four allowed operations -----------------------------------------------------------------------------
    def find(self, db: str, coll: str, filter: Optional[dict] = None, projection: Optional[dict] = None,
             sort: Optional[list] = None, limit: int = 0) -> list:
        def op(client):
            cursor = client[db][coll].find(filter or {}, projection, sort=sort, limit=max(0, int(limit)))
            return list(cursor)
        return self._run(op)

    def count(self, db: str, coll: str, filter: Optional[dict] = None) -> int:
        return self._run(lambda client: client[db][coll].count_documents(filter or {}))

    def aggregate(self, db: str, coll: str, pipeline: list) -> list:
        assert_read_only_pipeline(pipeline)
        return self._run(lambda client: list(client[db][coll].aggregate(pipeline)))

    def collections(self, db: str) -> list:
        names = self._run(lambda client: client[db].list_collection_names())
        return sorted(n for n in names if not n.startswith("system."))

    def databases(self) -> Optional[list]:
        """list_database_names (read-only). None when this user may not list databases."""
        def op(client):
            try:
                return client.list_database_names()
            except Exception as exc:
                if getattr(exc, "code", None) == 13:  # Unauthorized: the caller falls back to PREGAME_DB only
                    return None
                raise
        return self._run(op)

    def close(self) -> None:
        """Close the connection pool (not a database operation)."""
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


# ---------------------------------------------------------------------------------------------------------------
# Activity: one plain sentence per ledger kind
# ---------------------------------------------------------------------------------------------------------------
def _short_id(value: Any) -> str:
    s = str(value or "")
    return s[:8] if re.fullmatch(r"[0-9a-f]{32}", s) else s


def _date_part(value: Any) -> str:
    s = iso(value) if isinstance(value, (dt.datetime, dt.date)) else str(value or "")
    return s[:10]


def _strip_label(text: str, *labels: str) -> str:
    text = (text or "").strip()
    for label in labels:
        if text.lower().startswith(label.lower() + ":"):
            return text[len(label) + 1:].strip()
    return text


def _sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return text
    if text[-1] in ".!?" or text[-2:] in ('."', '?"', '!"'):
        return text
    return text + "."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _acc(summary: Any) -> Optional[float]:
    if isinstance(summary, dict) and isinstance(summary.get("mean_accuracy"), (int, float)):
        return float(summary["mean_accuracy"])
    return None


_OUTCOME_WORDS = {"awaiting_owner": "waiting for a person to approve", "committed": "committed",
                  "rejected": "rejected", "stale": "stale"}


def _sum_seed(p: dict, actor: str, ctx: dict) -> Optional[str]:
    seeded = p.get("seeded") or []
    if not seeded:
        return None
    kinds = sorted({str(s).split(":")[0] for s in seeded if ":" in str(s)})
    return f"Seeded version 1 of {len(seeded)} config surfaces ({', '.join(kinds)})"


def _sum_event(p: dict, actor: str, ctx: dict) -> Optional[str]:
    action = p.get("action")
    if action == "load_world":
        fields = p.get("fields") or []
        return (f"Loaded the simulated world: {p.get('base_facts', 0)} base facts and "
                f"{_plural(int(p.get('events', 0) or 0), 'scripted market event')} across {_plural(len(fields), 'field')}")
    if action == "fire_event" or p.get("event_id"):
        event_id = p.get("event_id")
        title = p.get("title") or ctx.get("event_titles", {}).get(event_id) or event_id
        n = len(p.get("fact_ids") or [])
        where = f" in {p['field']}" if p.get("field") else ""
        return f"Market event fired{where}: {title} ({_plural(n, 'new fact')})"
    return f"World event: {action}" if action else None


def _sum_brief(p: dict, actor: str, ctx: dict) -> Optional[str]:
    account = p.get("account_id") or "an account"
    n = len(p.get("fact_ids") or [])
    versions = p.get("versions") or {}
    config = ", ".join(f"{k} v{v}" for k, v in versions.items()) if isinstance(versions, dict) else ""
    as_of = _date_part(p.get("as_of"))
    text = f"Drafted a brief for {account}"
    if as_of:
        text += f" as of {as_of}"
    text += f" from {_plural(n, 'fact')}"
    if config:
        text += f" ({config})"
    return text


def _sum_feedback(p: dict, actor: str, ctx: dict) -> Optional[str]:
    event_id = p.get("event_id")
    title = ctx.get("event_titles", {}).get(event_id) or event_id or "a market event"
    text = ctx.get("feedback_texts", {}).get(p.get("feedback_id"))
    if text:
        return f'Advisor feedback after {title}: "{str(text).strip()}"'
    return f"Advisor feedback after {title}: the brief for that call missed a material change"


def _sum_proposal(p: dict, actor: str, ctx: dict) -> Optional[str]:
    pid = p.get("proposal_id") or "a proposal"
    target = f"{p.get('kind', '?')}:{p.get('key', '?')}"
    base = p.get("base_version")
    diff = [str(d) for d in (p.get("diff") or [])]
    changes = "; ".join(diff[:3]) + (f" (+{len(diff) - 3} more)" if len(diff) > 3 else "")
    text = f"Proposed a change to {target}" + (f" v{base}" if base is not None else "") + f" ({pid})"
    return text + (f": {changes}" if changes else "")


def _sum_eval(p: dict, actor: str, ctx: dict) -> Optional[str]:
    pid = p.get("proposal_id") or "a proposal"
    tier = f" (tier {p['tier']})" if p.get("tier") else ""
    outcome = _OUTCOME_WORDS.get(p.get("outcome"), p.get("outcome") or "")
    heldout, tuning = p.get("heldout"), p.get("tuning")
    if isinstance(heldout, dict) and _acc(heldout.get("candidate")) is not None and _acc(heldout.get("champion")) is not None:
        cand, champ = _acc(heldout["candidate"]), _acc(heldout["champion"])
        verdict = "a win" if heldout.get("win") else "not a win"
        return (f"Scored {pid}{tier} on held-out scenarios: candidate {cand:.2f} vs champion {champ:.2f}, {verdict}"
                + (f"; {outcome}" if outcome else ""))
    if isinstance(tuning, dict) and _acc(tuning.get("candidate")) is not None and _acc(tuning.get("champion")) is not None:
        cand, champ = _acc(tuning["candidate"]), _acc(tuning["champion"])
        return (f"Screened {pid}{tier} on tuning scenarios: candidate {cand:.2f} vs champion {champ:.2f}; "
                f"it did not reach held-out" + (f"; {outcome}" if outcome else ""))
    decision = p.get("decision")
    if decision:
        return f"Evaluated {pid}{tier}: {decision}"
    return f"Evaluated {pid}{tier}" + (f": {outcome}" if outcome else "")


def _owner(actor: str) -> Optional[str]:
    return actor.split(":", 1)[1] if isinstance(actor, str) and actor.startswith("owner:") else None


def _sum_commit(p: dict, actor: str, ctx: dict) -> Optional[str]:
    target = f"{p.get('kind', '?')}:{p.get('key', '?')}"
    text = f"Committed {target} v{p.get('version', '?')}"
    if p.get("base_version") is not None:
        text += f" (was v{p['base_version']})"
    if p.get("proposal_id"):
        text += f" from {p['proposal_id']}"
    if _owner(actor):
        text += f", approved by {_owner(actor)}"
    return text


def _sum_reject(p: dict, actor: str, ctx: dict) -> Optional[str]:
    pid = p.get("proposal_id") or "a proposal"
    decision = _strip_label(p.get("decision") or "", "Rejected", "Stale")
    who = f" ({_owner(actor)})" if _owner(actor) else ""
    if p.get("status") == "stale":
        return f"Marked {pid} stale{who}" + (f": {decision}" if decision else "")
    return f"Rejected {pid}{who}" + (f": {decision}" if decision else "")


def _sum_refused(p: dict, actor: str, ctx: dict) -> Optional[str]:
    if p.get("attempted"):
        reason = p.get("reason")
        return f"Stopped the improver from reading {p['attempted']}" + (f": {reason}" if reason else "")
    if p.get("what") == "brief":
        violations = [str(v) for v in (p.get("violations") or [])]
        text = f"Blocked a brief for {p.get('account_id') or 'an account'}"
        if violations:
            text += f": {_plural(len(violations), 'guardrail violation')}, first: {violations[0]}"
        return text
    pid = p.get("proposal_id")
    reason = _strip_label(p.get("decision") or p.get("reason") or "", "Refused")
    if pid:
        return f"Refused {pid}" + (f": {reason}" if reason else "")
    return f"Refused: {reason}" if reason else None


def _sum_approve(p: dict, actor: str, ctx: dict) -> Optional[str]:
    who = _owner(actor) or actor or "A person"
    text = f"{who} approved {p.get('proposal_id') or 'a proposal'}"
    return text + (f", committing {p['version_id']}" if p.get("version_id") else "")


def _sum_rollback(p: dict, actor: str, ctx: dict) -> Optional[str]:
    target = f"{p.get('kind', '?')}:{p.get('key', '?')}"
    return f"Rolled back {target} to the body of v{p.get('restores', '?')}, as v{p.get('version', '?')}"


SUMMARIES: dict = {
    "seed": _sum_seed, "event": _sum_event, "brief": _sum_brief, "feedback": _sum_feedback,
    "proposal": _sum_proposal, "eval": _sum_eval, "commit": _sum_commit, "reject": _sum_reject,
    "refused": _sum_refused, "approve": _sum_approve, "rollback": _sum_rollback,
}


def summarize(kind: str, actor: str, payload: Any, ctx: Optional[dict] = None) -> str:
    """One plain sentence for a ledger entry; unknown kinds (or unexpected payloads) fall back to '<actor> <kind>'."""
    fn = SUMMARIES.get(kind)
    text = None
    if fn is not None and isinstance(payload, dict):
        try:
            text = fn(payload, actor or "", ctx or {})
        except Exception:
            text = None
    return _sentence(text) if text else f"{actor} {kind}"


def entry_status(kind: str, payload: dict) -> Optional[str]:
    """Set for proposal-related kinds: what this entry did to the proposal."""
    p = payload if isinstance(payload, dict) else {}
    if kind == "proposal":
        return "pending"
    if kind == "eval":
        return p.get("outcome") or None
    if kind in ("commit", "approve"):
        return "committed"
    if kind == "reject":
        return p.get("status") or "rejected"
    if kind == "refused" and p.get("proposal_id"):
        return "refused"
    return None


def entry_field(kind: str, payload: dict, ctx: Optional[dict] = None) -> Optional[str]:
    p = payload if isinstance(payload, dict) else {}
    if p.get("field"):
        return p["field"]
    if kind in ("commit", "rollback") and p.get("key"):
        return p["key"]
    if kind == "feedback":  # the feedback payload carries ids only; its document has the field
        return (ctx or {}).get("feedback_fields", {}).get(p.get("feedback_id"))
    return None


def entry_ref(kind: str, payload: dict) -> Optional[dict]:
    p = payload if isinstance(payload, dict) else {}
    if p.get("proposal_id"):
        return {"type": "proposal", "id": p["proposal_id"]}
    if kind == "brief" and p.get("brief_id"):
        return {"type": "brief", "id": p["brief_id"]}
    if kind == "feedback" and p.get("feedback_id"):
        return {"type": "feedback", "id": p["feedback_id"]}
    if kind == "event" and p.get("event_id"):
        return {"type": "event", "id": p["event_id"]}
    if kind in ("commit", "rollback") and p.get("kind") and p.get("key") and p.get("version") is not None:
        return {"type": "version", "id": f"{p['kind']}:{p['key']}@v{p['version']}"}
    return None


# ---------------------------------------------------------------------------------------------------------------
# Plain words for judges: a display-time translation of the activity sentence. Stored text is never altered: the
# payload and decision fields stay exactly as stored; only `summary` (plain) is built here, next to
# `summary_technical` (the engineering wording) and `actor_label`.
# ---------------------------------------------------------------------------------------------------------------
PLAIN_SURFACE = {"policy": "what goes into the brief", "rules": "writing instructions", "tools": "data sources",
                 "guardrails": "safety checks", "scenarios": "the unseen test meetings' questions"}
PLAIN_TIER = {"G": "adopted automatically if it wins", "H": "needs a person's sign-off",
              "X": "never allowed: it would change how the system is graded"}
PLAIN_FACT_KIND = {"price": "rate and price facts", "regulation": "tax and rule facts",
                   "competitor": "product-change facts", "disruption": "market-shock facts",
                   "demand": "economy facts", "account": "the client's own notes"}
PLAIN_SOURCE = {"market_feed": "the verified market feed", "account_notes": "the advisor's client notes",
                "analyst_notes": "analyst notes (unverified)"}
PLAIN_OUTCOME = {"rejected": "rejected", "committed": "adopted", "stale": "set aside (the settings moved on)",
                 "awaiting_owner": "waiting for a person's sign-off"}
DEFAULT_PROPOSER = "Opus"


def actor_names(actor: str, proposer: Optional[str] = None) -> tuple:
    """(actor_label, sentence subject) for an actor id; the stored `actor` is kept as is."""
    proposer = proposer or DEFAULT_PROPOSER
    table = {"improver": (f"{proposer} (proposer)", proposer), "gate": ("test gate (code)", "Test gate"),
             "drafter": ("Sonnet (writer)", "Sonnet"), "world": ("simulated world", "The simulated world"),
             "guardrails": ("safety checks (code)", "Safety checks")}
    if actor in table:
        return table[actor]
    owner = _owner(actor)
    if owner:
        return f"{owner} (person)", owner
    return actor or "someone", actor or "Someone"


_PLAIN_TERMS = [
    (r"\bheld-?out\b", "unseen test meetings"), (r"\btuning\b", "practice meetings"),
    (r"\bchampion\b", "current version"), (r"\bcandidate\b", "proposed version"),
    (r"\bcontext tokens\b", "facts given to the writer"), (r"\bcontext\b", "facts given to the writer"),
    (r"\bmean accuracy\b", "questions answered correctly"), (r"\baccuracy\b", "questions answered correctly"),
    (r"\bproposal\b", "proposed change"), (r"\bCommitted\b", "Adopted"), (r"\bcommitted\b", "adopted"),
    (r"\bledger\b", "audit log"), (r"\bguardrail violations\b", "safety-check failures"),
    (r"\bpass\^k\b", "consistency across repeat runs"), (r"\binclude_kinds\b", "fact types"),
    (r"\bmax_facts\b", "most facts"), (r"\bprefer_exposed\b", "client-relevant facts first"),
]


def plainify(text: str) -> str:
    """Word-for-word vocabulary swap, the fallback when a sentence cannot be built from the numbers."""
    for pattern, repl in _PLAIN_TERMS:
        text = re.sub(pattern, repl, text)
    return text


def _and(items: list) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    last = ", and " if any(" and " in i for i in items) else " and "
    return ", ".join(items[:-1]) + last + items[-1]


def plain_change(line: str) -> str:
    """One diff line ("max_facts: 6 -> 10") in plain words ("use up to 10 facts (was 6)")."""
    line = str(line).strip()
    m = re.match(r"^include_kinds:\s*(.+)$", line)
    if m:
        added, dropped = [], []
        for part in m.group(1).split(","):
            part = part.strip()
            kind = part[1:].strip() if part[:1] in "+-" else part
            (dropped if part.startswith("-") else added).append(PLAIN_FACT_KIND.get(kind, kind))
        bits = []
        if added:
            bits.append("also include " + _and(added))
        if dropped:
            bits.append("leave out " + _and(dropped))
        return "; ".join(bits) or line
    m = re.match(r"^max_facts:\s*(\d+)\s*->\s*(\d+)$", line)
    if m:
        return f"use up to {m.group(2)} facts (was {m.group(1)})"
    m = re.match(r"^prefer_exposed:\s*(\w+)\s*->\s*(\w+)$", line)
    if m:
        return ("put the facts that affect this client first" if m.group(2).lower() == "true"
                else "stop putting the facts that affect this client first")
    m = re.match(r"^recency_days:\s*(\d+)\s*->\s*(\d+)$", line)
    if m:
        return f"only use facts from the last {m.group(2)} days (was {m.group(1)})"
    m = re.match(r"^likely_questions:\s*(\d+)\s*->\s*(\d+)$", line)
    if m:
        return f"list {m.group(2)} likely client questions (was {m.group(1)})"
    if line.startswith("section_order"):
        return "reorder the brief's sections"
    m = re.match(r"^(market_feed|account_notes|analyst_notes):\s*(\w+)\s*->\s*(\w+)$", line)
    if m:
        return ("turn on " if m.group(3).lower() == "true" else "turn off ") + PLAIN_SOURCE[m.group(1)]
    m = re.match(r"^heldout questions:\s*(.+)$", line)
    if m:
        return m.group(1).replace("rewrite them", "rewrite the unseen test meetings' questions")
    line = re.sub(r"^rule\b", "writing instruction", line)
    line = re.sub(r"^guardrail\b", "safety check", line)
    return plainify(line)


_REASON_PATTERNS = [
    (r"inside the [\d.]+ margin|needs at least \+", "margin"),
    (r"guardrail violations rose", "it failed more safety checks"),
    (r"context grew[^;]*?(\d+)\s*->\s*(\d+) tokens(?: \((\+\d+%))?", "facts size"),
    (r"context grew", "the facts given to the writer grew too much"),
    (r"false alarms rose", "it raised more false alarms (flagging news that doesn't affect the client)"),
    (r"worst scenario fell", "its worst unseen test meeting got worse"),
    (r"pass\^k fell", "it was less consistent across repeat runs"),
    (r"missed changes rose", "it missed more changes the client would ask about"),
    (r"stale claims rose", "it repeated more out-of-date facts"),
    (r"uncited claims rose", "it made more statements without a cited fact"),
]


def plain_reasons(decision: str) -> tuple:
    """(too_small_gain, [other reasons in plain words]) read from the gate's stored decision sentence."""
    text = decision or ""
    too_small, reasons = False, []
    for pattern, meaning in _REASON_PATTERNS:
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        if meaning == "margin":
            too_small = True
        elif meaning == "facts size":
            grew = f", {m.group(3)}" if m.group(3) else ""
            reasons.append(f"the facts given to the writer grew too much ({m.group(1)} -> {m.group(2)} tokens{grew}; the limit is +50%)")
        elif not (meaning == "the facts given to the writer grew too much" and any(r.startswith("the facts given to the writer grew too much (") for r in reasons)):
            reasons.append(meaning)
    return too_small, reasons


def _pct(value: Optional[float]) -> Optional[str]:
    return None if value is None else f"{round(value * 100)}%"


def _change_phrase(kind: Optional[str], field: Optional[str], key: Optional[str] = None) -> str:
    surface = PLAIN_SURFACE.get(kind or "", plainify(kind or "a setting"))
    where = (field or (key if key and key != "global" else "") or "").replace("_", " ")
    return f"the proposed change to {surface}" + (f" ({where})" if where else "")


def _scores(cand: Optional[float], champ: Optional[float], where: str = "unseen test meetings'") -> Optional[str]:
    if cand is None or champ is None:
        return None
    of = f"of {where} questions" if where else "of the questions"
    return f"the proposed version answered {_pct(cand)} {of} correctly vs {_pct(champ)} for the current version"


def _proposal_info(p: dict, ctx: dict) -> dict:
    """What the sentence needs about a proposal: from the payload, else from the proposals lookup in ctx."""
    info = dict((ctx.get("proposals") or {}).get(p.get("proposal_id")) or {})
    for key in ("kind", "field", "key", "tier"):
        if p.get(key) is not None:
            info[key] = p[key]
    return info


def _plain_seed(p, subject, ctx):
    seeded = p.get("seeded") or []
    kinds = sorted({str(s).split(":")[0] for s in seeded if ":" in str(s)}, key=lambda k: CONFIG_KIND_ORDER.get(k, 9))
    return (f"{subject} set up the first version of {len(seeded)} settings "
            f"({_and([PLAIN_SURFACE.get(k, k) for k in kinds])})") if seeded else None


def _plain_event(p, subject, ctx):
    if p.get("action") == "load_world":
        return (f"{subject} loaded {p.get('base_facts', 0)} starting facts and "
                f"{_plural(int(p.get('events', 0) or 0), 'scripted market event')} for "
                f"{_plural(len(p.get('fields') or []), 'client group')}")
    if p.get("action") == "fire_event" or p.get("event_id"):
        title = p.get("title") or ctx.get("event_titles", {}).get(p.get("event_id")) or p.get("event_id")
        group = f" for {str(p['field']).replace('_', ' ')} clients" if p.get("field") else ""
        return f"Market news{group}: {title} ({_plural(len(p.get('fact_ids') or []), 'new fact')})"
    return None


def _plain_brief(p, subject, ctx):
    versions = p.get("versions") if isinstance(p.get("versions"), dict) else {}
    text = f"{subject} wrote a call-prep brief for {p.get('account_id') or 'a client'}"
    if _date_part(p.get("as_of")):
        text += f" dated {_date_part(p.get('as_of'))}"
    text += f" from {_plural(len(p.get('fact_ids') or []), 'fact')}"
    if versions.get("policy") is not None:
        text += f", using version {versions['policy']} of what goes into the brief"
    return text


def _plain_feedback(p, subject, ctx):
    title = ctx.get("event_titles", {}).get(p.get("event_id")) or p.get("event_id") or "the market news"
    said = ctx.get("feedback_texts", {}).get(p.get("feedback_id"))
    if said:
        return f'After "{title}", the advisor said: "{str(said).strip()}"'
    return f'After "{title}", the advisor reported that the brief missed something the client asked about'


def _plain_proposal(p, subject, ctx):
    changes = [plain_change(d) for d in (p.get("diff") or [])]
    text = f"{subject} proposed a change to " + _change_phrase(p.get("kind"), p.get("field"), p.get("key"))[
        len("the proposed change to "):]
    return text + (f": {'; '.join(changes)}" if changes else "")


def _plain_eval(p, subject, ctx):
    info = _proposal_info(p, ctx)
    change = _change_phrase(info.get("kind"), info.get("field"), info.get("key"))
    heldout, tuning = p.get("heldout"), p.get("tuning")
    outcome = PLAIN_OUTCOME.get(p.get("outcome"), p.get("outcome") or "")
    tier = PLAIN_TIER.get(info.get("tier") or "")
    if tier and info.get("tier") != "X":
        change = change[:-1] + f"; {tier})" if change.endswith(")") else change + f" ({tier})"
    if isinstance(heldout, dict):
        scores = _scores(_acc(heldout.get("candidate")), _acc(heldout.get("champion")), "")
        if scores:
            return f"{subject} scored {change} on unseen test meetings: {scores}" + (f"; {outcome}" if outcome else "")
    if isinstance(tuning, dict):
        scores = _scores(_acc(tuning.get("candidate")), _acc(tuning.get("champion")), "")
        if scores:
            return (f"{subject} tried {change} on practice meetings: {scores}; it did not go on to the unseen test "
                    f"meetings" + (f"; {outcome}" if outcome else ""))
    return f"{subject} scored {change}" + (f": {outcome}" if outcome else "")


def _plain_commit(p, subject, ctx):
    info = _proposal_info(p, ctx)
    change = _change_phrase(p.get("kind") or info.get("kind"), info.get("field"), p.get("key"))
    owner = _owner(ctx.get("_actor", ""))
    text = f"{subject} adopted {change} as version {p.get('version', '?')}"
    if p.get("base_version") is not None:
        text += f" (was version {p['base_version']})"
    if owner:
        text += f", signed off by {owner}"
    scores = _scores(info.get("cand"), info.get("champ"))
    return text + (f": {scores}" if scores else "")


def _plain_reject(p, subject, ctx):
    info = _proposal_info(p, ctx)
    change = _change_phrase(info.get("kind"), info.get("field"), info.get("key"))
    decision = p.get("decision") or info.get("decision") or ""
    if p.get("status") == "stale":
        return f"{subject} set aside {change}: the settings it was tested against have changed since"
    too_small, reasons = plain_reasons(decision)
    scores = _scores(info.get("cand"), info.get("champ"))
    verb = f"{subject} rejected {change}"
    if scores:
        worse = info.get("cand") is not None and info.get("champ") is not None and info["cand"] < info["champ"]
        if too_small:
            tail = "a gain too small to trust" + (f", and {_and(reasons)}" if reasons else "")
        elif worse:
            tail = "worse than the current version" + (f", and {_and(reasons)}" if reasons else "")
        else:
            tail = f"but {_and(reasons)}" if reasons else ""
        return f"{verb}: {scores}" + (f", {tail}" if tail else "")
    if too_small or reasons:
        return f"{verb}: " + _and((["the gain was too small to trust"] if too_small else []) + reasons)
    return f"{verb}: {plainify(_strip_label(decision, 'Rejected'))}" if decision else verb


def _plain_refused(p, subject, ctx):
    if p.get("attempted"):
        what = plainify(str(p["attempted"]).replace("_", " "))
        proposer = ctx.get("_proposer") or DEFAULT_PROPOSER
        return f"Test gate stopped {proposer} from reading {what}: the proposer may only see practice meetings"
    if p.get("what") == "brief":
        violations = [str(v) for v in (p.get("violations") or [])]
        text = f"{subject} blocked a brief for {p.get('account_id') or 'a client'}"
        return text + (f": {_plural(len(violations), 'safety check')} failed, first: {violations[0]}"
                       if violations else "")
    info = _proposal_info(p, ctx)
    change = _change_phrase(info.get("kind"), info.get("field"), info.get("key"))
    if info.get("tier") == "X" or info.get("kind") == "scenarios":
        return f"{subject} refused {change}: it is never allowed, because it would change how the system is graded"
    reason = _strip_label(p.get("decision") or p.get("reason") or "", "Refused")
    return f"{subject} refused {change}" + (f": {plainify(reason)}" if reason else "")


def _plain_approve(p, subject, ctx):
    info = _proposal_info(p, ctx)
    return f"{subject} signed off {_change_phrase(info.get('kind'), info.get('field'), info.get('key'))}; it was adopted"


def _plain_rollback(p, subject, ctx):
    surface = PLAIN_SURFACE.get(p.get("kind") or "", p.get("kind") or "a setting")
    key = str(p.get("key") or "").replace("_", " ")
    return (f"{subject} rolled {surface}" + (f" ({key})" if key and key != "global" else "")
            + f" back to version {p.get('restores', '?')}'s content, saved as version {p.get('version', '?')}")


PLAIN_SUMMARIES: dict = {
    "seed": _plain_seed, "event": _plain_event, "brief": _plain_brief, "feedback": _plain_feedback,
    "proposal": _plain_proposal, "eval": _plain_eval, "commit": _plain_commit, "reject": _plain_reject,
    "refused": _plain_refused, "approve": _plain_approve, "rollback": _plain_rollback,
}


def plain_summary(kind: str, actor: str, payload: Any, ctx: Optional[dict] = None) -> str:
    """The judges' sentence. Built from the numbers when present; otherwise the technical sentence with the
    vocabulary swapped in."""
    ctx = dict(ctx or {})
    ctx["_actor"] = actor or ""
    label, subject = actor_names(actor or "", ctx.get("_proposer"))
    fn = PLAIN_SUMMARIES.get(kind)
    if fn is None:
        return f"{label} {kind}"
    text = None
    if fn is not None and isinstance(payload, dict):
        try:
            text = fn(payload, subject, ctx)
        except Exception:
            text = None
    if text:
        text = text[0].upper() + text[1:]
        return _sentence(text)
    return plainify(summarize(kind, actor, payload, ctx))


def activity_item(entry: dict, ctx: Optional[dict] = None) -> dict:
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    kind, actor = entry.get("kind") or "", entry.get("actor") or ""
    return {
        "seq": entry.get("seq"),
        "kind": kind,
        "actor": actor,
        "actor_label": actor_names(actor, (ctx or {}).get("_proposer"))[0],
        "sim_time": iso(entry.get("sim_time")),
        "recorded_at": iso(entry.get("recorded_at")),
        "field": entry_field(kind, payload, ctx),
        "summary": plain_summary(kind, actor, payload, ctx),
        "summary_technical": summarize(kind, actor, payload, ctx),
        "status": entry_status(kind, payload),
        "ref": entry_ref(kind, payload),
        "hash": entry.get("hash"),
        "prev_hash": entry.get("prev_hash"),
        "payload": jsonable(payload),
    }


def check_chain(entries: list) -> dict:
    """Every entry's prev_hash equals the previous entry's hash, in seq order (the first one's is GENESIS when it
    is seq 1). Seq gaps count as a break. No payload re-hashing."""
    ordered = sorted((e for e in entries if isinstance(e.get("seq"), int)), key=lambda e: e["seq"])
    links_ok, first_break, prev = True, None, None
    for e in ordered:
        if prev is None:
            ok = e.get("prev_hash") == GENESIS_HASH if e["seq"] == 1 else True
        else:
            ok = e["seq"] == prev["seq"] + 1 and e.get("prev_hash") == prev.get("hash")
        if not ok:
            links_ok, first_break = False, e["seq"]
            break
        prev = e
    last = ordered[-1] if ordered else None
    return {"entries": len(ordered), "links_ok": links_ok, "first_break_seq": first_break,
            "last_seq": last["seq"] if last else 0, "last_hash": last.get("hash") if last else None}


# ---------------------------------------------------------------------------------------------------------------
# Versions, cabinet timeline, findings (pure helpers)
# ---------------------------------------------------------------------------------------------------------------
def changed_keys(prev_body: Any, body: Any) -> list:
    """What changed from the previous version of the same kind:key: dict keys, or ids of list items (rules,
    guardrails). The first version has no previous one, so nothing changed."""
    if prev_body is None:
        return []
    if isinstance(prev_body, dict) and isinstance(body, dict):
        return sorted(k for k in set(prev_body) | set(body) if prev_body.get(k) != body.get(k))
    if isinstance(prev_body, list) and isinstance(body, list):
        def by_id(items: list) -> Optional[dict]:
            out = {}
            for item in items:
                if not isinstance(item, dict) or "id" not in item:
                    return None
                out[str(item["id"])] = item
            return out
        a, b = by_id(prev_body), by_id(body)
        if a is not None and b is not None:
            changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            if not changed and [str(i["id"]) for i in prev_body] != [str(i["id"]) for i in body]:
                return ["order"]
            return changed
    return [] if prev_body == body else ["body"]


def norm_date(value: Any) -> Optional[str]:
    """YYYY-MM-DD from a datetime (UTC) or a date string (notes and preps store strings, events datetimes)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s[:10] or None


_FEED_SKIP = {"date", "client_id", "type", "event_id", "_id", "id"}
_FEED_ORDER = ("instrument", "instrument_type", "tool", "document", "topic", "email", "sender", "user", "text",
               "counterparty", "amount", "days", "note", "result")
_NOTABLE_TYPES = {"trade_buy", "trade_sell", "tool_used", "email_to_banker", "document_requested", "reply_lag"}


def _fmt_value(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(jsonable(v))


def feed_text(event: dict) -> str:
    """'trade_sell instrument=Balanced Growth Fund, instrument_type=fund, amount=110000'"""
    keys = [k for k in _FEED_ORDER if k in event] + sorted(k for k in event if k not in _FEED_ORDER)
    parts = [f"{k}={_fmt_value(event[k])}" for k in keys if k not in _FEED_SKIP and event[k] is not None]
    return (str(event.get("type") or "event") + (" " + ", ".join(parts) if parts else "")).strip()


def is_newsletter(email: str) -> bool:
    return bool(re.search(r"newsletter|market letter|monthly letter|weekly letter", (email or "").lower()))


def is_notable(event: dict) -> bool:
    """Trades, tool use, client emails, document requests, reply lags, tech-stock articles and
    non-newsletter emails. Logins, cash flows, other articles and newsletters are routine."""
    etype = str(event.get("type") or "")
    if etype in _NOTABLE_TYPES or etype.startswith("trade_"):
        return True
    if etype == "article_read":
        return "tech" in str(event.get("topic") or "").lower()
    if etype.startswith("email_"):
        return not is_newsletter(str(event.get("email") or event.get("topic") or ""))
    return False


_FAULT_KEYS = ("fault_id", "fault_type", "level", "attribute", "claimed", "truth", "fix", "evidence", "severity",
               "claimed_text", "as_of")
_EXPECTED_KEYS = ("attribute", "action", "note_id", "reason", "evidence")


def fault_item(doc: dict) -> dict:
    return {k: jsonable(doc[k]) for k in _FAULT_KEYS if doc.get(k) is not None}


def expected_item(doc: dict) -> dict:
    """An expected action from cabinet_truth.expected_actions: attribute, action (ask | flag | brief_both), and
    note_id (flags), reason and evidence when present."""
    return {k: jsonable(doc[k]) for k in _EXPECTED_KEYS if doc.get(k) not in (None, "")}


def _first_value(doc: dict, exact: tuple, pattern: Optional[str] = None) -> Any:
    for key in exact:
        if doc.get(key) not in (None, ""):
            return doc[key]
    if pattern:
        for key, value in doc.items():
            if key != "_id" and re.search(pattern, str(key), re.I) and value not in (None, ""):
                return value
    return None


def harness_prep_item(doc: dict) -> dict:
    """A harness-written prep (the pregame db's cabinet_preps, shape not fixed): a few normalised keys for the
    timeline, and every original field under `fields`."""
    raw = clean(doc)
    text = _first_value(doc, ("text", "markdown", "prep", "prep_text", "body", "content"))
    if text is not None and not isinstance(text, str):
        text = json.dumps(jsonable(text), ensure_ascii=False)
    date = norm_date(_first_value(doc, ("date", "as_of", "prep_date", "call_date", "created_sim", "created_at")))
    return {
        "date": date, "kind": "harness_prep", "id": str(raw.get("id") or ""),
        "client_id": jsonable(_first_value(doc, ("client_id", "client"))),
        "prep_id": jsonable(_first_value(doc, ("prep_id", "baseline_prep_id", "source_prep_id"))),
        "text": text,
        "policy": jsonable(_first_value(doc, ("policy", "policy_version", "policy_id"), r"polic")),
        "label": jsonable(_first_value(doc, ("label", "config_label", "variant", "arm"), r"label")),
        "run_id": jsonable(_first_value(doc, ("run_id", "run"), r"(^|_)run($|_)")),
        "fields": raw,
    }


def build_timeline(notes: list, preps: list, events: list, faults_by_doc: Optional[dict] = None,
                   expected_by_prep: Optional[dict] = None, harness_preps: Optional[list] = None) -> list:
    """Notes, preps, harness preps and every feed event on one timeline sorted by date, then kind (feed, note, prep,
    harness_prep), then id. With `faults_by_doc` (the answer side, keyed by doc_id), notes and preps carry `faults`,
    and preps and harness preps carry `expected` (expected actions keyed by prep_id)."""
    answer = faults_by_doc is not None
    expected_by_prep = expected_by_prep or {}
    items = []
    for ev in events:
        items.append({"date": norm_date(ev.get("date")), "kind": "feed",
                      "id": str(ev.get("event_id") or jsonable(ev.get("_id")) or ""),
                      "type": ev.get("type"), "instrument_type": ev.get("instrument_type"),
                      "text": feed_text(ev), "notable": is_notable(ev)})
    for n in notes:
        item = {"date": norm_date(n.get("date")), "kind": "note", "id": str(n.get("note_id") or jsonable(n.get("_id"))),
                "author": n.get("author"), "text": n.get("text")}
        if answer:
            item["faults"] = [fault_item(f) for f in faults_by_doc.get(item["id"], [])]
        items.append(item)
    for p in preps:
        item = {"date": norm_date(p.get("date")), "kind": "prep", "id": str(p.get("prep_id") or jsonable(p.get("_id"))),
                "text": p.get("text"), "used_note_ids": jsonable(p.get("used_note_ids") or [])}
        if answer:
            item["faults"] = [fault_item(f) for f in faults_by_doc.get(item["id"], [])]
            item["expected"] = [expected_item(x) for x in expected_by_prep.get(item["id"], [])]
        items.append(item)
    for h in harness_preps or []:
        item = harness_prep_item(h)
        if answer:
            item["expected"] = [expected_item(x) for x in expected_by_prep.get(str(item.get("prep_id") or ""), [])]
        items.append(item)
    items.sort(key=lambda i: (i["date"] or "", KIND_ORDER.get(i["kind"], 9), i["id"]))
    return items


def strip_faults(timeline: list) -> list:
    """The presenter toggle off: no faults and no expected actions."""
    return [{k: v for k, v in item.items() if k not in ("faults", "expected")} for item in timeline]


def _load_json_file(path: Path, errors: list) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return None


def _prep_rows(scorer: Any) -> list:
    rows = []
    for p in (scorer or {}).get("preps") or []:
        if not isinstance(p, dict):
            continue
        faults = [f for f in p.get("faults") or [] if isinstance(f, dict)]
        rows.append({"prep_id": p.get("prep_id"), "client_id": p.get("client_id"), "date": norm_date(p.get("date")),
                     "fault_types": [f.get("fault_type") for f in faults],
                     "warnings": sum(1 for f in faults if f.get("level") == "warning"),
                     "questions_expected": len(p.get("questions_expected") or []),
                     "questions_hit": len(p.get("questions_hit") or []),
                     "expected_actions": len(p.get("expected_actions") or []),
                     "actions_hit": len(p.get("actions_hit") or [])})
    return rows


def _faults_by_client(scorer: Any) -> Optional[dict]:
    if not isinstance(scorer, dict):
        return None
    summary = scorer.get("summary") if isinstance(scorer.get("summary"), dict) else scorer
    if isinstance(summary.get("faulty_preps_by_client"), dict):
        return summary["faulty_preps_by_client"]
    rows = _prep_rows(scorer)
    if not rows:
        return None
    out: dict = {}
    for r in rows:
        faulty, total = out.get(r["client_id"], (0, 0))
        out[r["client_id"]] = (faulty + (1 if r["fault_types"] else 0), total + 1)
    return {c: f"{f}/{t}" for c, (f, t) in sorted(out.items())}


def findings_from_files(data_dir: Path) -> dict:
    """The file-backed part of /api/findings (read fresh on every request, online or offline). The scorer's
    `summary` objects pass through unchanged."""
    errors: list = []
    baseline = _load_json_file(data_dir / "cabinet_baseline.json", errors)
    harness = _load_json_file(data_dir / "cabinet_harness.json", errors)
    research = _load_json_file(data_dir / "research_findings.json", errors)

    def summary_of(scorer: Any) -> Any:
        if not isinstance(scorer, dict):
            return None
        return jsonable(scorer.get("summary") if isinstance(scorer.get("summary"), dict) else scorer)

    cabinet = {
        "baseline": summary_of(baseline),
        "harness": summary_of(harness),
        "baseline_faults_by_client": jsonable(_faults_by_client(baseline)),
        "harness_faults_by_client": jsonable(_faults_by_client(harness)),
        "baseline_source": baseline.get("source") if isinstance(baseline, dict) else None,
        "harness_source": harness.get("source") if isinstance(harness, dict) else None,
        "baseline_preps": _prep_rows(baseline) if isinstance(baseline, dict) else [],
        "harness_preps": _prep_rows(harness) if isinstance(harness, dict) else [],
    }
    return {"cabinet": cabinet, "research": jsonable(research), "errors": errors}


RUN_INFO_KEYS = ("label", "mode", "proposer", "proposer_model", "writer_model", "grader_model", "stage",
                 "recorded_from", "replayable", "note")


def load_run_info(data_dir: Path) -> dict:
    """Optional data/run_info.json: {"<db>": {"label", "mode", "proposer", "proposer_model", "writer_model",
    "grader_model", "stage", "recorded_from", "replayable", "note"}} (a plain string is taken as the label). The
    databases record only the writer's model (briefs.model), so the run's setup and which run is on stage are
    stated here."""
    data = _load_json_file(data_dir / "run_info.json", [])
    out: dict = {}
    for db, value in (data.items() if isinstance(data, dict) else []):
        if str(db).startswith("_"):  # comments such as "_note"
            continue
        if isinstance(value, str):
            value = {"label": value}
        if isinstance(value, dict):
            out[str(db)] = {k: value.get(k) for k in RUN_INFO_KEYS if k in value}
    return out


def llm_mode_for(db: str, cfg: dict, run_info: dict) -> tuple:
    """(llm_mode, source): the database's own mode from data/run_info.json (e.g. replay, recorded), else
    PREGAME_LLM_MODE from viewer.env."""
    mode = (run_info.get(db) or {}).get("mode")
    if mode:
        return str(mode), "run_info"
    return (cfg.get("PREGAME_LLM_MODE") or None), "env"


def has_cabinet_data(row: dict) -> bool:
    return bool(row.get("cabinet_preps") or row.get("cabinet_runs"))


def pick_cabinet_source(db: str, rows: list) -> tuple:
    """(source_db, fallback) for harness preps and cabinet_runs: the database itself when it has cabinet_preps (or
    cabinet_runs), else the most recently written pregame database that has them, else the database itself."""
    own = next((r for r in rows if r.get("name") == db), None)
    if own is not None and has_cabinet_data(own):
        return db, False
    candidates = [r for r in rows if has_cabinet_data(r)]
    if not candidates:
        return db, False
    best = max(candidates, key=lambda r: (r.get("cabinet_written_at") or "", r.get("name") or ""))
    return best["name"], best["name"] != db


# Public copy: nothing from cabinet_truth (faults, fault counts, expected actions, truth, claims), no per-client or
# per-prep scoring rows, no local paths. Summaries (the scorer's totals per writer) are kept.
PER_ROW_KEYS = {"faulty_preps_by_client", "baseline_faults_by_client", "harness_faults_by_client", "baseline_preps",
                "harness_preps"}
_LOCAL_PATH = re.compile(r"(?:\b[A-Za-z]:[\\/]|/(?:Users|home)/)(?:[^\\/\r\n\"<>|:*?]+[\\/])+")
PUBLIC_RUN_KEYS = ("id", "created_at", "data_source", "policy", "prep_count", "preps_collection", "scores")


def scrub_local_paths(value: Any) -> Any:
    """Drop the directory part of any local absolute path inside strings ("C:\\x\\y\\score.py" -> "score.py")."""
    if isinstance(value, str):
        return _LOCAL_PATH.sub("", value)
    if isinstance(value, dict):
        return {k: scrub_local_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_local_paths(v) for v in value]
    return value


def public_summary(summary: Any) -> Any:
    """A scorer summary without its per-client rows."""
    if not isinstance(summary, dict):
        return summary
    return {k: v for k, v in summary.items() if k not in PER_ROW_KEYS}


def public_cabinet_run(doc: Any) -> Any:
    if not isinstance(doc, dict):
        return doc
    out = {k: doc[k] for k in PUBLIC_RUN_KEYS if k in doc}
    if isinstance(out.get("scores"), dict):
        out["scores"] = {k: public_summary(v) for k, v in out["scores"].items()}
    return scrub_local_paths(out)


def public_findings(findings: dict) -> dict:
    """Findings for the public copy: cabinet summaries only (baseline / naive / harness), no per-prep or per-client
    rows, no local paths."""
    out = dict(findings)
    cab = findings.get("cabinet") or {}
    out["cabinet"] = {"baseline": public_summary(cab.get("baseline")), "harness": public_summary(cab.get("harness")),
                      "baseline_source": cab.get("baseline_source"), "harness_source": cab.get("harness_source")}
    runs = findings.get("cabinet_runs")
    out["cabinet_runs"] = [public_cabinet_run(d) for d in runs] if runs else None
    out["public"] = True
    return scrub_local_paths(out)


def now_iso() -> str:
    return iso(dt.datetime.now(dt.timezone.utc))


def _int_param(query: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = (query.get(name) or [None])[0]
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"{name} must be a whole number")
    return max(lo, min(hi, value))


_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
PREGAME_PREFIX = "pregame_"


def is_pregame_name(name: str) -> bool:
    return bool(name) and name.startswith(PREGAME_PREFIX) and bool(_SAFE_NAME.match(name))


def check_collection_name(coll: str) -> None:
    if not _SAFE_NAME.match(coll or "") or coll.startswith("system."):
        raise ApiError(403, f"collection {coll!r} is not browsable")


# The Pregame databases the viewer offers, in this order (scratch and re-grade databases are left out).
# VIEWER_PREGAME_DBS in viewer.env overrides it (comma-separated; "*" shows every pregame_* database).
SHOWN_PREGAME_DBS = ("pregame_demo", "pregame_stage", "pregame_run_a", "pregame_run_b", "pregame_run_c",
                     "pregame_cabinet_live", "pregame_v3_cabinet20")


def shown_pregame_dbs(found: list, cfg: dict) -> list:
    """The discovered pregame_* databases the switcher shows: the configured list, in its order."""
    raw = (cfg.get("VIEWER_PREGAME_DBS") or "").strip()
    if raw == "*":
        return sorted(found, key=run_order)
    wanted = [n.strip() for n in raw.split(",") if n.strip()] if raw else list(SHOWN_PREGAME_DBS)
    return [n for n in wanted if n in found]


def run_order(name: str) -> tuple:
    """pregame_demo first, then pregame_run_* in order, then the rest."""
    return (0 if name == "pregame_demo" else 1 if name.startswith("pregame_run_") else 2, name)


def _strip_per_scenario(summary: Any) -> Any:
    if isinstance(summary, dict):
        return {k: v for k, v in summary.items() if k != "per_scenario"}
    return summary


def run_summary(db: str, ledger: list, proposals: list, models: list, info: Optional[dict] = None) -> dict:
    """One row of /api/runs from a pregame database's ledger (seq, hash, prev_hash, kind, recorded_at), proposals
    (oldest first) and distinct brief models."""
    chain = check_chain(ledger)
    by_status = {s: 0 for s in PROPOSAL_STATUSES}
    for p in proposals:
        by_status[str(p.get("status"))] = by_status.get(str(p.get("status")), 0) + 1
    by_status["awaiting_approval"] = by_status.get("awaiting_owner", 0)
    recorded = [e.get("recorded_at") for e in ledger if isinstance(e.get("recorded_at"), dt.datetime)]
    committed, rejected = [], []
    for p in proposals:
        if p.get("status") == "committed":
            committed.append(jsonable({
                "id": p.get("_id"), "field": p.get("field"), "kind": p.get("kind"), "diff": p.get("diff") or [],
                "decision": p.get("decision"),
                "heldout_champion": _strip_per_scenario(p.get("heldout_champion")),
                "heldout_candidate": _strip_per_scenario(p.get("heldout_candidate"))}))
        elif p.get("status") in ("rejected", "stale") and p.get("tier") != "X":
            rejected.append(jsonable({
                "id": p.get("_id"), "field": p.get("field"), "kind": p.get("kind"), "tier": p.get("tier"),
                "status": p.get("status"), "diff": p.get("diff") or [], "decision": p.get("decision"),
                "heldout_champion_mean": _acc(p.get("heldout_champion")),
                "heldout_candidate_mean": _acc(p.get("heldout_candidate"))}))
    champion = [_acc(p.get("heldout_champion")) for p in proposals if _acc(p.get("heldout_champion")) is not None]
    info = info or {}
    return {
        "db": db, "label": info.get("label"), "mode": info.get("mode"), "proposer": info.get("proposer"),
        "proposer_model": info.get("proposer_model"), "writer_model": info.get("writer_model"),
        "grader_model": info.get("grader_model"), "stage": bool(info.get("stage")),
        "recorded_from": info.get("recorded_from"), "replayable": info.get("replayable"), "note": info.get("note"),
        "chain": {"entries": chain["entries"], "links_ok": chain["links_ok"], "last_seq": chain["last_seq"]},
        "proposals_by_status": by_status,
        "models": sorted(str(m) for m in models if m),
        "first_at": iso(min(recorded)) if recorded else None,
        "last_at": iso(max(recorded)) if recorded else None,
        "committed": committed,
        "rejected": rejected,
        "refused": sum(1 for e in ledger if e.get("kind") == "refused"),
        "champion_mean_accuracy": champion,
    }


class TTLCache:
    """Tiny time-based cache for the database list and per-database counts (the page polls every 2 s)."""

    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key: Any, ttl: float, compute: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = compute()
        with self._lock:
            self._data[key] = (now + ttl, value)
        return value


# ---------------------------------------------------------------------------------------------------------------
# Live source: builds every endpoint from MongoDB (read-only)
# ---------------------------------------------------------------------------------------------------------------
DB_COUNT_COLLECTIONS = ("ledger", "proposals", "briefs", "cabinet_preps", "cabinet_runs")


class LiveSource:
    offline = False

    def __init__(self, cfg: dict, data_dir: Path = DATA_DIR, secrets: Optional[Secrets] = None):
        self.cfg = cfg
        self.data_dir = data_dir
        self.secrets = secrets or Secrets(cfg.get("MONGODB_URI", ""))
        self.ro = ReadOnlyMongo(cfg.get("MONGODB_URI", ""), int(cfg.get("VIEWER_DB_TIMEOUT_MS") or 6000),
                                self.secrets)
        self.default_db = cfg.get("PREGAME_DB") or DEFAULT_PREGAME_DB
        self.cache = TTLCache()

    # -- databases ------------------------------------------------------------------------------------------------
    def pregame_dbs(self) -> list:
        """Every database whose name starts with pregame_, discovered at runtime (cached 30 s), plus PREGAME_DB."""
        def discover() -> list:
            names = self.ro.databases()
            found = shown_pregame_dbs([n for n in (names or []) if is_pregame_name(n)], self.cfg)
            if self.default_db not in found:
                found.insert(0, self.default_db)
            return found
        return self.cache.get("pregame_dbs", 30.0, discover)

    def resolve_db(self, name: Optional[str]) -> str:
        if not name or name == self.default_db:
            return self.default_db
        if is_pregame_name(name) and name in self.pregame_dbs():
            return name
        raise ApiError(403, f"database {name!r} is not a Pregame database the viewer reads",
                       hint="db must name a database that starts with pregame_ (see overview.pregame_dbs).")

    def allowed(self) -> list:
        out = []
        for name in self.pregame_dbs() + list(OTHER_DBS):
            if name not in out:
                out.append(name)
        return out

    def check_browsable(self, db: str, coll: str) -> None:
        # Decide the obvious cases without touching the database, so a refusal never waits on Atlas.
        if db not in OTHER_DBS and db != self.default_db:
            if not is_pregame_name(db) or db not in self.pregame_dbs():
                raise ApiError(403, f"database {db!r} is not one the viewer reads",
                               hint="Allowed: every pregame_* database, cabinet, cabinet_truth, harness.")
        check_collection_name(coll)

    def pregame_db_counts(self) -> list:
        """[{name, ledger, proposals, briefs, cabinet_preps, cabinet_runs, cabinet_written_at}] for every pregame
        database (cached 5 s). Not to be called from inside a _parallel job."""
        def compute() -> list:
            names = self.pregame_dbs()
            ro = self.ro
            counts = self._parallel({(n, c): (lambda d, cc: lambda: ro.count(d, cc))(n, c)
                                     for n in names for c in DB_COUNT_COLLECTIONS})
            rows = [{"name": n, **{c: counts[(n, c)] for c in DB_COUNT_COLLECTIONS}} for n in names]
            written = self._parallel({r["name"]: (lambda d: lambda: self._cabinet_written_at(d))(r["name"])
                                      for r in rows if has_cabinet_data(r)})
            for r in rows:
                r["cabinet_written_at"] = written.get(r["name"])
            return rows
        return self.cache.get("pregame_db_counts", 5.0, compute)

    def _cabinet_written_at(self, db: str) -> Optional[str]:
        """When the database's cabinet data was last written: the newest created_at, or the newest ObjectId's
        time, over cabinet_runs and cabinet_preps."""
        times = []
        for coll in ("cabinet_runs", "cabinet_preps"):
            docs = self.ro.find(db, coll, {}, {"_id": 1, "created_at": 1}, sort=[("_id", -1)], limit=1)
            docs += self.ro.find(db, coll, {"created_at": {"$type": "date"}}, {"created_at": 1},
                                 sort=[("created_at", -1)], limit=1)
            for d in docs:
                if isinstance(d.get("created_at"), dt.datetime):
                    times.append(d["created_at"])
                if isinstance(getattr(d.get("_id"), "generation_time", None), dt.datetime):
                    times.append(d["_id"].generation_time)
        return max(iso(t) for t in times) if times else None

    def cabinet_source(self, db: str, override: Optional[str] = None) -> tuple:
        """(source_db, fallback) for harness preps and cabinet_runs; ?cabinet_db= overrides (validated)."""
        if override:
            return self.resolve_db(override), False
        return pick_cabinet_source(db, self.pregame_db_counts())

    # -- helpers --------------------------------------------------------------------------------------------------
    @staticmethod
    def _parallel(jobs: dict) -> dict:
        """Run independent reads at once (each is an Atlas round trip). Jobs must not call _parallel themselves."""
        futures = {name: _DB_POOL.submit(fn) for name, fn in jobs.items()}
        return {name: future.result() for name, future in futures.items()}

    def _summary_ctx(self, db: str) -> dict:
        """Lookups the activity sentences need: event titles, feedback texts, and each proposal's kind, field, tier
        and held-out means (a reject entry carries only the decision text)."""
        titles = {e.get("_id"): e.get("title") for e in self.ro.find(db, "events", {}, {"title": 1})}
        feedback = self.ro.find(db, "feedback", {}, {"text": 1, "field": 1})
        proposals = {}
        for p in self.ro.find(db, "proposals", {}, {"kind": 1, "field": 1, "key": 1, "tier": 1, "decision": 1,
                                                   "heldout_candidate.mean_accuracy": 1,
                                                   "heldout_champion.mean_accuracy": 1}):
            proposals[p.get("_id")] = {"kind": p.get("kind"), "field": p.get("field"), "key": p.get("key"),
                                       "tier": p.get("tier"), "decision": p.get("decision"),
                                       "cand": _acc(p.get("heldout_candidate")),
                                       "champ": _acc(p.get("heldout_champion"))}
        return {"event_titles": titles, "feedback_texts": {f.get("_id"): f.get("text") for f in feedback},
                "feedback_fields": {f.get("_id"): f.get("field") for f in feedback}, "proposals": proposals,
                "_proposer": load_run_info(self.data_dir).get(db, {}).get("proposer")}

    # -- Pregame endpoints (db already resolved) ------------------------------------------------------------------
    def overview(self, db: str) -> dict:
        ro = self.ro
        latest_kinds = ("commit", "reject", "refused", "brief")
        jobs: dict = {
            "clock": lambda: ro.find(db, "clock", {"_id": "sim"}, limit=1) or ro.find(db, "clock", {}, limit=1),
            "chain": lambda: ro.find(db, "ledger", {}, {"seq": 1, "hash": 1, "prev_hash": 1}, sort=[("seq", 1)]),
            "proposals": lambda: ro.count(db, "proposals"),
            "briefs": lambda: ro.count(db, "briefs"),
            "feedback": lambda: ro.count(db, "feedback"),
            "facts": lambda: ro.count(db, "facts"),
            "events_fired": lambda: ro.count(db, "events", {"fired": True}),
            "eval_runs": lambda: ro.count(db, "eval_runs"),
            "by_status": lambda: ro.aggregate(db, "proposals", [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]),
            "ctx": lambda: self._summary_ctx(db),
        }
        for kind in latest_kinds:
            jobs["latest:" + kind] = (lambda k: lambda: ro.find(db, "ledger", {"kind": k}, sort=[("seq", -1)],
                                                                limit=1))(kind)
        r = self._parallel(jobs)
        pregame_dbs = self.pregame_db_counts()
        chain_rows, clock, ctx = r["chain"], r["clock"], r["ctx"]
        counts = {"ledger": len(chain_rows)}
        counts.update({k: r[k] for k in ("proposals", "briefs", "feedback", "facts", "events_fired", "eval_runs")})
        by_status = {s: 0 for s in PROPOSAL_STATUSES}
        for row in r["by_status"]:
            by_status[str(row.get("_id"))] = int(row.get("n", 0))
        by_status["awaiting_approval"] = by_status.get("awaiting_owner", 0)
        latest = {k: (activity_item(r["latest:" + k][0], ctx) if r["latest:" + k] else None) for k in latest_kinds}
        llm_mode, llm_mode_source = llm_mode_for(db, self.cfg, load_run_info(self.data_dir))
        return {
            "generated_at": now_iso(), "offline": False, "snapshot_at": None, "public": False,
            "pregame_db": db, "active_db": db, "default_db": self.default_db,
            "pregame_dbs": pregame_dbs,
            "sim_time": iso(clock[0].get("now")) if clock else None,
            "llm_mode": llm_mode, "llm_mode_source": llm_mode_source,
            "provider": self.cfg.get("PREGAME_PROVIDER") or None,
            "counts": counts,
            "chain": check_chain(chain_rows),
            "proposals_by_status": by_status,
            "latest": latest,
        }

    def activity(self, db: str, after_seq: int = 0, limit: int = 300) -> dict:
        rows = self.ro.find(db, "ledger", {"seq": {"$gt": after_seq}}, sort=[("seq", -1)], limit=limit)
        rows.reverse()
        last = self.ro.find(db, "ledger", {}, {"seq": 1}, sort=[("seq", -1)], limit=1)
        ctx = self._summary_ctx(db) if rows else {}
        return {"db": db, "items": [activity_item(r, ctx) for r in rows], "last_seq": last[0]["seq"] if last else 0}

    def proposals(self, db: str) -> list:
        docs = self.ro.find(db, "proposals", {}, sort=[("created_at", -1), ("_id", -1)])
        committed = {v.get("proposal_id"): v.get("version") for v in
                     self.ro.find(db, "config_versions", {"proposal_id": {"$ne": None}},
                                  {"proposal_id": 1, "version": 1})}
        out = []
        for d in docs:
            pid = d.get("_id")
            out.append(jsonable({
                "id": pid, "field": d.get("field"), "kind": d.get("kind"), "key": d.get("key"),
                "tier": d.get("tier"), "status": d.get("status"), "filed_by": d.get("filed_by"),
                "created_sim": d.get("created_sim"), "created_at": d.get("created_at"),
                "base_version": d.get("base_version"), "rationale": d.get("rationale"),
                "diff": d.get("diff") or [], "evidence": d.get("evidence") or [], "decision": d.get("decision"),
                "tuning": d.get("tuning"), "heldout_candidate": d.get("heldout_candidate"),
                "heldout_champion": d.get("heldout_champion"),
                "committed_version": d.get("committed_version") if d.get("committed_version") is not None
                else committed.get(pid),
                "evaluated_versions": d.get("evaluated_versions"),
                "body": d.get("body"),
            }))
        return out

    def versions(self, db: str) -> dict:
        heads = []
        for h in self.ro.find(db, "config_heads", {}):
            hid = str(h.get("_id"))
            kind, _, key = hid.partition(":")
            heads.append({"id": hid, "kind": kind, "key": key, "version": h.get("version")})
        heads.sort(key=lambda h: (CONFIG_KIND_ORDER.get(h["kind"], 9), h["key"]))
        docs = self.ro.find(db, "config_versions", {})
        docs.sort(key=lambda d: (str(d.get("kind")), str(d.get("key")), d.get("version") or 0))
        prev: dict = {}
        rows = []
        for d in docs:
            k = (d.get("kind"), d.get("key"))
            rows.append(jsonable({
                "id": d.get("_id"), "kind": d.get("kind"), "key": d.get("key"), "version": d.get("version"),
                "body": d.get("body"), "rationale": d.get("rationale"), "proposal_id": d.get("proposal_id"),
                "approved_by": d.get("approved_by"), "supersedes": d.get("supersedes"),
                "restores": d.get("restores"), "created_sim": d.get("created_sim"),
                "created_at": d.get("created_at"),
                "changed_keys": changed_keys(prev.get(k), d.get("body")) if k in prev else [],
            }))
            prev[k] = d.get("body")
        rows.sort(key=lambda r: (r.get("created_at") or "", r.get("version") or 0), reverse=True)
        return {"heads": heads, "versions": rows}

    def briefs(self, db: str, limit: int = 20) -> list:
        order = {}
        for e in self.ro.find(db, "ledger", {"kind": "brief"}, {"seq": 1, "payload.brief_id": 1}):
            bid = (e.get("payload") or {}).get("brief_id")
            if bid:
                order[bid] = e.get("seq") or 0
        heads = self.ro.find(db, "briefs", {}, {"as_of": 1})
        heads.sort(key=lambda b: (order.get(b.get("_id"), 0), iso(b.get("as_of")) or ""), reverse=True)
        ids = [b["_id"] for b in heads[:limit]]
        docs = {d["_id"]: d for d in self.ro.find(db, "briefs", {"_id": {"$in": ids}})} if ids else {}
        out = []
        for bid in ids:
            d = docs.get(bid)
            if not d:
                continue
            out.append(jsonable({
                "id": d.get("_id"), "seq": order.get(bid), "field": d.get("field"),
                "account_id": d.get("account_id"), "as_of": d.get("as_of"), "model": d.get("model"),
                "config_label": d.get("config_label"), "markdown": d.get("markdown"),
                "sections": d.get("sections") or {}, "receipt": d.get("receipt"),
                "dropped_claims": d.get("dropped_claims") or [],
            }))
        return out

    def world(self, db: str) -> dict:
        events = []
        for e in self.ro.find(db, "events", {}, sort=[("at", 1), ("_id", 1)]):
            events.append(jsonable({
                "id": e.get("id") or e.get("_id"), "field": e.get("field"), "title": e.get("title"),
                "month": e.get("month"), "at": e.get("at"), "fired": bool(e.get("fired")),
                "fired_at": e.get("fired_at"), "feedback": e.get("feedback"),
                "fact_count": len(e.get("facts") or []),
            }))
        feedback = [jsonable({"id": f.get("_id"), "field": f.get("field"), "brief_id": f.get("brief_id"),
                              "event_id": f.get("event_id"), "text": f.get("text"), "sim_time": f.get("sim_time")})
                    for f in self.ro.find(db, "feedback", {}, sort=[("sim_time", 1), ("_id", 1)])]
        facts_by_field = {str(r.get("_id")): int(r.get("n", 0)) for r in
                          self.ro.aggregate(db, "facts", [{"$group": {"_id": "$field", "n": {"$sum": 1}}},
                                                          {"$sort": {"_id": 1}}])}
        return {"events": events, "feedback": feedback, "facts_by_field": facts_by_field}

    def pregame_eval(self, db: str) -> list:
        rows = self.ro.find(db, "eval_runs", {"row": "summary"}, {"failures": 0},
                            sort=[("created_at", 1), ("_id", 1)])
        if not rows:  # older rows may not carry `row`
            rows = self.ro.find(db, "eval_runs", {"summary": {"$exists": True}}, {"failures": 0},
                                sort=[("created_at", 1), ("_id", 1)])
        return [jsonable({"field": r.get("field"), "split": r.get("split"), "config_label": r.get("config_label"),
                          "k": r.get("k"), "summary": r.get("summary"), "created_at": r.get("created_at")})
                for r in rows]

    def cabinet_runs(self, db: str) -> Optional[list]:
        docs = self.ro.find(db, "cabinet_runs", {}, sort=[("_id", -1)], limit=20)
        return [trim_for_browser(clean(d)) for d in docs] or None

    def findings(self, db: str, cabinet_db: Optional[str] = None) -> dict:
        out = findings_from_files(self.data_dir)
        out["db"] = db
        try:
            source, fallback = self.cabinet_source(db, cabinet_db)
            out["cabinet_source_db"], out["cabinet_source_fallback"] = source, fallback
            r = self._parallel({"eval": lambda: self.pregame_eval(db), "runs": lambda: self.cabinet_runs(source)})
            out["pregame_eval"], out["cabinet_runs"] = r["eval"], r["runs"]
        except DBUnavailable as exc:
            out["pregame_eval"], out["cabinet_runs"] = [], None
            out["cabinet_source_db"], out["cabinet_source_fallback"] = None, False
            out["pregame_eval_error"] = exc.payload.get("error")
        return out

    # -- runs -----------------------------------------------------------------------------------------------------
    def runs(self) -> dict:
        names = [n for n in self.pregame_dbs() if is_pregame_name(n)]
        ro = self.ro
        jobs: dict = {}
        for n in names:
            jobs[(n, "ledger")] = (lambda d: lambda: ro.find(d, "ledger", {}, {"seq": 1, "hash": 1, "prev_hash": 1,
                                                                             "kind": 1, "recorded_at": 1},
                                                             sort=[("seq", 1)]))(n)
            jobs[(n, "proposals")] = (lambda d: lambda: ro.find(d, "proposals", {}, {"body": 0, "tuning": 0},
                                                                sort=[("created_at", 1), ("_id", 1)]))(n)
            jobs[(n, "models")] = (lambda d: lambda: ro.aggregate(d, "briefs", [{"$group": {"_id": "$model"}}]))(n)
        r = self._parallel(jobs)
        info = load_run_info(self.data_dir)
        rows = [run_summary(n, r[(n, "ledger")], r[(n, "proposals")], [m.get("_id") for m in r[(n, "models")]],
                            info.get(n))
                for n in names]
        return {"runs": sorted(rows, key=lambda row: run_order(row["db"]))}

    # -- cabinet --------------------------------------------------------------------------------------------------
    def cabinet_clients(self, db: str, cabinet_db: Optional[str] = None) -> list:
        source, fallback = self.cabinet_source(db, cabinet_db)
        group = [{"$group": {"_id": "$client_id", "n": {"$sum": 1}}}]
        harness_group = [{"$group": {"_id": {"$ifNull": ["$client_id", "$client"]}, "n": {"$sum": 1}}}]
        ro = self.ro
        r = self._parallel({
            "clients": lambda: ro.find("cabinet", "clients", {}, sort=[("client_id", 1)]),
            "notes": lambda: ro.aggregate("cabinet", "notes", group),
            "preps": lambda: ro.aggregate("cabinet", "preps_baseline", group),
            "events": lambda: ro.aggregate("cabinet", "events", group),
            "faults": lambda: ro.aggregate("cabinet_truth", "answer_key", group),
            "harness_preps": lambda: ro.aggregate(source, "cabinet_preps", harness_group),
        })
        counts: dict = {}
        for name in ("notes", "preps", "events", "faults", "harness_preps"):
            for row in r[name]:
                counts.setdefault(str(row.get("_id")), {})[name] = int(row.get("n", 0))
        out = []
        for c in r["clients"]:
            item = clean(c)
            have = counts.get(str(c.get("client_id")), {})
            item["counts"] = {k: have.get(k, 0) for k in ("notes", "preps", "events", "faults", "harness_preps")}
            item["cabinet_source_db"], item["cabinet_source_fallback"] = source, fallback
            out.append(item)
        return out

    def cabinet_client(self, cid: str, faults: bool, db: str, cabinet_db: Optional[str] = None) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", cid or ""):
            raise ApiError(400, "client id must be letters, digits, _ or -")
        source, fallback = self.cabinet_source(db, cabinet_db)
        ro = self.ro
        jobs = {
            "client": lambda: ro.find("cabinet", "clients", {"client_id": cid}, limit=1),
            "notes": lambda: ro.find("cabinet", "notes", {"client_id": cid}, sort=[("date", 1), ("note_id", 1)]),
            "preps": lambda: ro.find("cabinet", "preps_baseline", {"client_id": cid},
                                     sort=[("date", 1), ("prep_id", 1)]),
            "events": lambda: ro.find("cabinet", "events", {"client_id": cid}, sort=[("date", 1)]),
            "answer": lambda: ro.find("cabinet_truth", "answer_key", {"client_id": cid}, sort=[("fault_id", 1)]),
            "harness": lambda: ro.find(source, "cabinet_preps", {"$or": [{"client_id": cid}, {"client": cid}]},
                                       sort=[("_id", 1)]),
        }
        if faults:
            jobs["expected"] = lambda: ro.find("cabinet_truth", "expected_actions", {"client_id": cid},
                                               sort=[("date", 1), ("prep_id", 1)])
        r = self._parallel(jobs)
        if not r["client"]:
            raise ApiError(404, f"no client {cid!r} in cabinet.clients")
        by_doc: dict = {}
        for f in r["answer"]:
            by_doc.setdefault(str(f.get("doc_id")), []).append(f)
        expected: dict = {}
        for x in r.get("expected") or []:
            expected.setdefault(str(x.get("prep_id")), []).append(x)
        client = clean(r["client"][0])
        client["counts"] = {"notes": len(r["notes"]), "preps": len(r["preps"]), "events": len(r["events"]),
                            "faults": len(r["answer"]), "harness_preps": len(r["harness"])}
        timeline = build_timeline(r["notes"], r["preps"], r["events"], by_doc if faults else None,
                                  expected if faults else None, r["harness"])
        return {"client": client, "db": db, "cabinet_source_db": source, "cabinet_source_fallback": fallback,
                "answer_key_included": bool(faults), "timeline": timeline}

    def cabinet_vocabulary(self) -> dict:
        found = self.ro.find("cabinet", "vocabulary", {}, limit=1)
        vocab = clean(found[0]) if found else None
        if isinstance(vocab, dict):
            vocab.pop("id", None)
        return {"vocabulary": vocab}

    # -- generic browser ------------------------------------------------------------------------------------------
    def dbs(self) -> dict:
        names = self.allowed()
        ro = self.ro
        colls = self._parallel({db: (lambda d: lambda: ro.collections(d))(db) for db in names})
        counts = self._parallel({(db, c): (lambda d, cc: lambda: ro.count(d, cc))(db, c)
                                 for db in names for c in colls[db]})
        return {"dbs": [{"name": db, "role": "pregame" if db == self.default_db or is_pregame_name(db) else db,
                         "default": db == self.default_db,
                         "collections": [{"name": c, "count": counts[(db, c)]} for c in colls[db]]}
                        for db in names]}

    def db_collection(self, db: str, coll: str, limit: int = 20) -> dict:
        self.check_browsable(db, coll)
        total = self.ro.count(db, coll)
        docs = self.ro.find(db, coll, {}, sort=[("_id", -1)], limit=limit) if total else []
        return {"db": db, "collection": coll, "count": total, "limit": limit,
                "docs": [trim_for_browser(clean(d)) for d in docs]}


# ---------------------------------------------------------------------------------------------------------------
# Fixture source: serves the snapshot in fixtures/ (no packages, no network)
# ---------------------------------------------------------------------------------------------------------------
def fixture_name_for_collection(db: str, coll: str) -> str:
    return f"db_{db}.{coll}"


def fixture_name(name: str, db: Optional[str], default_db: Optional[str]) -> str:
    """The default database's fixtures have plain names; another database's carry @<db>: overview@pregame_run_a."""
    return name if not db or db == default_db else f"{name}@{db}"


class FixtureSource:
    offline = True

    def __init__(self, cfg: dict, fixtures_dir: Path = FIXTURES_DIR, data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.dir = fixtures_dir
        self.data_dir = data_dir

    def _exists(self, name: str) -> bool:
        return (self.dir / f"{name}.json").is_file()

    def _load(self, name: str, missing_status: int = 503) -> Any:
        path = self.dir / f"{name}.json"
        if not path.is_file():
            raise ApiError(missing_status, f"fixture {name}.json is not in the snapshot",
                           hint="Run `server.py --snapshot` on a laptop that can reach Atlas, then copy fixtures/.")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ApiError(503, f"fixture {name}.json could not be read", detail=type(exc).__name__)

    def meta(self) -> dict:
        path = self.dir / "_meta.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    @property
    def default_db(self) -> str:
        return self.meta().get("pregame_db") or self.cfg.get("PREGAME_DB") or DEFAULT_PREGAME_DB

    def snapshot_dbs(self) -> list:
        meta = self.meta()
        names = [d.get("name") for d in meta.get("pregame_dbs") or [] if isinstance(d, dict) and d.get("in_snapshot")]
        default = self.default_db
        return names if default in names else [default] + names

    def resolve_db(self, name: Optional[str]) -> str:
        snap = self.snapshot_dbs()
        if not name:
            configured = self.cfg.get("PREGAME_DB")
            return configured if configured in snap else self.default_db
        if name in snap:
            return name
        raise ApiError(403, f"database {name!r} is not in this snapshot",
                       hint="Offline, db must be one of overview.pregame_dbs.", snapshot_dbs=snap)

    def _db_fixture(self, name: str, db: str) -> Any:
        return self._load(fixture_name(name, db, self.default_db))

    def _snapshot_rows(self) -> list:
        return [d for d in self.meta().get("pregame_dbs") or [] if isinstance(d, dict) and d.get("in_snapshot")]

    def cabinet_source(self, db: str, override: Optional[str] = None) -> tuple:
        if override:
            return self.resolve_db(override), False
        return pick_cabinet_source(db, self._snapshot_rows())

    def _cabinet_fixture(self, base: str, source: str, missing_status: int = 503) -> tuple:
        """(data, from_source): the source database's own cabinet fixture, else the plain one; from_source says
        whether its harness preps / cabinet_runs belong to `source`."""
        own = fixture_name(base, source, self.default_db)
        if self._exists(own):
            data = self._load(own)
            return data, True
        data = self._load(base, missing_status=missing_status)
        first = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else {}
        recorded = (first or {}).get("cabinet_source_db") or (first or {}).get("db") or self.default_db
        return data, recorded == source

    def overview(self, db: str) -> dict:
        data = self._db_fixture("overview", db)
        meta = self.meta()
        data["llm_mode"], data["llm_mode_source"] = llm_mode_for(db, self.cfg, load_run_info(self.data_dir))
        data["offline"] = True
        data["public"] = bool(meta.get("public"))
        data["snapshot_at"] = meta.get("snapshot_at") or data.get("snapshot_at")
        data["pregame_db"] = data["active_db"] = db
        data["default_db"] = self.default_db
        data["pregame_dbs"] = [d for d in meta.get("pregame_dbs") or [] if isinstance(d, dict) and d.get("in_snapshot")]
        return data

    def activity(self, db: str, after_seq: int = 0, limit: int = 300) -> dict:
        data = self._db_fixture("activity", db)
        items = [i for i in data.get("items") or [] if isinstance(i.get("seq"), int) and i["seq"] > after_seq]
        items.sort(key=lambda i: i["seq"])
        return {"db": db, "items": items[-limit:] if limit else [], "last_seq": data.get("last_seq", 0)}

    def proposals(self, db: str) -> list:
        return self._db_fixture("proposals", db)

    def versions(self, db: str) -> dict:
        return self._db_fixture("versions", db)

    def briefs(self, db: str, limit: int = 20) -> list:
        return (self._db_fixture("briefs", db) or [])[:limit]

    def world(self, db: str) -> dict:
        return self._db_fixture("world", db)

    def findings(self, db: str, cabinet_db: Optional[str] = None) -> dict:
        out = findings_from_files(self.data_dir)
        out["db"] = db
        source, fallback = self.cabinet_source(db, cabinet_db)
        out["cabinet_source_db"], out["cabinet_source_fallback"] = source, fallback
        try:
            out["pregame_eval"] = self._db_fixture("findings", db).get("pregame_eval") or []
        except ApiError:
            out["pregame_eval"] = []
            out["pregame_eval_error"] = f"findings for {db} are not in the snapshot"
        try:
            snap = self._db_fixture("findings", source)
            recorded = snap.get("cabinet_source_db") or snap.get("db") or source
            out["cabinet_runs"] = snap.get("cabinet_runs") if recorded == source else None
        except ApiError:
            out["cabinet_runs"] = None
        if self.meta().get("public"):  # the data files may be the full local ones: publish the public view only
            out = public_findings(out)
        return out

    def runs(self) -> dict:
        return self._load("runs")

    def cabinet_clients(self, db: str, cabinet_db: Optional[str] = None) -> list:
        source, fallback = self.cabinet_source(db, cabinet_db)
        data, from_source = self._cabinet_fixture("cabinet_clients", source)
        for c in data:
            if not from_source and isinstance(c.get("counts"), dict):
                c["counts"]["harness_preps"] = 0  # the fixture's harness preps belong to another database
            c["cabinet_source_db"], c["cabinet_source_fallback"] = source, fallback
        return data

    def cabinet_client(self, cid: str, faults: bool, db: str, cabinet_db: Optional[str] = None) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", cid or ""):
            raise ApiError(400, "client id must be letters, digits, _ or -")
        source, fallback = self.cabinet_source(db, cabinet_db)
        data, from_source = self._cabinet_fixture(f"cabinet_client_{cid}", source, missing_status=404)
        if not from_source:  # the fixture's harness preps belong to another database
            data["timeline"] = [i for i in data.get("timeline") or [] if i.get("kind") != "harness_prep"]
            if isinstance((data.get("client") or {}).get("counts"), dict):
                data["client"]["counts"]["harness_preps"] = 0
        data["db"] = db
        data["cabinet_source_db"], data["cabinet_source_fallback"] = source, fallback
        public = bool(self.meta().get("public")) or data.get("answer_key_included") is False
        if not faults or public:
            data["timeline"] = strip_faults(data.get("timeline") or [])
        data["answer_key_included"] = bool(faults) and not public
        return data

    def cabinet_vocabulary(self) -> dict:
        return self._load("cabinet_vocabulary")

    def allowed(self) -> list:
        out = []
        others = ["cabinet"] if self.meta().get("public") else list(OTHER_DBS)  # public: no cabinet_truth, harness
        for name in self.snapshot_dbs() + others:
            if name not in out:
                out.append(name)
        return out

    def check_browsable(self, db: str, coll: str) -> None:
        if db not in self.allowed():
            raise ApiError(403, f"database {db!r} is not one the viewer reads (or not in this snapshot)")
        check_collection_name(coll)

    def dbs(self) -> dict:
        return self._load("dbs")

    def db_collection(self, db: str, coll: str, limit: int = 20) -> dict:
        self.check_browsable(db, coll)
        try:
            data = self._load(fixture_name_for_collection(db, coll), missing_status=404)
        except ApiError:
            return {"db": db, "collection": coll, "count": 0, "limit": limit, "docs": [],
                    "note": "not in the snapshot"}
        data["docs"] = (data.get("docs") or [])[:limit]
        data["limit"] = limit
        return data


# ---------------------------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------------------------
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".ico": "image/x-icon", ".webp": "image/webp",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8", ".map": "application/json",
}

ROUTES = [
    (re.compile(r"^/api/overview/?$"), "overview"),
    (re.compile(r"^/api/activity/?$"), "activity"),
    (re.compile(r"^/api/proposals/?$"), "proposals"),
    (re.compile(r"^/api/versions/?$"), "versions"),
    (re.compile(r"^/api/briefs/?$"), "briefs"),
    (re.compile(r"^/api/world/?$"), "world"),
    (re.compile(r"^/api/findings/?$"), "findings"),
    (re.compile(r"^/api/runs/?$"), "runs"),
    (re.compile(r"^/api/cabinet/clients/?$"), "cabinet_clients"),
    (re.compile(r"^/api/cabinet/client/(?P<cid>[^/]+)/?$"), "cabinet_client"),
    (re.compile(r"^/api/cabinet/vocabulary/?$"), "cabinet_vocabulary"),
    (re.compile(r"^/api/dbs/?$"), "dbs"),
    (re.compile(r"^/api/db/(?P<db>[^/]+)/(?P<coll>[^/]+)/?$"), "db_collection"),
]
# Endpoints that read a Pregame database and take ?db=<name> (validated against the allowlist).
DB_ENDPOINTS = {"overview", "activity", "proposals", "versions", "briefs", "world", "findings", "cabinet_clients",
                "cabinet_client"}


class App:
    """Routing and the source (live or fixtures); shared by every request thread."""

    def __init__(self, cfg: dict, offline: bool, static_dir: Path = STATIC_DIR, fixtures_dir: Path = FIXTURES_DIR,
                 data_dir: Path = DATA_DIR):
        self.cfg = cfg
        self.secrets = Secrets(cfg.get("MONGODB_URI", ""))
        self.static_dir = static_dir
        self.fixtures_dir = fixtures_dir
        self.source = FixtureSource(cfg, fixtures_dir, data_dir) if offline else \
            LiveSource(cfg, data_dir, self.secrets)

    def close(self) -> None:
        if isinstance(self.source, LiveSource):
            self.source.ro.close()

    def api(self, path: str, query: dict) -> Any:
        for pattern, name in ROUTES:
            m = pattern.match(path)
            if not m:
                continue
            args = {k: urllib.parse.unquote(v) for k, v in m.groupdict().items()}
            src = self.source
            if name not in DB_ENDPOINTS:
                if name == "db_collection":
                    return src.db_collection(args["db"], args["coll"], _int_param(query, "limit", 20, 1, 200))
                return getattr(src, name)()
            db = src.resolve_db((query.get("db") or [""])[0].strip())
            if name == "activity":
                return src.activity(db, _int_param(query, "after_seq", 0, 0, 10 ** 12),
                                    _int_param(query, "limit", 300, 1, 5000))
            if name == "briefs":
                return src.briefs(db, _int_param(query, "limit", 20, 1, 500))
            cabinet_db = (query.get("cabinet_db") or [""])[0].strip() or None
            if name == "cabinet_client":
                faults = (query.get("faults") or ["0"])[0].lower() in ("1", "true", "yes", "on")
                return src.cabinet_client(args["cid"], faults, db, cabinet_db)
            if name in ("cabinet_clients", "findings"):
                return getattr(src, name)(db, cabinet_db)
            return getattr(src, name)(db)
        raise ApiError(404, f"no such endpoint: {path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "PregameViewer/1.0"
    app: App  # set by make_server

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        try:
            if int(code) >= 400:
                super().log_request(code, size)
        except (TypeError, ValueError):
            pass

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("[viewer] " + self.app.secrets.redact(format % args) + "\n")

    # -- responses -------------------------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, head_only: bool, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def send_json(self, status: int, obj: Any, head_only: bool = False) -> None:
        text = json.dumps(jsonable(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if self.app.secrets.leaks(text):
            status = 500
            text = json.dumps({"error": "response withheld: it contained part of the database connection string"})
        self._send(status, text.encode("utf-8"), "application/json; charset=utf-8", head_only)

    def send_file(self, root: Path, rel: str, head_only: bool) -> None:
        rel = urllib.parse.unquote(rel).replace("\\", "/").lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        try:
            root_resolved = root.resolve()
            target = (root_resolved / rel).resolve()
        except (OSError, ValueError):
            return self.send_json(404, {"error": "not found"}, head_only)
        if root_resolved != target and root_resolved not in target.parents:
            return self.send_json(404, {"error": "not found"}, head_only)
        if not target.is_file():
            if rel == "index.html":
                return self.send_json(404, {"error": "static/index.html is missing"}, head_only)
            return self.send_json(404, {"error": "not found", "path": "/" + rel}, head_only)
        try:
            body = target.read_bytes()
        except OSError:
            return self.send_json(404, {"error": "not found"}, head_only)
        if target.suffix.lower() == ".json" and self.app.secrets.leaks(body.decode("utf-8", "replace")):
            return self.send_json(500, {"error": "file withheld: it contained part of the connection string"},
                                  head_only)
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, ctype, head_only, cache="no-cache")

    # -- methods ---------------------------------------------------------------------------------------------------
    def _handle(self, head_only: bool) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path or "/"
            if path.startswith("/api/") or path == "/api":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    return self.send_json(200, self.app.api(path, query), head_only)
                except ApiError as exc:
                    return self.send_json(exc.status, exc.payload, head_only)
            if path.startswith("/fixtures/"):
                return self.send_file(self.app.fixtures_dir, path[len("/fixtures/"):], head_only)
            if path.startswith("/static/"):
                return self.send_file(self.app.static_dir, path[len("/static/"):], head_only)
            return self.send_file(self.app.static_dir, path, head_only)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return None
        except Exception as exc:  # never crash the server; never echo raw exception text (it may carry the URI)
            detail = f"{type(exc).__name__}: {self.app.secrets.redact(str(exc))[:300]}"
            try:
                self.send_json(500, {"error": "internal error", "detail": detail}, head_only)
            except Exception:
                pass
            self.log_message("500 on %s: %s", self.path, detail)

    def do_GET(self) -> None:
        self._handle(head_only=False)

    def do_HEAD(self) -> None:
        self._handle(head_only=True)

    def _read_only(self) -> None:
        self.send_json(405, {"error": "the viewer is read-only; only GET and HEAD are served"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _read_only


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server silently share a busy port; fail loudly instead, and take the
    # port exclusively so nothing else can bind it underneath us.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(cfg: dict, host: str, port: int, offline: bool, static_dir: Path = STATIC_DIR,
                fixtures_dir: Path = FIXTURES_DIR, data_dir: Path = DATA_DIR) -> ViewerServer:
    app = App(cfg, offline, static_dir, fixtures_dir, data_dir)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ViewerServer((host, port), handler)
    server.app = app  # type: ignore[attr-defined]
    return server


def make_server_on_free_port(cfg: dict, host: str, port: int, offline: bool, tries: int = PORT_TRIES,
                             **dirs: Any) -> ViewerServer:
    """Bind `port`, or the next free one of the following `tries` ports. Raises the last OSError if none is free."""
    last: Optional[OSError] = None
    for candidate in range(port, port + tries + 1):
        try:
            return make_server(cfg, host, candidate, offline, **dirs)
        except OSError as exc:
            last = exc
    assert last is not None
    raise last


# ---------------------------------------------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------------------------------------------
_FIXTURE_PATTERNS = re.compile(r"^(overview|activity|proposals|versions|briefs|world|findings|runs|cabinet_clients|"
                               r"cabinet_vocabulary|dbs|cabinet_client_[A-Za-z0-9_\-]+|db_.+|_meta)"
                               r"(@[A-Za-z0-9_.\-]+)?\.json$")
DB_FIXTURES = ("overview", "activity", "proposals", "versions", "briefs", "world", "findings")


def build_snapshot(source: LiveSource) -> dict:
    """Every endpoint's JSON, keyed by fixture name, for every pregame database. Raises ApiError (e.g.
    DBUnavailable) before anything is written."""
    out: dict = {}
    snapshot_at = now_iso()
    default = source.default_db
    counts = {row["name"]: row for row in source.pregame_db_counts()}
    db_meta = []
    for db in source.pregame_dbs():
        name = lambda base: fixture_name(base, db, default)  # noqa: E731
        written = list(DB_FIXTURES)
        out[name("overview")] = source.overview(db)
        out[name("activity")] = source.activity(db, 0, 10 ** 6)
        out[name("proposals")] = source.proposals(db)
        out[name("versions")] = source.versions(db)
        out[name("briefs")] = source.briefs(db, 10 ** 4)
        out[name("world")] = source.world(db)
        out[name("findings")] = source.findings(db)
        own_cabinet = has_cabinet_data(counts.get(db, {}))
        if db == default or own_cabinet:
            # A database with its own cabinet data is snapshotted from itself; the default one with the fallback.
            cabinet_db = db if own_cabinet else None
            clients = source.cabinet_clients(db, cabinet_db)
            out[name("cabinet_clients")] = clients
            written.append("cabinet_clients")
            for c in clients:
                cid = str(c.get("client_id") or "")
                if re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", cid):
                    out[name(f"cabinet_client_{cid}")] = source.cabinet_client(cid, True, db, cabinet_db)
            written.append("cabinet_client_<cid>")
        db_meta.append({**counts.get(db, {"name": db}), "in_snapshot": True, "fixtures": written})
    for key, value in out.items():
        if key == "overview" or key.startswith("overview@"):
            value["offline"] = True
            value["snapshot_at"] = snapshot_at
            value["pregame_dbs"] = db_meta
    out["runs"] = source.runs()
    out["cabinet_vocabulary"] = source.cabinet_vocabulary()
    dbs = source.dbs()
    out["dbs"] = dbs
    for d in dbs.get("dbs", []):
        # The latest 50 documents; 20 for the non-default pregame databases (their eval_scenarios are large).
        limit = 20 if is_pregame_name(d["name"]) and d["name"] != default else 50
        for c in d.get("collections", []):
            try:
                out[fixture_name_for_collection(d["name"], c["name"])] = \
                    source.db_collection(d["name"], c["name"], limit)
            except ApiError as exc:
                if exc.status != 403:  # an oddly named collection is skipped; a database outage is not
                    raise
    out["_meta"] = {"snapshot_at": snapshot_at, "pregame_db": default, "pregame_dbs": db_meta,
                    "llm_mode": source.cfg.get("PREGAME_LLM_MODE") or None,
                    "fixtures": sorted(f"{n}.json" for n in out) + ["_meta.json"]}
    return out


def build_public_snapshot(source: LiveSource) -> dict:
    """The public fixture set: the default database only (plus the runs comparison), the visible cabinet with the
    answer key off, and nothing from cabinet_truth, harness or the other pregame databases."""
    out: dict = {}
    snapshot_at = now_iso()
    db = source.default_db
    counts = {row["name"]: row for row in source.pregame_db_counts()}
    row = {k: v for k, v in counts.get(db, {"name": db}).items()
           if k in ("name", "ledger", "proposals", "briefs", "cabinet_preps", "cabinet_runs", "cabinet_written_at")}
    db_meta = [{**row, "in_snapshot": True, "fixtures": list(DB_FIXTURES) + ["cabinet_clients", "cabinet_client_<cid>"]}]
    overview = source.overview(db)
    overview.update({"offline": True, "public": True, "snapshot_at": snapshot_at, "pregame_dbs": db_meta})
    out["overview"] = overview
    out["activity"] = source.activity(db, 0, 10 ** 6)
    out["proposals"] = source.proposals(db)
    out["versions"] = source.versions(db)
    out["briefs"] = source.briefs(db, 10 ** 4)
    out["world"] = source.world(db)
    out["findings"] = public_findings(source.findings(db, db))  # no cabinet fallback to another database
    out["runs"] = {"runs": [r for r in source.runs()["runs"]
                            if r["db"] == db or r["db"].startswith("pregame_run_")]}
    clients = source.cabinet_clients(db, db)
    for c in clients:
        (c.get("counts") or {}).pop("faults", None)
    out["cabinet_clients"] = clients
    for c in clients:
        cid = str(c.get("client_id") or "")
        if re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", cid):
            client = source.cabinet_client(cid, False, db, db)
            (client.get("client") or {}).get("counts", {}).pop("faults", None)
            client["answer_key_included"] = False
            out[f"cabinet_client_{cid}"] = client
    out["cabinet_vocabulary"] = source.cabinet_vocabulary()
    visible = (db, "cabinet")
    dbs = {"dbs": [d for d in source.dbs()["dbs"] if d["name"] in visible]}
    out["dbs"] = dbs
    for d in dbs["dbs"]:
        for c in d.get("collections", []):
            try:
                out[fixture_name_for_collection(d["name"], c["name"])] = source.db_collection(d["name"], c["name"], 50)
            except ApiError as exc:
                if exc.status != 403:
                    raise
    for key in list(out):
        if key.startswith("db_") and key.endswith(".cabinet_runs"):
            out[key]["docs"] = [public_cabinet_run(doc) for doc in out[key]["docs"]]
    out["_meta"] = {"snapshot_at": snapshot_at, "public": True, "answer_key_included": False, "pregame_db": db,
                    "pregame_dbs": db_meta, "llm_mode": overview.get("llm_mode"),
                    "fixtures": sorted(f"{n}.json" for n in out) + ["_meta.json"]}
    return {name: scrub_local_paths(_drop_per_row_keys(data)) for name, data in out.items()}


def _drop_per_row_keys(value: Any) -> Any:
    if isinstance(value, dict):
        # Per-client / per-prep scoring rows are dicts or lists; a count such as counts.harness_preps stays.
        return {k: _drop_per_row_keys(v) for k, v in value.items()
                if not (k in PER_ROW_KEYS and isinstance(v, (dict, list)))}
    if isinstance(value, list):
        return [_drop_per_row_keys(v) for v in value]
    return value


def write_snapshot(fixtures: dict, fixtures_dir: Path, secrets: Secrets) -> list:
    """Serialise everything first and refuse to write anything if a secret appears; then replace the files and
    drop fixture files from an older snapshot that no longer exist."""
    rendered = {}
    for name, data in fixtures.items():
        text = json.dumps(jsonable(data), ensure_ascii=False, indent=1, allow_nan=False)
        if secrets.leaks(text):
            raise RuntimeError(f"fixture {name}.json would contain part of the connection string; nothing was written")
        rendered[f"{name}.json"] = text
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    for filename, text in rendered.items():
        tmp = fixtures_dir / (filename + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, fixtures_dir / filename)
    for old in fixtures_dir.glob("*.json"):
        if old.name not in rendered and _FIXTURE_PATTERNS.match(old.name):
            old.unlink()
    return sorted(rendered)


def run_snapshot(cfg: dict, fixtures_dir: Path = FIXTURES_DIR, public: bool = False) -> int:
    source = LiveSource(cfg)
    if public:
        print(f"[viewer] PUBLIC snapshot: {source.default_db} and the visible cabinet only, answer key off (read-only) ...")
    else:
        print(f"[viewer] snapshot: reading every pregame_* database (default {source.default_db}), cabinet, "
              f"cabinet_truth, harness (read-only) ...")
    try:
        fixtures = build_public_snapshot(source) if public else build_snapshot(source)
        written = write_snapshot(fixtures, fixtures_dir, source.secrets)
    except ApiError as exc:
        print("[viewer] snapshot failed: " + json.dumps(exc.payload), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"[viewer] snapshot refused: {exc}", file=sys.stderr)
        return 1
    finally:
        source.ro.close()
    dbs = ", ".join(d["name"] for d in fixtures["_meta"]["pregame_dbs"])
    print(f"[viewer] wrote {len(written)} files to fixtures/ at {fixtures['_meta']['snapshot_at']} "
          f"(pregame databases: {dbs}):")
    for name in written:
        print("   " + name)
    return 0


# ---------------------------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------------------------
def main(argv: Optional[list] = None) -> int:
    if sys.version_info < (3, 10):
        print("The Pregame viewer needs Python 3.10 or newer.", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):  # show the banner promptly even when output is piped
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Pregame viewer: a read-only local server (see README.md).")
    parser.add_argument("--offline", action="store_true", help="serve the snapshot in fixtures/ instead of MongoDB")
    parser.add_argument("--snapshot", action="store_true", help="write every endpoint's JSON into fixtures/ and exit")
    parser.add_argument("--public", action="store_true",
                        help="with --snapshot: write the public fixture set (default database, visible cabinet, no "
                             "answer key)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port (default VIEWER_PORT or {DEFAULT_PORT}; if busy, the next {PORT_TRIES} are tried)")
    parser.add_argument("--host", default="127.0.0.1", help="interface (default 127.0.0.1; 0.0.0.0 is opt-in)")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.snapshot:
        if args.offline:
            parser.error("--snapshot reads MongoDB; it cannot be combined with --offline")
        return run_snapshot(cfg, public=args.public)
    if args.public:
        parser.error("--public only applies to --snapshot")

    try:
        port = args.port if args.port is not None else int(cfg.get("VIEWER_PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    try:
        server = make_server_on_free_port(cfg, args.host, port, args.offline)
    except OSError as exc:
        print(f"[viewer] cannot listen on {args.host}:{port} or the next {PORT_TRIES} ports "
              f"({exc.strerror or exc}). Pass --port with a free port.", file=sys.stderr)
        return 1
    used = server.server_address[1]
    if used != port:
        print(f"[viewer] port {port} is busy; using {used} instead.")

    browse_host = "127.0.0.1" if args.host in ("0.0.0.0", "", "::") else args.host
    url = f"http://{browse_host}:{used}/"
    if args.offline:
        source = server.app.source  # type: ignore[attr-defined]
        meta = source.meta()
        mode = (f"OFFLINE snapshot from {meta.get('snapshot_at') or 'unknown time'}, "
                f"databases {', '.join(source.snapshot_dbs())}")
        if not (FIXTURES_DIR / "overview.json").is_file():
            mode += " (fixtures/ is empty: run --snapshot on a laptop that can reach Atlas)"
    else:
        mode = f"online, default database {cfg.get('PREGAME_DB') or DEFAULT_PREGAME_DB} (read-only)"
        if not cfg.get("MONGODB_URI"):
            mode += " -- MONGODB_URI is not set: fill in viewer.env or use --offline"
    print(f"[viewer] Pregame viewer at {url}  [{mode}]")
    print("[viewer] never writes to the database. Ctrl+C to stop.")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[viewer] listening on {args.host}: other machines on this network can read it.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[viewer] stopped.")
    finally:
        server.server_close()
        server.app.close()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    sys.exit(main())
