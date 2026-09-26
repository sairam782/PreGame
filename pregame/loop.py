"""Orchestration: wires db + world + compiler/drafter + oracle + improver/gate into one loop.

Binding per INTERFACES.md / the build brief:
- setup(db, llm=None) -> dict
- make_brief(db, field, account_id, llm) -> Brief
- market_event(db, event_id, llm) -> dict
- improve(db, field, llm) -> Proposal | None
- status(db) -> dict  (JSON-safe: datetimes -> ISO strings)
- run_demo(db, llm) -> None

Every import of a sibling module is deferred (function-local) on purpose: this file is being
written while db/ledger/versions/world/compiler/drafter/oracle/improver/gate are written in
parallel by other agents, and loop.py itself must stay importable (for cli.py / web/app.py)
even before all of them exist.
"""
from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Optional


# ---------------------------------------------------------------------------------------------------------------
# JSON-safety
# ---------------------------------------------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    """Recursively convert datetimes (and Mongo doc dict-likes) to plain JSON-safe values."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# ---------------------------------------------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------------------------------------------
def setup(db, llm=None) -> dict:
    """Reset, initialise, seed v1 configs, load the world and build+load eval scenarios.

    Returns a small dict of counts so the CLI/tests have something to print/assert on.
    """
    from pregame.contracts import FIELDS
    from pregame.db import init_db, reset_db
    from pregame.versions import seed_configs
    from pregame.world import fields, store
    from pregame.world import scenarios as world_scenarios

    reset_db(db)
    init_db(db)

    sim_time = fields.SIM_START
    seed_configs(db, sim_time)

    store.load_world(db)

    built = world_scenarios.build_scenarios()
    store.load_scenarios(db, built)

    n_accounts = sum(len(v) for v in fields.ACCOUNTS.values())
    n_events = sum(len(v) for v in fields.EVENTS.values())

    return {
        "fields": len(FIELDS),
        "accounts": n_accounts,
        "facts": db.facts.count_documents({}),
        "events": n_events,
        "scenarios": db.eval_scenarios.count_documents({}),
        "configs_seeded": db.config_heads.count_documents({}),
    }


# ---------------------------------------------------------------------------------------------------------------
# briefs
# ---------------------------------------------------------------------------------------------------------------
def _build_brief(db, field: str, account_id: Optional[str], llm, config_label: str = "live", cfg=None):
    """Compile context + draft a brief for `field`/`account_id` as of the sim clock. Returns (brief, ctx)."""
    from pregame import compiler, drafter, versions
    from pregame.world import store

    as_of = store.sim_now(db)
    account = store.get_account(field, account_id)
    if cfg is None:
        cfg = versions.resolve_field_config(db, field)
    facts = store.facts_until(db, field, as_of)
    ctx = compiler.compile_context(cfg, account, facts, as_of)
    brief = _guarded_draft(ctx, llm, config_label)
    return brief, ctx


class BriefBlocked(Exception):
    """A drafted brief failed its enabled guardrails twice; it is never stored or shown as a brief."""

    def __init__(self, violations: list, ctx: dict):
        super().__init__("brief blocked by guardrails: " + "; ".join(violations[:5]))
        self.violations = violations
        self.ctx = ctx


def _guarded_draft(ctx: dict, llm, config_label: str) -> dict:
    """Draft, then run the enabled guardrails on the finished brief BEFORE anyone stores or sees it (Codex HDY-37).

    One redraft on a violation (the model may phrase it differently), then fail closed with BriefBlocked.
    The prep brief is for the advisor; advice to the client never ships.
    """
    from pregame import drafter, oracle

    brief = drafter.draft_brief(ctx, llm, config_label=config_label)
    violations = oracle.run_guardrails(brief, ctx)
    if violations and not getattr(llm, "is_fake", False):
        brief = drafter.draft_brief(ctx, llm, config_label=config_label)
        violations = oracle.run_guardrails(brief, ctx)
    if violations:
        raise BriefBlocked(violations, ctx)
    return brief


def _record_blocked(db, field: str, exc: "BriefBlocked") -> None:
    """Write the blocked attempt to the ledger (not to briefs), so the refusal is auditable."""
    from pregame import ledger

    ledger.append(db, kind="refused", actor="guardrails",
                  payload={"what": "brief", "field": field, "account_id": exc.ctx["account"]["id"],
                           "violations": exc.violations[:10], "as_of": exc.ctx["as_of"]},
                  sim_time=exc.ctx["as_of"])


def _store_brief(db, brief: dict, ctx: dict, sim_time: datetime) -> None:
    from pregame import ledger

    db.briefs.insert_one(brief)
    ledger.append(
        db,
        kind="brief",
        actor="drafter",
        payload={
            "brief_id": brief["_id"],
            "field": brief.get("field"),
            "account_id": brief.get("account_id"),
            "as_of": ctx["as_of"],
            "fact_ids": ctx["receipt"]["fact_ids"],
            "versions": ctx["receipt"]["versions"],
        },
        sim_time=sim_time,
    )


