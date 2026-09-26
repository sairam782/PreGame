"""The data teammate's banker cabinet, wired into Pregame: adapter, harness policy, preps, scoring.

Reads the VISIBLE cabinet only: Atlas database `cabinet` through the same client as `get_db()`, or the
`visible/` folder of cabinet-eval as a files fallback (tests, offline). Never reads the answer side: the preps are
scored by cabinet-eval's `score_preps.py` run as a separate process, and only its aggregate summary comes back.

    data = load_data(get_db().client)            # or load_files(folder)
    facts = build_facts(data)                    # typed, dated, sourced facts per client
    preps = write_preps(data, HARNESS_POLICY)    # 24 preps: a client-file text + claims in the scorer's format
    summary = score(preps)                       # aggregates only, via the scorer subprocess

No model is called here: the notes are few and scripted, so a fixed set of small regexes maps them to facts, and the
claims are rendered from the facts by code. Nothing in this module reaches an existing model prompt, and it keeps its
own constants (contracts.py is only read: APPROVED_LANGUAGE is checked against the data's locked disclosures).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pregame.contracts import APPROVED_LANGUAGE

# ---------------------------------------------------------------------------------------------------------------
# constants (this module's own; contracts.py is not changed)
# ---------------------------------------------------------------------------------------------------------------
CABINET_DB = "cabinet"                      # the visible side only; the answer side is never opened here
LIST_COLLECTIONS = ("clients", "notes", "events", "market", "approved_language", "reference_data", "preps_baseline")
SINGLE_DOC_COLLECTIONS = ("book_summary", "vocabulary")
PREPS_COLLECTION = "cabinet_preps"
RUNS_COLLECTION = "cabinet_runs"

DEFAULT_EVAL_DIR = r"C:\Projects\cabinet-eval"
DEFAULT_SCORER_PYTHON = "C:/Projects/prep-harness/.venv/Scripts/python"

ATTRIBUTES = ("risk_attitude", "investing_style", "decision_maker", "retirement_date", "fee_rate", "disclosure")
STATE_ATTRS = ("risk_attitude", "investing_style", "decision_maker", "retirement_date")   # the client's own state
CLAIM_KINDS = ("observation", "question", "flag", "disclosure")
AUTHORS = ("banker", "junior", "assistant", "client", "reference", "account_record")
HUMAN_AUTHORS = ("banker", "junior")
# Source ranking (fix 3): the banker's notes, the client's own words and actions, the account record and the dated
# reference table outrank a junior's assumption; the assistant's summaries only restate notes, so they rank last.
AUTHOR_RANK = {"banker": 3, "client": 3, "reference": 3, "account_record": 3, "junior": 1, "assistant": 0}

# Used when the data has no vocabulary document (v1 files). v2 publishes `vocabulary`; load_* prefers it.
FALLBACK_VOCABULARY = {
    "risk_attitude": ["moderate", "cautious", "growth", "conservative"],
    "investing_style": ["funds", "index_only", "index_plus_single_stocks", "managed"],
}

_SINCE_OPENING = date(1900, 1, 1)
_TICKER_RE = re.compile(r"\([A-Z]{1,5}\)\s*$")

# ---------------------------------------------------------------------------------------------------------------
# policy: bounded knobs the self-improvement loop may tune next
# ---------------------------------------------------------------------------------------------------------------
# type "bool" knobs switch a fix on or off; "int" knobs carry bounds, and None means "off" (the naive setting).
# tunable=False marks the knobs the handoff freezes (source ranking, approved wording, the no-changes rule).
POLICY_KNOBS: dict[str, dict] = {
    "source_ranking": {"type": "bool", "tunable": False, "fix": "wrong_decision_maker",
                       "doc": "banker note / client's own words outrank a junior's assumption and the assistant's copies"},
    "label_expiry_days": {"type": "int", "bounds": (30, 180), "tunable": True, "fix": "sticky_label",
                          "doc": "a behaviour label older than this becomes a call question, never a first line"},
    "contradiction_threshold": {"type": "int", "bounds": (1, 5), "tunable": True, "fix": "said_vs_did",
                                "doc": "single-stock buys since a stated style before the prep asks about it"},
    "ask_on_conflict": {"type": "bool", "tunable": False, "fix": "sticky_label/said_vs_did",
                        "doc": "when the evidence disagrees with the latest human statement, ask on the call"},
    "no_changes_is_contact_only": {"type": "bool", "tunable": False, "fix": "stale_overwrite",
                                   "doc": "a junior's 'No changes' is a contact record, never a fact that wipes a plan"},
    "flag_no_changes_vs_activity": {"type": "bool", "tunable": True, "fix": "missed_change",
                                    "doc": "flag a 'No changes' note when the client's state moved since the last contact"},
    "fee_from_reference": {"type": "bool", "tunable": False, "fix": "stale_reference",
                           "doc": "quote the fee from the dated reference table, never from notes"},
    "brief_both_holders_on_conflict": {"type": "bool", "tunable": True, "fix": "wrong_decision_maker",
                                       "doc": "on a joint account whose sources disagree, name both holders"},
    "disclosures_locked": {"type": "bool", "tunable": False, "fix": "compliance_drift",
                           "doc": "insert the approved sentences word for word; never copy earlier wording"},
}

NAIVE_POLICY: dict[str, Any] = {
    "name": "naive",
    "source_ranking": False,            # latest note wins, whoever wrote it
    "label_expiry_days": None,          # labels never expire
    "contradiction_threshold": None,    # never compares the stated style with the trades
    "ask_on_conflict": False,
    "no_changes_is_contact_only": False,  # "No changes" wipes the plan on file
    "flag_no_changes_vs_activity": False,
    "fee_from_reference": False,        # fee from the latest note that mentions one
    "brief_both_holders_on_conflict": False,
    "disclosures_locked": False,        # copies its own earlier disclosure wording
}

HARNESS_POLICY: dict[str, Any] = {
    "name": "harness",
    "source_ranking": True,
    "label_expiry_days": 90,
    "contradiction_threshold": 1,
    "ask_on_conflict": True,
    "no_changes_is_contact_only": True,
    "flag_no_changes_vs_activity": True,
    "fee_from_reference": True,
    "brief_both_holders_on_conflict": True,
    "disclosures_locked": True,
}


def validate_policy(policy: dict) -> dict:
    """Raise ValueError on an unknown knob, a wrong type or a value outside its bounds; return the policy."""
    for key, value in policy.items():
        if key == "name":
            continue
        spec = POLICY_KNOBS.get(key)
        if spec is None:
            raise ValueError(f"unknown policy knob {key!r}")
        if spec["type"] == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a bool, got {value!r}")
        else:
            if value is None:
                continue
            lo, hi = spec["bounds"]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise ValueError(f"{key} must be an int in [{lo}, {hi}] or None, got {value!r}")
    missing = set(POLICY_KNOBS) - set(policy)
    if missing:
        raise ValueError(f"policy is missing knobs: {sorted(missing)}")
    return policy


# ---------------------------------------------------------------------------------------------------------------
# loading (visible side only)
# ---------------------------------------------------------------------------------------------------------------
def eval_dir() -> Path:
    return Path(os.environ.get("PREGAME_CABINET_EVAL", DEFAULT_EVAL_DIR))


def visible_dir() -> Path:
    return eval_dir() / "data" / "banker_sim_small" / "out" / "visible"


def _day(value: Any) -> Optional[date]:
    """A calendar date from a BSON date (Atlas), an ISO string (files), extended JSON, or a date."""
    if value is None:
        return None
    if isinstance(value, dict) and "$date" in value:
        value = value["$date"]
        if isinstance(value, dict):          # {"$date": {"$numberLong": "..."}}
            value = datetime.fromtimestamp(int(value["$numberLong"]) / 1000, tz=timezone.utc)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
    return date.fromisoformat(str(value)[:10])


def _normalize(raw: dict, source: str) -> dict:
    """Parse every date field to a `date`, drop Mongo `_id`s, sort notes and events by (date, id)."""
    data: dict[str, Any] = {"source": source}
    for name in LIST_COLLECTIONS:
        docs = [{k: v for k, v in d.items() if k != "_id"} for d in (raw.get(name) or [])]
        data[name] = docs
    for n in data["notes"]:
        n["date"] = _day(n["date"])
    for e in data["events"] + data["market"]:
        e["date"] = _day(e["date"])
    for p in data["preps_baseline"]:
        p["date"] = _day(p["date"])
    for r in data["reference_data"]:
        r["valid_from"] = _day(r.get("valid_from"))
        r["valid_to"] = _day(r.get("valid_to"))
    book = raw.get("book_summary") or {}
    book = {k: v for k, v in book.items() if k != "_id"}
    if book.get("last_updated") is not None:
        book["last_updated"] = _day(book["last_updated"])
    data["book_summary"] = book
    vocab = raw.get("vocabulary") or {}
    data["vocabulary"] = {k: v for k, v in vocab.items() if k != "_id"}
    data["notes"].sort(key=lambda n: (n["date"], n["note_id"]))
    data["events"].sort(key=lambda e: (e["date"], e["event_id"]))
    data["preps_baseline"].sort(key=lambda p: p["prep_id"])
    return data


def load_files(folder: Optional[Path] = None) -> dict:
    folder = Path(folder) if folder else visible_dir()
    raw: dict[str, Any] = {}
    for name in LIST_COLLECTIONS + SINGLE_DOC_COLLECTIONS:
        path = folder / f"{name}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                raw[name] = json.load(f)
    return _normalize(raw, f"files:{folder}")


def load_atlas(client) -> dict:
    """Read the visible collections of database `cabinet` through `client` (the one behind get_db())."""
    db = client[CABINET_DB]
    raw: dict[str, Any] = {}
    for name in LIST_COLLECTIONS:
        raw[name] = list(db[name].find({}))
    for name in SINGLE_DOC_COLLECTIONS:
        raw[name] = db[name].find_one({}) or {}
    if not raw["clients"] or not raw["notes"]:
        raise RuntimeError(f"database {CABINET_DB!r} has no clients or notes")
    return _normalize(raw, f"atlas:{CABINET_DB}")


def load_data(client=None, folder: Optional[Path] = None) -> dict:
    """Atlas `cabinet` when a client is given and reachable, else the visible files."""
    if client is not None:
        try:
            return load_atlas(client)
        except Exception as exc:  # unreachable cluster, empty database: fall back, and say so in data["source"]
            data = load_files(folder)
            data["source"] += f" (atlas unavailable: {type(exc).__name__})"
            return data
    return load_files(folder)


def vocabulary(data: dict) -> dict:
    vocab = data.get("vocabulary") or {}
    out = {}
    for attr, fallback in FALLBACK_VOCABULARY.items():
        values = vocab.get(attr)
        out[attr] = list(values) if isinstance(values, (dict, list)) and values else list(fallback)
    return out


# ---------------------------------------------------------------------------------------------------------------
# adapter: clients -> accounts, notes/events/reference -> typed dated facts
# ---------------------------------------------------------------------------------------------------------------
def accounts(data: dict) -> dict[str, dict]:
    """client_id -> {name, account_id, type, holders, advisory, joint, holdings}."""
    out = {}
    for c in data["clients"]:
        acct = (c.get("accounts") or [{}])[0]
        holders = list(acct.get("holders") or [h["name"] for h in c.get("household", [])])
        out[c["client_id"]] = {
            "client_id": c["client_id"], "name": c["name"], "tier": c.get("tier"),
            "account_id": acct.get("account_id"), "type": acct.get("type"), "holders": holders,
            "advisory": bool(acct.get("advisory")), "joint": len(holders) > 1,
            "holdings": [dict(h) for h in c.get("holdings", [])],
        }
    return out


def locked_disclosures(data: dict) -> dict[str, str]:
    """The data's locked approved language, by claim id."""
    return {a["claim_id"]: a["text"] for a in data["approved_language"] if a.get("locked", True)}


