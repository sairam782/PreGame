"""One client's account notes must never reach another client's brief (found by the world builder, 26 Sep)."""
from pregame.compiler import compile_context
from pregame.defaults import DEFAULT_GUARDRAILS, DEFAULT_POLICY, DEFAULT_RULES, DEFAULT_TOOLS
from pregame.world import fields


def _cfg(field):
    return {"field": field, "policy": dict(DEFAULT_POLICY, max_facts=30), "rules": DEFAULT_RULES[field],
            "tools": dict(DEFAULT_TOOLS), "guardrails": DEFAULT_GUARDRAILS,
            "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1}}


def test_account_notes_stay_with_their_client():
    for field in fields.FIELDS if hasattr(fields, "FIELDS") else ("insurance", "logistics", "energy"):
        demo, other = fields.ACCOUNTS[field][0], fields.ACCOUNTS[field][1]
        facts = fields.all_facts(field) if hasattr(fields, "all_facts") else fields.BASE_FACTS[field]
        as_of = fields.sim_date(6, 28)
        ctx = compile_context(_cfg(field), demo, facts, as_of)
        account_facts = [f for f in ctx["facts"] if f["kind"] == "account"]
        assert all(f["subject"] in demo["exposures"] for f in account_facts), field
        others = [f for f in facts if f["kind"] == "account" and f["subject"] in other["exposures"]
                  and f["subject"] not in demo["exposures"]]
        assert others, f"{field}: the fixture should hold another client's notes to prove the filter"
        assert not {f["_id"] for f in others} & set(ctx["receipt"]["fact_ids"]), field