def make_brief(db, field: str, account_id: Optional[str], llm) -> dict:
    """Resolve the field's current config, compile context up to sim-now, draft and store a brief.

    Raises BriefBlocked (after recording a ledger `refused` entry) if the brief fails its guardrails twice.
    """
    try:
        brief, ctx = _build_brief(db, field, account_id, llm, config_label="live")
    except BriefBlocked as exc:
        _record_blocked(db, field, exc)
        raise
    _store_brief(db, brief, ctx, ctx["as_of"])
    return brief


# ---------------------------------------------------------------------------------------------------------------
# world and client events -> a simulated client review -> advisor feedback
# ---------------------------------------------------------------------------------------------------------------
def market_event(db, event_id: str, llm) -> dict:
    """Fire a scripted event, brief the segment's demo client, simulate the client review.

    If the review reveals a missed material change, files the event's scripted advisor feedback.
    """
    from pregame import oracle
    from pregame.world import fields, store
    from pregame.world import scenarios as world_scenarios

    store.fire_event(db, event_id)
    event = fields.event_by_id(event_id)
    field = event["field"]
    account = store.get_account(field)

    try:
        brief, ctx = _build_brief(db, field, account["id"], llm, config_label="live")
    except BriefBlocked as exc:
        _record_blocked(db, field, exc)
        return {"event": event_id, "brief_id": None, "blocked": exc.violations, "call_accuracy": None,
                "missed": [], "feedback": None}
    as_of = ctx["as_of"]
    _store_brief(db, brief, ctx, as_of)

    facts_now = store.facts_until(db, field, as_of)
    scenario = world_scenarios.live_scenario(field, account, facts_now, as_of)
    grade = oracle.grade(brief, ctx, scenario, llm)

    questions_by_id = {q["id"]: q for q in scenario.get("questions", [])}
    missed = []
    for result in grade.get("results", []):
        if result.get("kind") == "change" and not result.get("correct"):
            q = questions_by_id.get(result.get("question_id"), {})
            missed.append(
                {
                    "question_id": result.get("question_id"),
                    "text": q.get("text", ""),
                    "reason": result.get("reason", ""),
                }
            )

    feedback_text = None
    if missed:
        from pregame import ledger

        feedback_text = event.get("feedback", "")
        feedback_doc = {
            "_id": f"fb-{event_id}-{brief['_id'][:8]}",
            "field": field,
            "brief_id": brief["_id"],
            "event_id": event_id,
            "text": feedback_text,
            "sim_time": as_of,
        }
        db.feedback.insert_one(feedback_doc)
        ledger.append(
            db,
            kind="feedback",
            actor="world",
            payload={"feedback_id": feedback_doc["_id"], "event_id": event_id, "brief_id": brief["_id"]},
            sim_time=as_of,
        )

    return {
        "event": event_id,
        "brief_id": brief["_id"],
        "call_accuracy": grade.get("accuracy"),
        "missed": missed,
        "feedback": feedback_text,
    }


# ---------------------------------------------------------------------------------------------------------------
# improve
# ---------------------------------------------------------------------------------------------------------------
def improve(db, field: str, llm) -> Optional[dict]:
    """Ask the improver for one proposal, file it, and run it through the gate."""
    from pregame import gate, improver
    from pregame.world import store

    sim_time = store.sim_now(db)
    # Trusted code only (never the improver itself): score the current champion on the tuning
    # split first, cached in eval_runs, so the improver has failure reasons to read before it
    # drafts its (first) proposal.
    gate.tuning_baseline(db, field, llm)
    view = improver.ImproverView(db)        # a plain-data snapshot: the improver gets no database handle
    try:
        proposal = improver.propose(view, field, llm, sim_time)
    finally:
        improver.record_refusals(db, view, sim_time)   # trusted: its refused reads go on the ledger
    if proposal is None:
        return None

    filed = gate.file_proposal(db, proposal)
    evaluated = gate.evaluate_proposal(db, filed["_id"], llm)
    return evaluated