def check_disclosures(data: dict) -> bool:
    """True when the data's locked disclosures equal contracts.APPROVED_LANGUAGE word for word."""
    return locked_disclosures(data) == dict(APPROVED_LANGUAGE)


def instrument_type(event: dict) -> str:
    """fund / index_fund / single_stock: the data's own field when present (v2), else a name heuristic (v1)."""
    kind = event.get("instrument_type")
    if kind:
        return kind
    name = event.get("instrument", "")
    if _TICKER_RE.search(name):
        return "single_stock"
    return "index_fund" if "index" in name.lower() else "fund"


# (attribute, pattern, value, behaviour_label). First match per attribute wins, so order matters.
NOTE_RULES: tuple[tuple[str, str, Any, bool], ...] = (
    ("risk_attitude", r"\bpanic seller\b", "cautious", True),
    ("risk_attitude", r"\bsold everything\b|\brattled\b", "cautious", True),
    ("risk_attitude", r"\bheld steady\b|\badding to equities\b", "growth", True),
    ("risk_attitude", r"\bmoderate risk\b", "moderate", False),
    ("risk_attitude", r"\bconservative\b", "conservative", False),
    ("investing_style", r"\bonly do index funds\b|\bindex-only\b|\bindex only\b|\bindex portfolio\b", "index_only", False),
    ("investing_style", r"\bmanaged advisory\b|\badvisory account\b|\bday-to-day to us\b", "managed", False),
    ("investing_style", r"\bfund investor\b|\bfunds only\b|\bin funds\b|\bbalanced fund\b|\bher funds\b", "funds", False),
)
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), start=1)}


