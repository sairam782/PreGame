"""Versioned harness configuration: insert-only versions, one head per key, and the fence.

Binding per INTERFACES.md. Keys: `policy:<field>`, `rules:<field>`, `tools:<field>`,
`guardrails:global`. `_id` of a version doc is `"<kind>:<key>@v<n>"`; the head doc's `_id` is
`"<kind>:<key>"`.

`commit` is THE fence gate.py and the world/loop code must go through -- it is the only place
that is allowed to advance a config head. It does no model calls and has no opinion about
tiers or held-out evaluation; callers (gate.py) decide whether a change is allowed to reach
here at all. Concretely, for gate.py and world/loop:

- `commit(...)` raises `StaleVersion` if `base_version` is not the current head OR if the new
  version id already exists (a concurrent committer beat you to it, detected as a duplicate
  key on insert). Either way, nothing is left half-changed: the head update is a single
  conditional `find_one_and_update` that only writes when it actually matches, so a raised
  `StaleVersion` means no head, version, ledger, or proposal document changed.
- `commit` always appends exactly one ledger entry: kind `"rollback"` when `restores` is
  given, `"commit"` otherwise. The ledger actor is `approved_by` (default `"gate"`; pass
  `f"owner:{name}"` for a human approval, as `gate.approve` should).
- If you pass `proposal_id`, `commit` will flip that proposal's status to `"committed"` but
  ONLY if it is currently `"evaluating"`, `"awaiting_owner"`, or `"pending"` -- if the
  proposal is already `"committed"`/`"rejected"`/`"stale"` the status update silently matches
  zero documents (this is intentional: commit's own job is the version fence, not proposal
  bookkeeping, so it does not raise on that mismatch).
- Everything happens inside one `db.run_txn`, so under mongomock (no real transactions) the
  only thing keeping a stale commit from corrupting state is that first conditional update
  failing fast, before any other write. A genuine concurrent race on the *second* fence (the
  version-doc duplicate-key insert) can only be tested against a real MongoDB with real
  transactions -- mongomock has no rollback, so don't rely on it as a regression test for
  that path.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from pregame import ledger as ledger_module
from pregame.contracts import FIELDS
from pregame.db import run_txn
from pregame.defaults import DEFAULT_GUARDRAILS, DEFAULT_POLICY, DEFAULT_RULES, DEFAULT_TOOLS

_CONFIG_KEYS = ("policy", "rules", "tools", "guardrails")


class StaleVersion(Exception):
    """Raised when a commit's base_version no longer matches the head (optimistic-concurrency conflict)."""


def _head_id(kind: str, key: str) -> str:
    return f"{kind}:{key}"


def _version_id(kind: str, key: str, version: int) -> str:
    return f"{kind}:{key}@v{version}"


def head(db: Database, kind: str, key: str) -> int:
    """Current version number for `<kind>:<key>`."""
    doc = db.config_heads.find_one({"_id": _head_id(kind, key)})
    if doc is None:
        raise KeyError(f"no head for {kind}:{key}")
    return doc["version"]


def get_version(db: Database, kind: str, key: str, version: Optional[int] = None) -> dict:
    """The ConfigVersion doc for `<kind>:<key>` at `version` (None = current head)."""
    if version is None:
        version = head(db, kind, key)
    doc = db.config_versions.find_one({"_id": _version_id(kind, key, version)})
    if doc is None:
        raise KeyError(f"no such version: {kind}:{key}@v{version}")
    return doc


def history(db: Database, kind: str, key: str) -> list[dict]:
    """All versions for `<kind>:<key>`, oldest first."""
    return list(db.config_versions.find({"kind": kind, "key": key}, sort=[("version", 1)]))


def resolve_field_config(db: Database, field: str) -> dict:
    """Resolve the four current heads (policy/rules/tools for `field`, guardrails:global) into a FieldConfig."""
    policy_doc = get_version(db, "policy", field)
    rules_doc = get_version(db, "rules", field)
    tools_doc = get_version(db, "tools", field)
    guardrails_doc = get_version(db, "guardrails", "global")
    return {
        "field": field,
        "policy": policy_doc["body"],
        "rules": rules_doc["body"],
        "tools": tools_doc["body"],
        "guardrails": guardrails_doc["body"],
        "versions": {
            "policy": policy_doc["version"],
            "rules": rules_doc["version"],
            "tools": tools_doc["version"],
            "guardrails": guardrails_doc["version"],
        },
    }


def with_change(cfg: dict, kind: str, body: Any) -> dict:
    """Pure: a deep copy of `cfg` with the `kind` surface replaced by `body`. Never mutates `cfg`."""
    if kind not in _CONFIG_KEYS:
        raise ValueError(f"unknown config surface: {kind!r} (expected one of {_CONFIG_KEYS})")
    new_cfg = copy.deepcopy(cfg)
    new_cfg[kind] = copy.deepcopy(body)
    return new_cfg


