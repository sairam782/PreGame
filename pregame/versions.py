"""Versioned harness configuration: insert-only versions, one head per key, and the fence.

Binding per INTERFACES.md. Keys: `policy:<field>`, `rules:<field>`, `tools:<field>`,
`guardrails:global`. `_id` of a version doc is `"<kind>:<key>@v<n>"`; the head doc's `_id` is
`"<kind>:<key>"`.

`commit` is THE fence gate.py and the world/loop code must go through -- it is the only place
that is allowed to advance a config head. It does no model calls and has no opinion about
tiers or held-out evaluation; callers (gate.py) decide whether a change is allowed to reach
here at all. Concretely, for gate.py and world/loop:

- `commit(...)` raises `StaleVersion` if `base_version` is not the current head, OR if the new
  version id already exists (a concurrent committer beat you to it), OR (when `proposal_id` is
  given) if that proposal doesn't exist or isn't in an eligible status. All three are checked
  as a pre-check BEFORE any write happens, so the common case (a stale caller) never touches
  the database. The conditional `find_one_and_update` on the head remains the real fence
  against a genuine concurrent committer on Atlas (with_transaction retries the whole callback
  on a write conflict); under mongomock, which has no rollback, this function tracks exactly
  what THIS invocation wrote after that head bump (the version doc, the ledger entry, whether
  the proposal flipped) and on any later exception -- including a proposal update that loses
  its own race (matched_count != 1) -- undoes all of it in reverse order (proposal snapshot
  restored, ledger entry deleted, version doc deleted, head reset to base_version) before
  re-raising. A raised `StaleVersion` always means no head, version, ledger, or proposal
  document changed -- verified by tests that pre-create a colliding version id, an ineligible
  proposal, and a proposal update that loses its race after the pre-check passed, each
  asserting every collection is byte-for-byte unchanged.
- `commit` always appends exactly one ledger entry: kind `"rollback"` when `restores` is
  given, `"commit"` otherwise. The ledger actor is `approved_by` (default `"gate"`; pass
  `f"owner:{name}"` for a human approval, as `gate.approve` should).
- If you pass `proposal_id`, `commit` requires that proposal to exist and be `"evaluating"`,
  `"awaiting_owner"`, or `"pending"` (checked before any write), and requires the status flip
  to `"committed"` to match exactly one document (checked again at write time, closing the
  race window on Atlas) -- a config version can never claim provenance from a proposal that
  wasn't actually eligible.
- If you pass `expected_heads` (`{"<kind>:<key>": version}`, e.g. the four heads the gate
  evaluated a candidate on), every one of those heads must still be at that version, checked
  with the other pre-checks inside the same transaction: a change evaluated against one
  combination of surfaces can never go live on top of another.
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
from pregame.db import is_mock, run_txn
from pregame.defaults import DEFAULT_GUARDRAILS, DEFAULT_POLICY, DEFAULT_RULES, DEFAULT_TOOLS

_CONFIG_KEYS = ("policy", "rules", "tools", "guardrails")
_ELIGIBLE_PROPOSAL_STATUSES = ("evaluating", "awaiting_owner", "pending")


class StaleVersion(Exception):
    """Raised when a commit's base_version no longer matches the head (optimistic-concurrency conflict)."""


class SeedInconsistent(Exception):
    """Raised when config_heads/config_versions are neither empty nor completely seeded.

    seed_configs must not treat "at least one head exists" as proof the whole database is
    seeded -- an interrupted or partial prior seed attempt would then be permanently stuck
    with some fields unconfigured, silently, forever.
    """


def _head_id(kind: str, key: str) -> str:
    return f"{kind}:{key}"


def _version_id(kind: str, key: str, version: int) -> str:
    return f"{kind}:{key}@v{version}"


# The exact 10 (kind, key) pairs seed_configs is responsible for: policy/rules/tools per field,
# plus the one global guardrails key. Computed once, at import time, so it can never drift from
# what `_seed_one` below actually writes.
_EXPECTED_SEED_ENTRIES: tuple[tuple[str, str], ...] = tuple(
    (kind, field) for field in FIELDS for kind in ("policy", "rules", "tools")
) + (("guardrails", "global"),)

_EXPECTED_SEED_HEAD_IDS: tuple[str, ...] = tuple(_head_id(kind, key) for kind, key in _EXPECTED_SEED_ENTRIES)