def _full_name(first: str, holders: list[str]) -> Optional[str]:
    for h in holders:
        if h.split()[0].lower() == first.lower():
            return h
    return None


def _is_no_changes(note: dict) -> bool:
    return note["author"] == "junior" and bool(re.search(r"\bno changes\b", note["text"], re.I))


def _fact(note_or_event_id, client_id, attribute, value, kind, author, valid_from, text, label=False,
          valid_to=None, **extra) -> dict:
    f = {"fact_id": f"{note_or_event_id}:{attribute}", "client_id": client_id, "attribute": attribute,
         "value": value, "kind": kind, "author": author, "source_id": note_or_event_id,
         "valid_from": valid_from, "valid_to": valid_to, "label": label, "text": text}
    f.update(extra)
    return f


def note_facts(note: dict, acct: dict) -> list[dict]:
    """The facts one note states. A junior's "No changes" yields only a contact record."""
    text, author, nid, cid, day = note["text"], note["author"], note["note_id"], note["client_id"], note["date"]
    out: list[dict] = []
    if author in HUMAN_AUTHORS:
        out.append(_fact(nid, cid, "contact", "no_changes" if _is_no_changes(note) else "note", "contact",
                         author, day, text))
    if _is_no_changes(note):
        return out
    kind_for = "label" if author == "assistant" else "statement"
    seen = set()
    for attr, pattern, value, behaviour in NOTE_RULES:
        if attr in seen or not re.search(pattern, text, re.I):
            continue
        seen.add(attr)
        out.append(_fact(nid, cid, attr, value, "label" if behaviour else kind_for, author, day, text,
                         label=behaviour or author == "assistant"))
    holders = acct["holders"]
    m = re.search(r"Decision maker: ([A-Z][a-z]+ [A-Z][a-z]+)", text)
    dm = m.group(1) if m else None
    if dm is None:
        m = re.search(r"(\w+) (?:handles|runs) the household finances", text)
        if m:
            who = m.group(1)
            if who.lower() in ("he", "she"):
                spoke = re.search(r"Spoke with (\w+)", text)
                who = spoke.group(1) if spoke else who
            dm = _full_name(who, holders)
    if dm is None:
        m = re.search(r"Spoke with (\w+)\.\s*(?:He|She) makes the decisions", text)
        if m:
            dm = _full_name(m.group(1), holders)
    if dm:
        out.append(_fact(nid, cid, "decision_maker", dm, kind_for if author != "junior" else "label", author, day,
                         text, label=author in ("assistant", "junior")))
    m = re.search(r"plans to retire (\w+) (\d{4})", text, re.I)
    if m and m.group(1).lower() in _MONTHS:
        out.append(_fact(nid, cid, "retirement_date", f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}", "plan",
                         author, day, text))
    elif re.search(r"\bno retirement plans?\b", text, re.I):
        out.append(_fact(nid, cid, "retirement_date", None, "plan", author, day, text))
    m = re.search(r"\bfee (\d+(?:\.\d+)?)%", text, re.I)
    if m:
        out.append(_fact(nid, cid, "fee_rate", float(m.group(1)), "fee_note", author, day, text))
    return out


