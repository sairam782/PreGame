"""One client's account notes must never reach another client's brief.

Found by the world builder (26 Sep); Codex's check then showed a subject-based filter is not ownership: two clients
can share an exposure subject, and filtering after supersession lets another client's newer note replace this one's.
Notes now carry an explicit owner (`account_id`), filtered before supersession; notes with no owner are dropped.
"""
from datetime import datetime, timedelta, timezone

from pregame.compiler import compile_context
from pregame.defaults import DEFAULT_GUARDRAILS, DEFAULT_POLICY, DEFAULT_RULES, DEFAULT_TOOLS

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _cfg():
    return {"field": "insurance", "policy": dict(DEFAULT_POLICY, max_facts=30), "rules": DEFAULT_RULES["insurance"],
            "tools": dict(DEFAULT_TOOLS), "guardrails": DEFAULT_GUARDRAILS,
            "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1}}


def _account(aid):
    # both clients share the exposure "ohio-property": the case a subject-based filter gets wrong
    return {"id": aid, "field": "insurance", "name": aid, "counterpart": "x", "profile": "p",
            "exposures": ["ohio-property", "reinsurance_rates"]}


def _note(aid, value, days, owner=True):
    return {"_id": f"insurance:ohio-property:renewal_note@{(T0 + timedelta(days=days)).date()}:{aid}",
            "field": "insurance", "subject": "ohio-property", "relation": "renewal_note", "value": value, "unit": "",
            "text": f"{aid} note: {value}", "kind": "account", "source": "account_notes",
            "valid_from": T0 + timedelta(days=days), "event_id": None, "simulated": True,
            "account_id": aid if owner else None}


def test_notes_on_a_shared_subject_stay_with_their_owner():
    mine, theirs = _note("alpha", "renewal moved to June", 1), _note("beta", "switching brokers", 5)  # theirs is newer
    facts = [mine, theirs]
    ctx_alpha = compile_context(_cfg(), _account("alpha"), facts, T0 + timedelta(days=10))
    ctx_beta = compile_context(_cfg(), _account("beta"), facts, T0 + timedelta(days=10))
    assert [f["_id"] for f in ctx_alpha["facts"] if f["kind"] == "account"] == [mine["_id"]]   # not replaced by beta's
    assert [f["_id"] for f in ctx_beta["facts"] if f["kind"] == "account"] == [theirs["_id"]]


def test_notes_without_an_owner_are_dropped():
    ctx = compile_context(_cfg(), _account("alpha"), [_note("alpha", "x", 1, owner=False)], T0 + timedelta(days=10))
    assert not [f for f in ctx["facts"] if f["kind"] == "account"]


def test_scalar_fact_ids_from_the_model_are_dropped_not_crashed():
    """Codex HDY-30 (1cbab1d): a model returning fact_ids as a bare number must not raise TypeError."""
    from pregame import drafter
    ctx = compile_context(_cfg(), _account("alpha"), [_note("alpha", "x", 1)], T0 + timedelta(days=10))
    raw = {name: [{"text": "claim", "fact_ids": 123}] for name in ctx["policy"]["section_order"]}
    sections, dropped = drafter._salvage_sections({"sections": raw}, ctx)
    assert dropped == len(raw) and all(v == [] for v in sections.values())
