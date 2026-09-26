"""The world in MongoDB: facts (insert-only), event definitions, the simulated clock and the frozen scenarios.

Collections: ``facts``, ``events``, ``clock``, ``eval_scenarios``. Ledger entries go through
``pregame.ledger.append`` (imported lazily so this module loads even when the ledger is not importable).
Datetimes read back from a client without ``tz_aware=True`` are treated as UTC.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from pregame.contracts import FIELDS, Account, Fact, Scenario
from pregame.world import fields as W

CLOCK_ID = "sim"
ACTOR = "world"


def _ledger_append(db, kind: str, actor: str, payload: dict, sim_time: datetime) -> Any:
    from pregame import ledger
    return ledger.append(db, kind, actor, payload, sim_time)


def _aware(dt: Any) -> Any:
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _fix_fact(doc: dict) -> Fact:
    doc = dict(doc)
    doc["valid_from"] = _aware(doc.get("valid_from"))
    return doc  # type: ignore[return-value]


def _event_doc(event: dict, fired: bool = False, fired_at: datetime | None = None) -> dict:
    return {"_id": event["id"], "id": event["id"], "field": event["field"], "title": event["title"],
            "month": event["month"], "at": W.event_time(event), "facts": copy.deepcopy(event["facts"]),
            "feedback": event["feedback"], "fired": fired, "fired_at": fired_at}


def load_world(db) -> None:
    """(Re)initialise the world: month-0 facts, every event definition (unfired), clock at sim_date(0).

    Clears ``facts``, ``events`` and ``clock`` first, so it is safe to call again for a clean start.
    Appends one ledger ``event`` entry for the load.
    """
    start = W.sim_date(0)
    db.facts.delete_many({})
    db.events.delete_many({})
    db.clock.delete_many({})
    base = [copy.deepcopy(f) for field in FIELDS for f in W.BASE_FACTS[field]]
    if base:
        db.facts.insert_many(base)
    events = [_event_doc(e) for field in FIELDS for e in W.EVENTS[field]]
    if events:
        db.events.insert_many(events)
    db.clock.replace_one({"_id": CLOCK_ID}, {"_id": CLOCK_ID, "now": start}, upsert=True)
    _ledger_append(db, "event", ACTOR, {"action": "load_world", "base_facts": len(base), "events": len(events),
                                        "fields": list(FIELDS)}, start)


class FrozenScenarioError(ValueError):
    """A scenario with this _id is already stored with different content; frozen scenarios are never rewritten."""


def _canon(value: Any) -> Any:
    """A comparable form of a stored document: UTC-aware ISO datetimes, lists for tuples, sorted keys."""
    if isinstance(value, datetime):
        return _aware(value).astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def load_scenarios(db, scenarios: list[Scenario]) -> None:
    """Store the frozen evaluation scenarios in ``eval_scenarios``. Insert-only: an existing scenario is never
    rewritten. Reloading an identical scenario is a no-op; reloading a DIFFERENT one under an existing ``_id``
    raises ``FrozenScenarioError`` ("frozen scenario differs") and leaves the stored document untouched."""
    from pymongo.errors import DuplicateKeyError

    for sc in scenarios:
        doc = copy.deepcopy(sc)
        try:
            db.eval_scenarios.insert_one(doc)
        except DuplicateKeyError:
            stored = db.eval_scenarios.find_one({"_id": sc["_id"]})
            if _canon(stored) != _canon(sc):
                raise FrozenScenarioError(f"frozen scenario differs: {sc['_id']} is already stored with "
                                          "different content and cannot be rewritten") from None


def sim_now(db) -> datetime:
    """The simulated clock (SIM_START if the world has not been loaded)."""
    doc = db.clock.find_one({"_id": CLOCK_ID})
    if not doc or not doc.get("now"):
        return W.SIM_START
    return _aware(doc["now"])


def fire_event(db, event_id: str) -> list[Fact]:
    """Land a scripted event: insert its facts, mark it fired, move the clock forward to its time, ledger ``event``.

    Exactly-once: the event is CLAIMED with one conditional update on ``{"_id": event_id, "fired": not True}``;
    only the caller whose update matched inserts the facts and appends the ledger ``event``. A second (or
    concurrent, losing) fire returns [] and writes nothing. Facts are insert-only: an already-stored fact with the
    same ``_id`` is left untouched and not returned. The clock only moves forward (atomic ``$max``), so firing an
    earlier event after a later one keeps the later time. Raises KeyError for an unknown event id.
    """
    from pymongo.errors import DuplicateKeyError

    event = W.event_by_id(event_id)
    at = W.event_time(event)
    if db.events.find_one({"_id": event_id}, {"_id": 1}) is None:     # world not loaded: register it unfired
        try:
            db.events.insert_one(_event_doc(event))
        except DuplicateKeyError:
            pass
    claim = db.events.update_one({"_id": event_id, "fired": {"$ne": True}},
                                 {"$set": {"fired": True, "fired_at": at}})
    if claim.matched_count != 1:
        return []
    inserted: list[Fact] = []
    for f in event["facts"]:
        doc = copy.deepcopy(f)
        try:
            db.facts.insert_one(doc)
        except DuplicateKeyError:
            continue
        inserted.append(copy.deepcopy(f))
    db.clock.update_one({"_id": CLOCK_ID}, {"$max": {"now": at}}, upsert=True)
    _ledger_append(db, "event", ACTOR, {"action": "fire_event", "event_id": event_id, "field": event["field"],
                                        "title": event["title"], "fact_ids": [f["_id"] for f in inserted]}, at)
    return inserted


def facts_until(db, field: str, as_of: datetime) -> list[Fact]:
    """Every stored fact of a field with valid_from <= as_of, oldest first (the compiler decides what is current)."""
    cursor = db.facts.find({"field": field, "valid_from": {"$lte": as_of}}).sort([("valid_from", 1), ("_id", 1)])
    return [_fix_fact(d) for d in cursor]


def list_events(db, field: str | None = None) -> list[dict]:
    """The scripted events (all of them, fired or not), in time order.

    Each: {id, field, title, month, at, kinds, fact_ids, feedback, fired, fired_at}.
    """
    fired: dict[str, dict] = {d["_id"]: d for d in db.events.find({}, {"fired": 1, "fired_at": 1})}
    out = []
    for f in FIELDS if field is None else (field,):
        for e in W.EVENTS[f]:
            state = fired.get(e["id"], {})
            out.append({"id": e["id"], "field": e["field"], "title": e["title"], "month": e["month"],
                        "at": W.event_time(e), "kinds": sorted({x["kind"] for x in e["facts"]}),
                        "fact_ids": [x["_id"] for x in e["facts"]], "feedback": e["feedback"],
                        "fired": bool(state.get("fired")), "fired_at": _aware(state.get("fired_at"))})
    return sorted(out, key=lambda e: (e["at"], FIELDS.index(e["field"])))


def get_account(field: str, account_id: str | None = None) -> Account:
    """An account by id, or the field's demo account (the first) when account_id is None. KeyError if unknown."""
    accounts = W.ACCOUNTS[field]
    if account_id is None:
        return copy.deepcopy(accounts[0])
    for account in accounts:
        if account["id"] == account_id:
            return copy.deepcopy(account)
    raise KeyError(f"no account {account_id!r} in {field}")
