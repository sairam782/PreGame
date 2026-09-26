"""Hash-chained, append-only ledger.

Binding per INTERFACES.md:
- append(db, kind, actor, payload, sim_time, session=None) -> LedgerEntry
- verify(db) -> tuple[bool, int, str]
- tail(db, n=30) -> list[LedgerEntry]

Hash formula (per the build brief): sha256 of the canonical JSON of
{seq, prev_hash, kind, actor, payload, sim_time ISO}. `_id` is the seq (int). Payloads must be
JSON-serialisable: datetimes inside them are converted to ISO strings before hashing AND before
storing, so re-reading the stored payload back from Mongo reproduces the exact same hash input
(no dict-ordering or BSON round-trip surprises).

Mongo's BSON date type only has millisecond precision (verified against mongomock, which
mimics this): a naive tz-aware Python datetime with microsecond precision would hash
differently at append time than after being read back from the database. So the ledger's own
`sim_time` field is truncated to millisecond precision before it is hashed *and* before it is
stored -- what gets hashed is exactly what a later `verify()` will read back.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

GENESIS_HASH = "GENESIS"
_MAX_APPEND_ATTEMPTS = 3


def _truncate_to_millis(dt: datetime) -> datetime:
    """Match BSON's millisecond precision so hash(stored) == hash(at append time)."""
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def _to_jsonable(value: Any) -> Any:
    """Recursively convert datetimes to ISO strings so payloads hash/store deterministically."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _compute_hash(seq: int, prev_hash: str, kind: str, actor: str, payload: dict, sim_time_iso: str) -> str:
    canonical = _canonical(
        {
            "seq": seq,
            "prev_hash": prev_hash,
            "kind": kind,
            "actor": actor,
            "payload": payload,
            "sim_time": sim_time_iso,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _last_entry(db: Database, session: Optional[Any]) -> Optional[dict]:
    return db.ledger.find_one({}, sort=[("_id", -1)], session=session)


def append(
    db: Database,
    kind: str,
    actor: str,
    payload: dict,
    sim_time: datetime,
    session: Optional[Any] = None,
) -> dict:
    """Append one hash-chained entry. seq = last seq + 1 (read with `session` when given).

    When `session` is None (no surrounding transaction), a duplicate `_id` on insert means a
    concurrent append raced us for the same seq: re-read the tail and retry, up to 3 attempts
    total. When `session` is provided, a single attempt is made and any error propagates --
    the caller (inside `run_txn`) owns the transaction's own retry semantics.
    """
    jsonable_payload = _to_jsonable(payload)
    sim_time = _truncate_to_millis(sim_time)
    sim_time_iso = sim_time.isoformat()

    attempts = 1 if session is not None else _MAX_APPEND_ATTEMPTS
    last_error: Optional[Exception] = None

    for _ in range(attempts):
        last = _last_entry(db, session)
        seq = (last["seq"] + 1) if last else 1
        prev_hash = last["hash"] if last else GENESIS_HASH

        entry_hash = _compute_hash(seq, prev_hash, kind, actor, jsonable_payload, sim_time_iso)
        entry = {
            "_id": seq,
            "seq": seq,
            "prev_hash": prev_hash,
            "hash": entry_hash,
            "kind": kind,
            "actor": actor,
            "payload": jsonable_payload,
            "sim_time": sim_time,
            "recorded_at": datetime.now(timezone.utc),
        }
        try:
            db.ledger.insert_one(entry, session=session)
            return entry
        except DuplicateKeyError as exc:
            last_error = exc
            if session is not None:
                raise
            continue

    assert last_error is not None
    raise last_error


def verify(db: Database) -> tuple[bool, int, str]:
    """Recompute every hash in seq order. Returns (ok, entries_checked, first_problem or "")."""
    checked = 0
    expected_prev = GENESIS_HASH
    for entry in db.ledger.find({}, sort=[("_id", 1)]):
        checked += 1
        seq = entry.get("seq")
        prev_hash = entry.get("prev_hash", "")
        stored_hash = entry.get("hash", "")
        sim_time = entry.get("sim_time")

        if seq != checked:
            return False, checked, f"seq {seq} out of order at position {checked}"
        if prev_hash != expected_prev:
            return False, checked, f"seq {seq}: prev_hash mismatch (chain broken)"

        sim_time_iso = sim_time.isoformat() if isinstance(sim_time, datetime) else str(sim_time)
        recomputed = _compute_hash(
            seq, prev_hash, entry.get("kind", ""), entry.get("actor", ""), entry.get("payload", {}), sim_time_iso
        )
        if recomputed != stored_hash:
            return False, checked, f"seq {seq}: hash mismatch (payload or fields tampered)"

        expected_prev = stored_hash

    return True, checked, ""


def tail(db: Database, n: int = 30) -> list[dict]:
    """The most recent `n` ledger entries, newest first."""
    return list(db.ledger.find({}, sort=[("_id", -1)], limit=n))