def event_facts(event: dict, acct: dict) -> list[dict]:
    """Feed events that bear on a client: trades, questionnaire/tool results, client emails, reply lags."""
    eid, cid, day, typ = event["event_id"], event.get("client_id"), event["date"], event["type"]
    if typ in ("trade_buy", "trade_sell"):
        side = "buy" if typ == "trade_buy" else "sell"
        itype = instrument_type(event)
        value = {"side": side, "instrument": event.get("instrument"), "instrument_type": itype,
                 "amount": event.get("amount")}
        return [_fact(eid, cid, "trade", value, "trade", "client", day,
                      f"{side} {event.get('instrument')} ${event.get('amount', 0):,} ({itype})")]
    if typ == "tool_used":
        tool = event.get("tool", "")
        if tool == "risk questionnaire" and event.get("result"):
            return [_fact(eid, cid, "risk_attitude", event["result"], "questionnaire", "client", day,
                          f"risk questionnaire result: {event['result']}")]
        who = f" by {event['user']}" if event.get("user") else ""
        return [_fact(eid, cid, "activity", tool, "tool", "client", day, f"used the {tool}{who}")]
    if typ == "email_to_banker":
        sender = event.get("sender")
        out = [_fact(eid, cid, "activity", event.get("topic"), "email", "client", day,
                     f"email from {sender}: {event.get('text', '')}", sender=sender)]
        if acct["joint"] and sender in acct["holders"]:
            # the holder who writes in to instruct the bank is acting on the account
            out.append(_fact(eid, cid, "decision_maker", sender, "email", "client", day,
                             f"{sender} emailed an instruction ({event.get('topic')})"))
        return out
    if typ == "reply_lag":
        return [_fact(eid, cid, "activity", "reply_lag", "reply_lag", "client", day,
                      f"reply after {event.get('days')} days: {event.get('note', '')}")]
    return []


def build_facts(data: dict) -> list[dict]:
    """Every visible fact, in date order: account records, notes, feed events, reference fees."""
    accts = accounts(data)
    facts: list[dict] = []
    for cid, a in accts.items():
        if not a["joint"]:
            facts.append(_fact(a["account_id"], cid, "decision_maker", a["holders"][0], "holder", "account_record",
                               _SINCE_OPENING, f"sole holder of {a['account_id']} ({a['type']})"))
    for n in data["notes"]:
        if n["client_id"] in accts:
            facts.extend(note_facts(n, accts[n["client_id"]]))
    for e in data["events"]:
        if e.get("client_id") in accts:
            facts.extend(event_facts(e, accts[e["client_id"]]))
    for r in data["reference_data"]:
        if r.get("item") == "advisory_fee":
            facts.append(_fact(r["ref_id"], None, "fee_rate", r["value_pct"], "reference", "reference",
                               r["valid_from"], f"advisory fee {r['value_pct']}% (reference table {r['ref_id']})",
                               valid_to=r["valid_to"]))
    facts.sort(key=lambda f: (f["valid_from"], f["source_id"]))
    return facts


# ---------------------------------------------------------------------------------------------------------------
# resolving the current state of one attribute under a policy
# ---------------------------------------------------------------------------------------------------------------
def _known(facts: list[dict], cid: str, attr: str, day: date) -> list[dict]:
    """Facts about (client, attribute) written strictly before the prep day."""
    return [f for f in facts if f["client_id"] == cid and f["attribute"] == attr and f["valid_from"] < day]


