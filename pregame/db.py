"""Connection, schema validators, indexes, transaction helper.

Binding per INTERFACES.md:
- get_db(settings=None) -> Database
- init_db(db) -> None
- run_txn(db, fn) -> Any
- reset_db(db) -> None
- is_mock(db) -> bool

`pregame.config` is owned by another agent and may not exist yet while this file is being
written in parallel. We import it lazily inside get_db and fall back to env var / default.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import CollectionInvalid
from pymongo.read_concern import ReadConcern
from pymongo.server_api import ServerApi
from pymongo.write_concern import WriteConcern

DEFAULT_MONGODB_URI = "mongodb://localhost:27017"
DEFAULT_DB_NAME = "pregame"

# Collections that hold self-modifiable / audited state and get strict $jsonSchema validators.
_VALIDATED_COLLECTIONS = ("proposals", "config_versions", "ledger")

# All collections init_db makes sure exist (validated ones + the rest of the schema).
_ALL_COLLECTIONS = (
    "facts",
    "events",
    "clock",
    "config_versions",
    "config_heads",
    "briefs",
    "feedback",
    "proposals",
    "eval_scenarios",
    "eval_runs",
    "ledger",
)

# ConfigKind / Tier / ProposalStatus enums, mirrored from contracts.py (kept in sync by hand:
# contracts.py is shared and must not be edited from here).
_CONFIG_KINDS = ["policy", "rules", "tools", "guardrails"]
_PROPOSAL_STATUSES = [
    "pending",
    "evaluating",
    "rejected",
    "awaiting_owner",
    "committed",
    "stale",
]
_LEDGER_KINDS = [
    "seed",
    "event",
    "brief",
    "feedback",
    "proposal",
    "eval",
    "reject",
    "commit",
    "approve",
    "rollback",
    "refused",
]


def _resolve_uri() -> str:
    """Best-effort URI resolution: settings module first, then env var, then localhost.

    pregame.config is owned by another agent and being written in parallel with this file,
    so the import is lazy and any failure (ImportError, missing attribute, etc.) falls back
    quietly rather than blowing up get_db for every other module that needs a database.
    """
    try:
        from pregame.config import settings  # noqa: PLC0415 (intentionally lazy)

        return settings().mongodb_uri
    except Exception:
        return os.environ.get("MONGODB_URI", DEFAULT_MONGODB_URI)


def _resolve_db_name() -> str:
    try:
        from pregame.config import settings  # noqa: PLC0415

        return settings().db_name
    except Exception:
        return os.environ.get("PREGAME_DB_NAME", DEFAULT_DB_NAME)


def get_db(settings: Optional[Any] = None) -> Database:
    """Return the pregame Database, connecting with the hackathon-safe client options.

    `settings`, if given, is a `pregame.config.Settings`-shaped object (duck-typed: needs
    `.mongodb_uri` and `.db_name`) so callers who already loaded settings can pass it in and
    skip the lazy import. server_api=ServerApi("1") per DESIGN.md; NEVER strict=True (it
    refuses $search / $vectorSearch on Atlas).
    """
    if settings is not None:
        uri = settings.mongodb_uri
        db_name = getattr(settings, "db_name", DEFAULT_DB_NAME)
    else:
        uri = _resolve_uri()
        db_name = _resolve_db_name()

    client: MongoClient = MongoClient(
        uri,
        server_api=ServerApi("1"),
        appname="pregame",
        tz_aware=True,
    )
    return client[db_name]


def is_mock(db: Database) -> bool:
    """True when `db` is backed by mongomock rather than a real MongoClient."""
    client = db.client
    module = type(client).__module__
    return "mongomock" in module


def run_txn(db: Database, fn: Callable[[Optional[Any]], Any]) -> Any:
    """Run `fn(session)` inside a real transaction, or `fn(None)` under mongomock.

    `fn` must be side-effect free outside the database: with_transaction may invoke it more
    than once before it commits.
    """
    if is_mock(db):
        return fn(None)

    client = db.client
    with client.start_session() as session:
        return session.with_transaction(
            lambda s: fn(s),
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
        )


def reset_db(db: Database) -> None:
    """Drop every pregame collection (tests / demo reset)."""
    for name in _ALL_COLLECTIONS + ("config_heads",):
        db.drop_collection(name)


# ---------------------------------------------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------------------------------------------

def _proposals_schema() -> dict:
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "additionalProperties": True,
            "required": [
                "_id",
                "field",
                "kind",
                "key",
                "base_version",
                "body",
                "rationale",
                "filed_by",
                "idem_key",
                "status",
                "created_sim",
                "created_at",
            ],
            "properties": {
                "_id": {"bsonType": "string"},
                "field": {"bsonType": "string"},
                "kind": {"bsonType": "string"},
                "key": {"bsonType": "string"},
                "base_version": {"bsonType": "int", "minimum": 1},
                "status": {"enum": _PROPOSAL_STATUSES},
                "idem_key": {"bsonType": "string"},
                "created_sim": {"bsonType": "date"},
                "created_at": {"bsonType": "date"},
            },
        }
    }


def _config_versions_schema() -> dict:
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "additionalProperties": True,
            "required": [
                "_id",
                "kind",
                "key",
                "version",
                "body",
                "rationale",
                "created_sim",
                "created_at",
            ],
            "properties": {
                "_id": {"bsonType": "string"},
                "kind": {"enum": _CONFIG_KINDS},
                "key": {"bsonType": "string"},
                "version": {"bsonType": "int", "minimum": 1},
                "created_sim": {"bsonType": "date"},
                "created_at": {"bsonType": "date"},
            },
        }
    }


def _ledger_schema() -> dict:
    # Ledger is the audit trail: strictest of the three, no additionalProperties.
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "additionalProperties": False,
            "required": [
                "_id",
                "seq",
                "prev_hash",
                "hash",
                "kind",
                "actor",
                "payload",
                "sim_time",
                "recorded_at",
            ],
            "properties": {
                "_id": {"bsonType": "int"},
                "seq": {"bsonType": "int", "minimum": 1},
                "prev_hash": {"bsonType": "string"},
                "hash": {"bsonType": "string"},
                "kind": {"enum": _LEDGER_KINDS},
                "actor": {"bsonType": "string"},
                "payload": {"bsonType": "object"},
                "sim_time": {"bsonType": "date"},
                "recorded_at": {"bsonType": "date"},
            },
        }
    }


_SCHEMAS = {
    "proposals": _proposals_schema,
    "config_versions": _config_versions_schema,
    "ledger": _ledger_schema,
}


def _apply_validator(db: Database, name: str) -> None:
    schema = _SCHEMAS[name]()
    if name in db.list_collection_names():
        db.command(
            "collMod",
            name,
            validator=schema,
            validationLevel="strict",
            validationAction="error",
        )
    else:
        db.create_collection(
            name,
            validator=schema,
            validationLevel="strict",
            validationAction="error",
        )


def init_db(db: Database) -> None:
    """Idempotent: create collections if missing, apply strict validators, build indexes.

    Under mongomock, $jsonSchema validators are not enforced by the library the same way
    Atlas enforces them (mongomock's collMod/create_collection validator support is partial
    and inconsistent across versions), so we skip validator creation there and only build
    collections + indexes -- matching INTERFACES.md's "under mongomock, skip validators
    silently".
    """
    mock = is_mock(db)
    existing = set(db.list_collection_names())

    for name in _ALL_COLLECTIONS:
        if name in _VALIDATED_COLLECTIONS:
            if mock:
                if name not in existing:
                    try:
                        db.create_collection(name)
                    except CollectionInvalid:
                        pass
            else:
                _apply_validator(db, name)
        else:
            if name not in existing:
                try:
                    db.create_collection(name)
                except CollectionInvalid:
                    pass

    # config_heads is not in _ALL_COLLECTIONS's validated set but still needs to exist.
    if "config_heads" not in existing:
        try:
            db.create_collection("config_heads")
        except CollectionInvalid:
            pass

    _ensure_indexes(db)


def _ensure_indexes(db: Database) -> None:
    # config_heads._id is naturally unique (it's the _id); nothing extra needed, but an
    # explicit index call is harmless and documents the invariant per INTERFACES.md.
    db.config_heads.create_index([("_id", ASCENDING)], unique=True)

    db.proposals.create_index([("idem_key", ASCENDING)], unique=True)

    db.facts.create_index(
        [
            ("field", ASCENDING),
            ("subject", ASCENDING),
            ("relation", ASCENDING),
            ("valid_from", ASCENDING),
        ]
    )

    db.briefs.create_index([("field", ASCENDING), ("as_of", ASCENDING)])

    db.eval_runs.create_index(
        [("config_hash", ASCENDING), ("scenario_id", ASCENDING), ("run", ASCENDING)],
        unique=True,
    )

    # Helpful, not-in-spec extras that cost nothing and support ledger/version reads.
    db.ledger.create_index([("seq", DESCENDING)], unique=True)
    db.config_versions.create_index([("kind", ASCENDING), ("key", ASCENDING), ("version", DESCENDING)])
