"""Smoke test against the hackathon's Atlas Sandbox cluster.

Run with:
    C:/Projects/prep-harness/.venv/Scripts/python scripts/smoke_atlas.py

Steps, each printed as it runs:
  1. connect via pregame.db.get_db() and print buildInfo's server version + a best-effort
     "looks like Atlas" signal.
  2. commit one two-collection transaction in a scratch database (`pregame_smoke`).
  3. open a change stream on a scratch collection, insert one document, and confirm the
     stream reports it within a 10 second timeout.
  4. drop the scratch database.

This could not be run during the build (no cluster was reachable from the build machine), so
it is written to fail loudly and specifically at whichever step breaks, rather than crashing
with a raw traceback -- point it at the real MONGODB_URI later and read the [smoke] lines.

Safety: this script never prints the connection URI, and never prints any exception text
verbatim -- every message is passed through `_sanitize`, which redacts anything shaped like
`scheme://user:pass@host` before it reaches stdout, as defense in depth on top of pymongo's
own credential redaction in its error messages.
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone

# Make `pregame` importable regardless of cwd (e.g. `python scripts/smoke_atlas.py` from the
# repo root only puts scripts/ on sys.path, not the repo root).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from pregame.db import get_db, is_mock

SCRATCH_DB_NAME = "pregame_smoke"
CHANGE_STREAM_TIMEOUT_S = 10

# Matches "scheme://user:pass@" so a connection string caught in an exception's text (e.g. a
# malformed-URI error that echoes it back) never reaches stdout intact.
_CREDENTIAL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://[^/@\s]+:[^/@\s]+@")


def _sanitize(text: str) -> str:
    return _CREDENTIAL_RE.sub("<redacted-uri>://<redacted>@", text)


def _step(label: str) -> None:
    print(f"[smoke] {label} ...", flush=True)


def _ok(label: str, detail: str = "") -> None:
    suffix = f" -- {detail}" if detail else ""
    print(f"[smoke] OK: {label}{suffix}", flush=True)


def _fail(label: str, exc: Exception) -> None:
    print(f"[smoke] FAIL: {label}: {type(exc).__name__}: {_sanitize(str(exc))}", flush=True)


def check_connection_and_version(db) -> bool:
    _step("connect + buildInfo")
    try:
        info = db.client.admin.command("buildInfo")
    except Exception as exc:  # noqa: BLE001 -- top-level smoke check: report, don't crash
        _fail("connect + buildInfo", exc)
        return False

    version = info.get("version", "unknown")

    # No single buildInfo field reliably says "this is Atlas" across every tier, so combine a
    # few independent, harmless-to-query signals.
    looks_like_atlas = "atlas" in str(info).lower()
    try:  # Atlas hosts end in mongodb.net; checked without printing the host
        looks_like_atlas = looks_like_atlas or any(h.endswith(".mongodb.net") for h, _ in db.client.nodes)
    except Exception:
        pass
    if not looks_like_atlas:
        try:
            host_info = db.client.admin.command("hostInfo")
            looks_like_atlas = "atlas" in str(host_info).lower()
        except Exception:
            pass  # hostInfo can be restricted on shared tiers; absence isn't a failure

    print(f"[smoke]   server version: {version}")
    print(f"[smoke]   looks like Atlas: {looks_like_atlas}")
    _ok("connect + buildInfo")
    return True


def check_transaction(db) -> bool:
    _step("two-collection transaction in scratch db")
    if is_mock(db):
        print("[smoke]   skipped: db is mongomock (no real transactions to test here)")
        return True

    def _txn(session, scratch) -> None:
        scratch.a.insert_one({"_id": "smoke-a", "at": datetime.now(timezone.utc)}, session=session)
        scratch.b.insert_one({"_id": "smoke-b", "at": datetime.now(timezone.utc)}, session=session)

    try:
        scratch = db.client[SCRATCH_DB_NAME]
        scratch.drop_collection("a")
        scratch.drop_collection("b")

        with db.client.start_session() as session:
            session.with_transaction(
                lambda s: _txn(s, scratch),
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"),
            )

        a_count = scratch.a.count_documents({})
        b_count = scratch.b.count_documents({})
    except Exception as exc:  # noqa: BLE001 -- every network op in this step can fail; report, don't crash
        _fail("two-collection transaction", exc)
        return False

    if a_count != 1 or b_count != 1:
        _fail(
            "two-collection transaction",
            RuntimeError(f"expected 1 doc in each collection, got a={a_count} b={b_count}"),
        )
        return False

    _ok("two-collection transaction", f"a={a_count} doc, b={b_count} doc")
    return True


def check_change_stream(db, timeout_s: int = CHANGE_STREAM_TIMEOUT_S) -> bool:
    _step(f"change stream sees one insert (timeout {timeout_s}s)")
    if is_mock(db):
        print("[smoke]   skipped: db is mongomock (change streams need a real replica set)")
        return True

    seen = None
    try:
        scratch = db.client[SCRATCH_DB_NAME]
        coll = scratch.get_collection("watched")
        coll.drop()

        with coll.watch([{"$match": {"operationType": "insert"}}]) as stream:
            coll.insert_one({"_id": "smoke-watch-1", "at": datetime.now(timezone.utc)})
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                change = stream.try_next()
                if change is not None:
                    seen = change
                    break
                time.sleep(0.25)
    except Exception as exc:  # noqa: BLE001 -- every network op in this step can fail; report, don't crash
        _fail("change stream", exc)
        return False

    if seen is None:
        _fail("change stream", TimeoutError(f"no insert event observed within {timeout_s}s"))
        return False

    _ok("change stream", f"operationType={seen.get('operationType')!r}")
    return True


def cleanup(db) -> bool:
    # Drop the scratch collections one by one: a readWriteAnyDatabase user may drop collections but not databases.
    # An empty database disappears on its own.
    _step(f"drop scratch collections in {SCRATCH_DB_NAME!r}")
    try:
        scratch = db.client[SCRATCH_DB_NAME]
        for name in ("a", "b", "watched"):
            scratch.drop_collection(name)
    except Exception as exc:  # noqa: BLE001
        _fail("drop scratch collections", exc)
        return False
    _ok("drop scratch collections")
    return True


def main() -> int:
    print("[smoke] connecting via pregame.db.get_db() (URI/credentials are never printed) ...", flush=True)
    try:
        db = get_db()
    except Exception as exc:  # noqa: BLE001
        _fail("get_db()", exc)
        return 1

    results = [
        check_connection_and_version(db),
        check_transaction(db),
        check_change_stream(db),
    ]
    results.append(cleanup(db))

    if all(results):
        print("[smoke] ALL CHECKS PASSED")
        return 0

    print("[smoke] SOME CHECKS FAILED -- see FAIL lines above")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[smoke] interrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 -- last-resort guard against a raw traceback
        print(f"[smoke] UNEXPECTED FAILURE: {type(exc).__name__}: {_sanitize(str(exc))}")
        sys.exit(1)