def _latest(fs: list[dict]) -> Optional[dict]:
    return max(fs, key=lambda f: (f["valid_from"], f["source_id"])) if fs else None


def _no_changes_after(facts, cid, since: date, day: date) -> Optional[dict]:
    hits = [f for f in facts if f["client_id"] == cid and f["attribute"] == "contact" and f["value"] == "no_changes"
            and since < f["valid_from"] < day]
    return _latest(hits)


def _expired(fact: dict, day: date, policy: dict) -> bool:
    days = policy.get("label_expiry_days")
    return bool(fact["label"] and days is not None and (day - fact["valid_from"]).days > days)


def resolve(facts: list[dict], cid: str, attr: str, day: date, policy: dict) -> Optional[dict]:
    """The fact the prep should state for `attr` on `day`, or None. The result may carry `expired: True`."""
    known = _known(facts, cid, attr, day)
    if not policy["source_ranking"]:
        # naive: the latest note wins, whoever wrote it (feed events and the account record are not read)
        notes = [f for f in known if f["source_id"].startswith("N-")]
        chosen = _latest(notes)
        if attr == "retirement_date" and chosen is not None and not policy["no_changes_is_contact_only"]:
            wipe = _no_changes_after(facts, cid, chosen["valid_from"], day)   # a later "No changes" wipes the plan
            if wipe is not None:
                return dict(wipe, attribute=attr, value=None, kind="wiped", fact_id=f"{wipe['source_id']}:{attr}")
        return dict(chosen) if chosen else None
    if not known:
        return None
    top = max(AUTHOR_RANK.get(f["author"], 0) for f in known)
    chosen = dict(_latest([f for f in known if AUTHOR_RANK.get(f["author"], 0) == top]))
    if _expired(chosen, day, policy):
        chosen["expired"] = True
    return chosen


def single_stock_buys(facts, cid, since: date, day: date) -> list[dict]:
    return [f for f in facts if f["client_id"] == cid and f["attribute"] == "trade" and since <= f["valid_from"] < day
            and f["value"]["side"] == "buy" and f["value"]["instrument_type"] == "single_stock"]


def current_state(facts: list[dict], cid: str, attr: str, day: date, policy: dict) -> dict:
    """{"fact": the resolved fact or None, "value": the value the prep states now (None = no statement),
    "evidence": extra basis ids, "contradicted": bool}."""
    fact = resolve(facts, cid, attr, day, policy)
    out = {"fact": fact, "value": None if fact is None or fact.get("expired") else fact["value"],
           "evidence": [], "contradicted": False}
    threshold = policy.get("contradiction_threshold")
    if attr == "investing_style" and fact and threshold is not None and fact["value"] in ("index_only", "funds"):
        buys = single_stock_buys(facts, cid, fact["valid_from"], day)
        if len(buys) >= threshold:
            out["contradicted"] = True
            out["evidence"] = [b["source_id"] for b in buys]
            out["value"] = "index_plus_single_stocks" if fact["value"] == "index_only" else None
    return out


def _latest_human(facts, cid, attr, day) -> Optional[dict]:
    return _latest([f for f in _known(facts, cid, attr, day)
                    if f["author"] in HUMAN_AUTHORS and f["value"] is not None])


# ---------------------------------------------------------------------------------------------------------------
# preps: the client file (text) and its claims
# ---------------------------------------------------------------------------------------------------------------
_ATTR_TITLES = {"risk_attitude": "Risk attitude", "investing_style": "Investing style",
                "decision_maker": "Decision maker", "retirement_date": "Retirement plan", "fee_rate": "Advisory fee"}


def _holdings_on(acct: dict, facts: list[dict], day: date) -> list[tuple[str, float]]:
    """Opening holdings with the feed's trades applied up to the prep day."""
    book: dict[str, float] = {}
    for h in acct["holdings"]:
        book[h["instrument"]] = book.get(h["instrument"], 0) + h["amount"]
    for f in facts:
        if f["client_id"] == acct["client_id"] and f["attribute"] == "trade" and f["valid_from"] < day:
            sign = 1 if f["value"]["side"] == "buy" else -1
            name = f["value"]["instrument"]
            book[name] = book.get(name, 0) + sign * (f["value"]["amount"] or 0)
    return [(k, v) for k, v in book.items() if v > 0]


def _copied_disclosures(data: dict, cid: str, day: date) -> Optional[list[str]]:
    """The assistant's own disclosure sentences from its latest earlier prep for this client (naive copying)."""
    preps = [p for p in data["preps_baseline"] if p["client_id"] == cid and p["date"] <= day]
    if not preps:
        return None
    text = max(preps, key=lambda p: p["date"])["text"]
    i = text.find("Disclosures:")
    if i < 0:
        return None
    return [s for s in re.split(r"(?<=[.!?])\s+", text[i + len("Disclosures:"):].strip()) if s]