def config_hash(cfg: dict) -> str:
    """sha256 of the canonical JSON of the four *bodies* -- version numbers are not part of the hash."""
    payload = {kind: cfg[kind] for kind in _CONFIG_KEYS}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def commit(
    db: Database,
    kind: str,
    key: str,
    base_version: int,
    body: Any,
    *,
    rationale: str,
    sim_time: datetime,
    proposal_id: Optional[str] = None,
    approval_hash: Optional[str] = None,
    approved_by: str = "gate",
    restores: Optional[int] = None,
) -> dict:
    """THE fence. One transaction: bump the head iff it is still at base_version, insert the new
    version, append a ledger entry, and (if proposal_id) mark that proposal committed.

    Raises StaleVersion if base_version doesn't match the current head, or if the target
    version id already exists (a concurrent commit got there first). Does no model calls.
    """
    new_version = base_version + 1
    head_id = _head_id(kind, key)

    def _txn(session: Optional[Any]) -> dict:
        updated_head = db.config_heads.find_one_and_update(
            {"_id": head_id, "version": base_version},
            {"$set": {"version": new_version}},
            session=session,
        )
        if updated_head is None:
            raise StaleVersion(f"{head_id} is not at base_version {base_version}")

        version_doc = {
            "_id": _version_id(kind, key, new_version),
            "kind": kind,
            "key": key,
            "version": new_version,
            "body": copy.deepcopy(body),
            "rationale": rationale,
            "proposal_id": proposal_id,
            "approval_hash": approval_hash,
            "approved_by": approved_by,
            "supersedes": base_version,
            "restores": restores,
            "created_sim": sim_time,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            db.config_versions.insert_one(version_doc, session=session)
        except DuplicateKeyError as exc:
            raise StaleVersion(f"{version_doc['_id']} already exists") from exc

        ledger_kind = "rollback" if restores is not None else "commit"
        ledger_module.append(
            db,
            ledger_kind,
            approved_by,
            {
                "kind": kind,
                "key": key,
                "version": new_version,
                "base_version": base_version,
                "rationale": rationale,
                "proposal_id": proposal_id,
                "restores": restores,
            },
            sim_time,
            session=session,
        )

        if proposal_id is not None:
            db.proposals.update_one(
                {"_id": proposal_id, "status": {"$in": ["evaluating", "awaiting_owner", "pending"]}},
                {"$set": {"status": "committed", "committed_version": new_version}},
                session=session,
            )

        return version_doc

    return run_txn(db, _txn)


def rollback(db: Database, kind: str, key: str, to_version: int, actor: str, sim_time: datetime) -> dict:
    """A new version carrying `to_version`'s body -- never rewrites history, just replays it forward."""
    target = get_version(db, kind, key, to_version)
    base_version = head(db, kind, key)
    return commit(
        db,
        kind,
        key,
        base_version,
        target["body"],
        rationale=f"rollback {kind}:{key} to v{to_version}",
        sim_time=sim_time,
        approved_by=actor,
        restores=to_version,
    )


def seed_configs(db: Database, sim_time: datetime) -> None:
    """v1 for policy/rules/tools per field, and guardrails:global. Idempotent: no-op if any head exists."""
    if db.config_heads.count_documents({}) > 0:
        return

    created_at = datetime.now(timezone.utc)
    seeded_keys: list[str] = []

    def _seed_one(session: Optional[Any], kind: str, key: str, body: Any) -> None:
        head_id = _head_id(kind, key)
        version_doc = {
            "_id": _version_id(kind, key, 1),
            "kind": kind,
            "key": key,
            "version": 1,
            "body": copy.deepcopy(body),
            "rationale": "seed",
            "proposal_id": None,
            "approval_hash": None,
            "approved_by": None,
            "supersedes": None,
            "restores": None,
            "created_sim": sim_time,
            "created_at": created_at,
        }
        db.config_versions.insert_one(version_doc, session=session)
        db.config_heads.insert_one({"_id": head_id, "version": 1}, session=session)
        seeded_keys.append(head_id)

    def _txn(session: Optional[Any]) -> None:
        for field in FIELDS:
            _seed_one(session, "policy", field, DEFAULT_POLICY)
            _seed_one(session, "rules", field, DEFAULT_RULES[field])
            _seed_one(session, "tools", field, DEFAULT_TOOLS)
        _seed_one(session, "guardrails", "global", DEFAULT_GUARDRAILS)

        ledger_module.append(
            db,
            "seed",
            "world",
            {"seeded": list(seeded_keys)},
            sim_time,
            session=session,
        )

    run_txn(db, _txn)