# ---------------------------------------------------------------------------------------------------------------
# status (everything the page needs)
# ---------------------------------------------------------------------------------------------------------------
def status(db) -> dict:
    from pregame import ledger, versions
    from pregame.contracts import FIELDS
    from pregame.world import store

    try:
        sim_time = store.sim_now(db)
    except Exception:
        sim_time = None

    ok, checked, problem = ledger.verify(db)

    fields_status: dict[str, Any] = {}
    for field in FIELDS:
        try:
            cfg = versions.resolve_field_config(db, field)
        except Exception:
            cfg = None
        history: dict[str, Any] = {}
        for kind in ("policy", "rules", "tools"):
            try:
                history[kind] = versions.history(db, kind, field)
            except Exception:
                history[kind] = []
        fields_status[field] = {"config": cfg, "history": history}

    try:
        guardrails_history = versions.history(db, "guardrails", "global")
    except Exception:
        guardrails_history = []

    try:
        events = store.list_events(db)
    except Exception:
        events = []

    try:
        proposals = list(db.proposals.find().sort([("created_sim", -1), ("created_at", -1)]))
    except Exception:
        proposals = []

    briefs_by_field: dict[str, Any] = {}
    for field in FIELDS:
        try:
            briefs_by_field[field] = list(db.briefs.find({"field": field}).sort([("as_of", -1)]).limit(2))
        except Exception:
            briefs_by_field[field] = []

    try:
        feedback = list(db.feedback.find().sort([("sim_time", -1)]).limit(20))
    except Exception:
        feedback = []

    try:
        ledger_tail = ledger.tail(db, 30)
    except Exception:
        ledger_tail = []

    llm_usage = None
    try:
        from pregame.llm import get_llm

        active = get_llm()
        usage_attr = getattr(active, "usage", None)
        llm_usage = usage_attr() if callable(usage_attr) else usage_attr
    except Exception:
        llm_usage = None

    result = {
        "sim_time": sim_time,
        "ledger_verified": ok,
        "ledger_checked": checked,
        "ledger_problem": problem,
        "fields": fields_status,
        "guardrails_history": guardrails_history,
        "events": events,
        "proposals": proposals,
        "briefs": briefs_by_field,
        "feedback": feedback,
        "ledger_tail": ledger_tail,
        "llm_usage": llm_usage,
    }
    return _json_safe(result)


# ---------------------------------------------------------------------------------------------------------------
# scripted demo
# ---------------------------------------------------------------------------------------------------------------
def _print(line: str = "") -> None:
    print(line)
    sys.stdout.flush()


def run_demo(db, llm) -> None:
    """A scripted run for the retirement segment, printed step by step, for the judges."""
    from pregame import gate, improver
    from pregame.world import fields, store

    field = "retirement"

    _print(f"== pregame demo: {field} (demo client: {store.get_account(field)['name']}) ==")

    _print("\n-- setup --")
    counts = setup(db, llm)
    _print(f"seeded: {counts}")

    _print("\n-- brief v1 --")
    brief1 = make_brief(db, field, None, llm)
    _print(f"brief {brief1['_id']}  versions={brief1['receipt']['versions']}")

    demo_events = [e["id"] for e in fields.EVENTS.get(field, [])[:2]]
    for event_id in demo_events:
        _print(f"\n-- event: {event_id} ({fields.event_by_id(event_id)['title']}) --")
        result = market_event(db, event_id, llm)
        _print(
            f"call_accuracy={result['call_accuracy']:.2f}  "
            f"missed={len(result['missed'])}  "
            f"feedback_filed={'yes' if result['feedback'] else 'no'}"
        )

    _print("\n-- improve #1 (expect an over-broad proposal rejected) --")
    p1 = improve(db, field, llm)
    if p1:
        _print(f"proposal {p1['_id']}  tier={p1.get('tier')}  status={p1['status']}")
        _print(f"  {p1.get('decision')}")
    else:
        _print("no proposal filed")

    _print("\n-- improve #2 (expect a policy change committed) --")
    p2 = improve(db, field, llm)
    if p2:
        _print(f"proposal {p2['_id']}  tier={p2.get('tier')}  status={p2['status']}")
        _print(f"  {p2.get('decision')}")
    else:
        _print("no proposal filed")

    _print("\n-- brief v2 --")
    brief2 = make_brief(db, field, None, llm)
    _print(f"brief {brief2['_id']}  versions={brief2['receipt']['versions']}")

    _print("\n-- tamper attempt (expect refused) --")
    sim_time = store.sim_now(db)
    tamper = improver.tamper_proposal(field, sim_time)
    filed = gate.file_proposal(db, tamper)
    evaluated = gate.evaluate_proposal(db, filed["_id"], llm)
    _print(f"proposal {evaluated['_id']}  tier={evaluated.get('tier')}  status={evaluated['status']}")
    _print(f"  {evaluated.get('decision')}")

    _print("\n-- improve #3 (a rule change; tier H needs a held-out win, then owner approval) --")
    p3 = improve(db, field, llm)
    if p3:
        _print(f"proposal {p3['_id']}  tier={p3.get('tier')}  status={p3['status']}")
        _print(f"  {p3.get('decision')}")
        if p3.get("status") == "awaiting_owner":
            _print(f"\nto approve: python -m pregame.cli approve {p3['_id']} {p3.get('approval_hash')}")
        elif getattr(llm, "is_fake", False) and p3.get("tier") == "H":
            _print(
                "  note: this is running in fake mode, where the fake drafter does not read the drafting "
                "rules at all, so a rules change cannot move held-out accuracy and will not win here. With a "
                "live drafter model this same proposal can win and land awaiting_owner for a human to sign."
            )
    else:
        _print("no proposal filed")

    _print("\n== demo complete ==")