def _fmt_value(v: Any) -> str:
    if isinstance(v, list):
        return " and ".join(v)
    return str(v)


def _line(fact: dict) -> str:
    return f"since {fact['valid_from'].isoformat() if fact['valid_from'] != _SINCE_OPENING else 'opening'}, " \
           f"{fact['author']}, {fact['source_id']}"


def write_prep(data: dict, facts: list[dict], cid: str, day: date, policy: dict, prep_id: str) -> dict:
    """One prep: deterministic client-file text plus current-state claims in score_preps.py's format."""
    acct = accounts(data)[cid]
    claims: list[dict] = []
    file_lines: list[str] = []
    questions: list[str] = []
    flags: list[str] = []
    history: list[str] = []
    asked: set[str] = set()

    def ask(attr: str, basis: list[str], why: str) -> None:
        if attr in asked:
            return
        asked.add(attr)
        claims.append({"attribute": attr, "value": None, "kind": "question", "basis": sorted(set(basis))})
        questions.append(f"{_ATTR_TITLES[attr]}: {why}")

    for attr in STATE_ATTRS:
        st = current_state(facts, cid, attr, day, policy)
        fact, value = st["fact"], st["value"]
        if fact is None:
            continue
        basis = [fact["source_id"]] + st["evidence"]
        human = _latest_human(facts, cid, attr, day)
        if human and human["source_id"] != fact["source_id"] and policy["source_ranking"]:
            history.append(f"{human['valid_from'].isoformat()} {human['source_id']} ({human['author']}): "
                           f"{_ATTR_TITLES[attr].lower()} stated as {_fmt_value(human['value'])}")

        # decision maker on a joint account: brief both holders when human/client sources disagree
        if attr == "decision_maker" and acct["joint"] and policy["brief_both_holders_on_conflict"]:
            rivals = {f["value"] for f in _known(facts, cid, attr, day)
                      if f["author"] in ("banker", "junior", "client") and f["value"] and f["value"] != value}
            if rivals:
                both = [h for h in acct["holders"]]
                rival_ids = [f["source_id"] for f in _known(facts, cid, attr, day)
                             if f["value"] in rivals and f["author"] in ("banker", "junior", "client")]
                claims.append({"attribute": attr, "value": both, "kind": "observation",
                               "basis": sorted(set(basis + rival_ids))})
                file_lines.append(f"- Decision maker: brief both holders ({', '.join(both)}). The ranked sources "
                                  f"say {value} ({_line(fact)}); lower-ranked notes say {', '.join(sorted(rivals))} "
                                  f"({', '.join(sorted(set(rival_ids)))}).")
                continue

        if fact.get("expired"):
            ask(attr, basis, f"the label '{_fmt_value(fact['value'])}' ({_line(fact)}) is older than "
                             f"{policy['label_expiry_days']} days; reconfirm before relying on it.")
            continue
        if value is None and not (fact.get("kind") == "wiped" and attr == "retirement_date"):
            if st["contradicted"]:
                ask(attr, basis, f"stated {_fmt_value(fact['value'])} ({_line(fact)}), but the feed shows "
                                 f"single-stock buys since ({', '.join(st['evidence'])}). Has the approach changed?")
            continue
        claims.append({"attribute": attr, "value": value, "kind": "observation", "basis": sorted(set(basis))})
        shown = "no plan on file" if value is None else _fmt_value(value)
        file_lines.append(f"- {_ATTR_TITLES[attr]}: {shown} ({_line(fact)})")
        if st["contradicted"] and policy["ask_on_conflict"]:
            ask(attr, basis, f"stated {_fmt_value(fact['value'])} ({_line(fact)}), but the feed shows "
                             f"{len(st['evidence'])} single-stock buy(s) since ({', '.join(st['evidence'])}). "
                             f"Has his approach changed?")
        elif policy["ask_on_conflict"] and human and human["value"] != value and attr != "decision_maker":
            ask(attr, [human["source_id"], fact["source_id"]],
                f"the file says {_fmt_value(human['value'])} ({_line(human)}), newer evidence says "
                f"{_fmt_value(value)} ({_line(fact)}). Confirm on the call.")

    # "No changes" logged although the client's state moved since the previous contact (fix 5)
    if policy["flag_no_changes_vs_activity"]:
        nc = _latest([f for f in facts if f["client_id"] == cid and f["attribute"] == "contact"
                      and f["value"] == "no_changes" and f["valid_from"] < day])
        if nc:
            prev = _latest([f for f in facts if f["client_id"] == cid and f["attribute"] == "contact"
                            and f["valid_from"] < nc["valid_from"]])
            before = (prev["valid_from"] if prev else _SINCE_OPENING) + timedelta(days=1)
            after = nc["valid_from"] + timedelta(days=1)
            for attr in STATE_ATTRS:
                a = current_state(facts, cid, attr, before, policy)
                b = current_state(facts, cid, attr, after, policy)
                if a["value"] is not None and b["value"] is not None and a["value"] != b["value"]:
                    ev = [b["fact"]["source_id"]] + b["evidence"]
                    claims.append({"attribute": attr, "value": None, "kind": "flag",
                                   "basis": sorted(set([nc["source_id"]] + ev))})
                    flags.append(f"{nc['source_id']} ({nc['author']}, {nc['valid_from'].isoformat()}) logged "
                                 f"'No changes', but {_ATTR_TITLES[attr].lower()} moved from {_fmt_value(a['value'])} "
                                 f"to {_fmt_value(b['value'])} since the previous contact ({', '.join(ev)}). "
                                 f"Flag for the banker.")

    # fee: advisory accounts only
    if acct["advisory"]:
        if policy["fee_from_reference"]:
            refs = [f for f in facts if f["attribute"] == "fee_rate" and f["author"] == "reference"
                    and f["valid_from"] <= day and (f["valid_to"] is None or day <= f["valid_to"])]
            fee = _latest(refs)
        else:
            fee = _latest([f for f in _known(facts, cid, "fee_rate", day)])
        if fee:
            claims.append({"attribute": "fee_rate", "value": fee["value"], "kind": "observation",
                           "basis": [fee["source_id"]]})
            file_lines.append(f"- Advisory fee: {fee['value']}% ({_line(fee)})")

    # disclosures: the locked text by id (harness), or the assistant's own earlier wording (naive)
    locked = locked_disclosures(data)
    ids = sorted(locked)
    texts = {i: locked[i] for i in ids}
    if not policy["disclosures_locked"]:
        copied = _copied_disclosures(data, cid, day)
        if copied:
            texts = {i: copied[n] if n < len(copied) else locked[i] for n, i in enumerate(ids)}
    for i in ids:
        claims.append({"attribute": "disclosure", "value": i, "kind": "disclosure", "text": texts[i]})

    holdings = "; ".join(f"{k} ${v:,.0f}" for k, v in _holdings_on(acct, facts, day)) or "none"
    lines = [f"CALL PREP: {acct['name']} ({day.isoformat()})  [Pregame cabinet, policy={policy.get('name', '?')}]",
             f"Account {acct['account_id']}: {acct['type']}; holders: {', '.join(acct['holders'])}",
             f"Holdings (feed trades applied): {holdings}",
             "", "Client file (current view; each line dated and sourced):"]
    lines += file_lines or ["- (nothing on file)"]
    lines += ["", "Call questions:"] + ([f"- {q}" for q in questions] or ["- (none)"])
    lines += ["", "Watch-outs:"] + ([f"- {x}" for x in flags] or ["- (none)"])
    if history:
        lines += ["", "History (dated; not current-state claims):"] + [f"- {h}" for h in history]
    lines += ["", "Disclosures:"] + [f"- {texts[i]} ({i})" for i in ids]
    return {"prep_id": prep_id, "client_id": cid, "date": day.isoformat(), "text": "\n".join(lines),
            "claims": claims}