def _diagnose_seed_state(db: Database) -> list[str]:
    """Everything wrong with the database as a fully-seeded v1 baseline. Empty == fully seeded.

    "Fully seeded" does NOT mean every head is still at version 1 -- a legitimate commit after
    seeding (e.g. the demo turning a tool on) legitimately advances a head past v1, and
    re-running seed_configs afterwards must still recognize that as seeded, not partial. So each
    (kind, key) is checked for: its v1 document exists with the right kind/key/version (v1 is
    insert-only and must always exist, however far the head has since moved), its head exists
    with an integer version >= 1, and that head's *current* version actually resolves to a real,
    matching version document. On top of that, the one `seed` ledger entry must exist.
    """
    problems: list[str] = []

    for kind, key in _EXPECTED_SEED_ENTRIES:
        head_id = _head_id(kind, key)
        v1_id = _version_id(kind, key, 1)

        v1_doc = db.config_versions.find_one({"_id": v1_id})
        if v1_doc is None:
            problems.append(f"missing seed version {v1_id!r}")
        elif v1_doc.get("kind") != kind or v1_doc.get("key") != key or v1_doc.get("version") != 1:
            problems.append(
                f"{v1_id!r} has unexpected kind/key/version: "
                f"{v1_doc.get('kind')!r}/{v1_doc.get('key')!r}/{v1_doc.get('version')!r}"
            )

        head_doc = db.config_heads.find_one({"_id": head_id})
        if head_doc is None:
            problems.append(f"missing head {head_id!r}")
            continue

        head_version = head_doc.get("version")
        if not isinstance(head_version, int) or head_version < 1:
            problems.append(f"head {head_id!r} has invalid version {head_version!r}")
            continue

        current_id = _version_id(kind, key, head_version)
        current_doc = db.config_versions.find_one({"_id": current_id})
        if current_doc is None:
            problems.append(f"head {head_id!r} (version {head_version}) points at missing version {current_id!r}")
        elif (current_doc.get("kind") != kind or current_doc.get("key") != key
              or current_doc.get("version") != head_version):
            problems.append(
                f"{current_id!r} has unexpected kind/key/version: {current_doc.get('kind')!r}/"
                f"{current_doc.get('key')!r}/{current_doc.get('version')!r} (head says {head_version})"
            )

    if db.ledger.count_documents({"kind": "seed"}) < 1:
        problems.append("missing the 'seed' ledger entry")

    return problems