def prep_slots(data: dict) -> list[tuple[str, str, date]]:
    """(prep_id, client_id, date) for each baseline prep, in the baseline's order."""
    return [(p["prep_id"], p["client_id"], p["date"]) for p in data["preps_baseline"]]


def write_preps(data: dict, policy: dict, facts: Optional[list[dict]] = None) -> list[dict]:
    """The preps for the baseline's 24 slots. Each keeps its slot's prep_id (P-0001...): the answer key lists
    its expected actions (ask / flag / brief_both) by that id, so another id would never be credited."""
    validate_policy(policy)
    facts = facts if facts is not None else build_facts(data)
    return [write_prep(data, facts, cid, day, policy, prep_id) for prep_id, cid, day in prep_slots(data)]


def validate_claims(prep: dict, data: Optional[dict] = None) -> list[str]:
    """Problems with a prep's claims against the scorer's input format ([] when valid)."""
    problems = []
    for k in ("prep_id", "client_id", "date", "text", "claims"):
        if k not in prep:
            problems.append(f"missing {k}")
    vocab = vocabulary(data) if data else FALLBACK_VOCABULARY
    holders = accounts(data)[prep["client_id"]]["holders"] if data else None
    for c in prep.get("claims", []):
        attr, kind, val = c.get("attribute"), c.get("kind"), c.get("value")
        if attr not in ATTRIBUTES:
            problems.append(f"bad attribute {attr!r}")
        if kind not in CLAIM_KINDS:
            problems.append(f"bad kind {kind!r}")
        if kind in ("question", "flag"):
            if val is not None:
                problems.append(f"{kind} on {attr} carries a value")
            continue
        if kind == "disclosure" and (attr != "disclosure" or not c.get("text")):
            problems.append("disclosure claim without text")
        if attr in vocab and val not in vocab[attr]:
            problems.append(f"{attr} value {val!r} not in the vocabulary")
        if attr == "decision_maker" and holders is not None:
            names = val if isinstance(val, list) else [val]
            if any(n not in holders for n in names):
                problems.append(f"decision_maker {val!r} is not a holder")
        if attr == "retirement_date" and val is not None and not re.fullmatch(r"\d{4}-\d{2}", str(val)):
            problems.append(f"retirement_date {val!r} is not YYYY-MM")
        if attr == "fee_rate" and not isinstance(val, (int, float)):
            problems.append(f"fee_rate {val!r} is not a number")
    return problems


# ---------------------------------------------------------------------------------------------------------------
# scoring: aggregates only, from the scorer run as a separate process
# ---------------------------------------------------------------------------------------------------------------
# The summary keys kept. Per-prep and per-client output is dropped: it names the truth.
SUMMARY_KEYS = ("preps", "preps_with_a_fault", "trusted_prep_rate", "preps_with_a_fault_ignoring_warnings",
                "trusted_prep_rate_ignoring_warnings", "faults_total", "warnings_total", "faults_by_type",
                "compliance_drift_by_severity", "forbidden_promises", "history_claims", "expected_actions",
                "expected_actions_by_type", "actions_hit", "actions_hit_by_type", "action_recall",
                "questions_expected", "questions_hit", "question_recall", "questions_unneeded")


def _scorer_python() -> str:
    exe = os.environ.get("PREGAME_CABINET_PYTHON", DEFAULT_SCORER_PYTHON)
    return exe if Path(exe).exists() or Path(exe + ".exe").exists() else sys.executable


def _run_scorer(args: list[str]) -> dict:
    scorer = eval_dir() / "score_preps.py"
    if not scorer.exists():
        raise FileNotFoundError(f"scorer not found: {scorer}")
    r = subprocess.run([_scorer_python(), str(scorer)] + args, capture_output=True, text=True,
                       cwd=str(eval_dir()), timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"score_preps.py failed ({r.returncode}): {r.stderr.strip()[-500:]}")
    summary = json.loads(r.stdout)["summary"]
    return {k: summary[k] for k in SUMMARY_KEYS if k in summary}


def _jsonable(preps: list[dict]) -> list[dict]:
    return [{"prep_id": p["prep_id"], "client_id": p["client_id"], "date": p["date"], "text": p["text"],
             "claims": p["claims"]} for p in preps]


def score(preps: list[dict]) -> dict:
    """Aggregate scores for `preps` (never per-prep results): writes them to a temp JSON, runs the scorer."""
    fd, path = tempfile.mkstemp(prefix="pregame_cabinet_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_jsonable(preps), f)
        return _run_scorer([path])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def score_baseline() -> dict:
    return _run_scorer(["--baseline"])


# ---------------------------------------------------------------------------------------------------------------
# storage (the pregame database; never the cabinet databases)
# ---------------------------------------------------------------------------------------------------------------
def ensure_indexes(db) -> None:
    from pymongo import ASCENDING, DESCENDING
    db[PREPS_COLLECTION].create_index([("run_id", ASCENDING), ("prep_id", ASCENDING)], unique=True,
                                      name="run_prep")
    db[PREPS_COLLECTION].create_index([("client_id", ASCENDING), ("date", ASCENDING)], name="client_date")
    db[RUNS_COLLECTION].create_index([("created_at", DESCENDING)], name="created_at_desc")


def _policy_doc(policy: dict) -> dict:
    return {k: policy[k] for k in ["name"] + list(POLICY_KNOBS) if k in policy}


def store_run(db, preps: list[dict], policy: dict, scores: dict, data_source: str,
              now: Optional[datetime] = None) -> dict:
    """Store the policy's preps in cabinet_preps and a receipt in cabinet_runs; return the receipt."""
    if db.name in (CABINET_DB, CABINET_DB + "_truth"):
        raise ValueError(f"refusing to write into {db.name!r}")
    ensure_indexes(db)
    now = now or datetime.now(timezone.utc)
    run_id = f"CR-{now.strftime('%Y%m%dT%H%M%S%fZ')}"
    docs = [{"_id": f"{run_id}:{p['prep_id']}", "run_id": run_id, "policy_name": policy.get("name"),
             "prep_id": p["prep_id"], "client_id": p["client_id"], "date": p["date"], "text": p["text"],
             "claims": p["claims"], "created_at": now} for p in preps]
    if docs:
        db[PREPS_COLLECTION].insert_many(docs)
    receipt = {"_id": run_id, "created_at": now, "data_source": data_source, "policy": _policy_doc(policy),
               "prep_count": len(docs), "prep_ids": [p["prep_id"] for p in preps],
               "preps_collection": PREPS_COLLECTION, "scores": scores,
               "scorer": str(eval_dir() / "score_preps.py")}
    db[RUNS_COLLECTION].insert_one(receipt)
    return receipt


def run_before_after(data: dict) -> dict:
    """Baseline, naive and harness scores plus the harness preps (the CLI's work, minus printing and storing)."""
    facts = build_facts(data)
    naive = write_preps(data, NAIVE_POLICY, facts)
    harness = write_preps(data, HARNESS_POLICY, facts)
    return {"baseline": score_baseline(), "naive": score(naive), "harness": score(harness),
            "naive_preps": naive, "harness_preps": harness}