def _seed_is_completely_absent(db: Database) -> bool:
    """True only if NOTHING related to seeding exists yet -- a genuinely fresh database."""
    any_head = db.config_heads.find_one({"_id": {"$in": list(_EXPECTED_SEED_HEAD_IDS)}})
    any_version = db.config_versions.find_one(
        {"_id": {"$in": [f"{head_id}@v1" for head_id in _EXPECTED_SEED_HEAD_IDS]}}
    )
    any_seed_ledger = db.ledger.find_one({"kind": "seed"})
    return any_head is None and any_version is None and any_seed_ledger is None


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
    expected_heads: Optional[dict[str, int]] = None,
) -> dict:
    """THE fence. One transaction: bump the head iff it is still at base_version, insert the new
    version, append a ledger entry, and (if proposal_id) mark that proposal committed.

    Raises StaleVersion if base_version doesn't match the current head, if the target version id
    already exists (a concurrent commit got there first), if a proposal_id is given for a
    proposal that doesn't exist or isn't in an eligible status, or if any head named in
    `expected_heads` has moved off its expected version. Does no model calls.

    Every precondition is checked before any write (see module docstring): the common "caller is
    stale" case never touches the database. The head bump stays a conditional
    find_one_and_update -- the real fence against a genuine concurrent committer on a real
    transaction -- but because mongomock has no rollback, this function tracks exactly what it
    wrote after that bump and undoes all of it (in reverse order) if anything later fails,
    including the proposal update itself losing its own race.
    """
    new_version = base_version + 1
    head_id = _head_id(kind, key)
    version_id = _version_id(kind, key, new_version)

    def _proposal_ineligible_message(proposal: Optional[dict]) -> str:
        status = proposal.get("status") if proposal else "<missing>"
        return f"proposal {proposal_id!r} is not eligible for commit (status={status!r})"

    def _txn(session: Optional[Any]) -> dict:
        # --- Pre-check every fence condition before any write. -----------------------------
        current_head = db.config_heads.find_one({"_id": head_id}, session=session)
        if current_head is None or current_head.get("version") != base_version:
            raise StaleVersion(f"{head_id} is not at base_version {base_version}")

        if db.config_versions.find_one({"_id": version_id}, session=session) is not None:
            raise StaleVersion(f"{version_id} already exists")

        for other_id, expected in sorted((expected_heads or {}).items()):
            other = db.config_heads.find_one({"_id": other_id}, session=session)
            now = other.get("version") if other else None
            if now != expected:
                raise StaleVersion(f"{other_id} is at v{now}, not v{expected} as evaluated")

        proposal_snapshot: Optional[dict] = None
        if proposal_id is not None:
            proposal = db.proposals.find_one({"_id": proposal_id}, session=session)
            if proposal is None or proposal.get("status") not in _ELIGIBLE_PROPOSAL_STATUSES:
                raise StaleVersion(_proposal_ineligible_message(proposal))
            proposal_snapshot = copy.deepcopy(proposal)  # to restore verbatim if we must undo

        # --- The real fence: only now do we write anything. ---------------------------------
        updated_head = db.config_heads.find_one_and_update(
            {"_id": head_id, "version": base_version},
            {"$set": {"version": new_version}},
            session=session,
        )
        if updated_head is None:
            raise StaleVersion(f"{head_id} is not at base_version {base_version}")

        # Track exactly what this invocation writes from here on, so a mongomock-side failure
        # (no real transaction to roll back) can be undone precisely -- and only what WE wrote,
        # never a concurrent writer's data.
        version_written = False
        ledger_entry: Optional[dict] = None
        proposal_flipped = False

        try:
            version_doc = {
                "_id": version_id,
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
                version_written = True
            except DuplicateKeyError as exc:
                raise StaleVersion(f"{version_id} already exists") from exc

            ledger_kind = "rollback" if restores is not None else "commit"
            ledger_entry = ledger_module.append(
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
                result = db.proposals.update_one(
                    {"_id": proposal_id, "status": {"$in": list(_ELIGIBLE_PROPOSAL_STATUSES)}},
                    {"$set": {"status": "committed", "committed_version": new_version}},
                    session=session,
                )
                if result.matched_count != 1:
                    # Closes the race window between the pre-check above and this write (e.g.
                    # a concurrent commit already claimed this proposal on Atlas).
                    raise StaleVersion(
                        f"proposal {proposal_id!r} was no longer eligible when committing"
                    )
                proposal_flipped = True
        except Exception:
            # mongomock has no transaction to roll back what we wrote above -- undo it by hand,
            # in reverse order, so a raised StaleVersion always means "nothing changed". Under a
            # real transaction this is redundant (the whole transaction aborts) but harmless to
            # skip there.
            if is_mock(db):
                if proposal_flipped and proposal_snapshot is not None:
                    db.proposals.replace_one(
                        {"_id": proposal_id}, proposal_snapshot, session=session
                    )
                if ledger_entry is not None:
                    db.ledger.delete_one({"_id": ledger_entry["_id"]}, session=session)
                if version_written:
                    db.config_versions.delete_one({"_id": version_id}, session=session)
                db.config_heads.update_one(
                    {"_id": head_id}, {"$set": {"version": base_version}}, session=session
                )
            raise

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
    """v1 for policy/rules/tools per field, and guardrails:global.

    Idempotent: a no-op if the database is already fully seeded per `_diagnose_seed_state`
    (every v1 doc present and correct, every head present and pointing at a real, matching
    version, and the seed ledger entry present -- NOT "every head is still at exactly v1", since
    a legitimate commit after seeding is expected to move a head past v1). Raises
    SeedInconsistent -- rather than silently no-op'ing or silently re-seeding over it -- if the
    database is only PARTIALLY / inconsistently seeded (e.g. an interrupted seed attempt, a
    non-transactional mongomock run that wrote some but not all documents, or a missing seed
    ledger receipt): that would otherwise leave some fields permanently unconfigured, or the
    audit trail permanently incomplete, with no indication anything is wrong.
    """
    problems = _diagnose_seed_state(db)
    if not problems:
        return  # already fully seeded

    if not _seed_is_completely_absent(db):
        raise SeedInconsistent(
            "config_heads/config_versions/ledger are partially or inconsistently seeded: "
            + "; ".join(problems)
            + ". Refusing to silently treat this as fully seeded or to re-seed over it -- "
            "fix or reset the database explicitly."
        )

    created_at = datetime.now(timezone.utc)
    # A fixed, never-mutated list: with_transaction may invoke `_txn` more than once on a real
    # Atlas retry, and each invocation must record the same 10 keys, not append to a shared list
    # that would double up across retries.
    seeded_keys = list(_EXPECTED_SEED_HEAD_IDS)

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
            {"seeded": seeded_keys},
            sim_time,
            session=session,
        )

    run_txn(db, _txn)
