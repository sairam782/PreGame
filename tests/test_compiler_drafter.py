"""Tests for the pure compiler and the fake-mode drafter. No network, no database."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pregame.compiler import compile_context
from pregame.contracts import BRIEF_SECTIONS
from pregame.drafter import draft_brief, render_markdown


def dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


AS_OF = dt(2026, 6, 1)


def make_fact(
    _id: str,
    subject: str,
    relation: str = "status",
    *,
    kind: str = "price",
    source: str = "market_feed",
    valid_from: datetime = AS_OF,
    text: str | None = None,
    value: object = "v",
) -> dict:
    return {
        "_id": _id,
        "field": "insurance",
        "subject": subject,
        "relation": relation,
        "value": value,
        "unit": "",
        "text": text if text is not None else f"{subject} {relation} is {value}",
        "kind": kind,
        "source": source,
        "valid_from": valid_from,
        "event_id": None,
        "simulated": True,
    }


def make_account(exposures: list[str] | None = None) -> dict:
    return {
        "id": "harbor-mutual",
        "field": "insurance",
        "name": "Harbor Mutual",
        "counterpart": "Dana Ortiz, VP Risk & Insurance",
        "profile": "Buys commercial property coverage; cares about renewal pricing.",
        "exposures": exposures if exposures is not None else ["reinsurance_rates"],
    }


def make_cfg(
    *,
    recency_days: int = 180,
    max_facts: int = 6,
    include_kinds: list[str] | None = None,
    section_order: list[str] | None = None,
    likely_questions: int = 3,
    prefer_exposed: bool = False,
    tools: dict | None = None,
    rules: list[dict] | None = None,
    guardrails: list[dict] | None = None,
) -> dict:
    return {
        "field": "insurance",
        "policy": {
            "recency_days": recency_days,
            "max_facts": max_facts,
            "include_kinds": include_kinds if include_kinds is not None else ["price", "competitor", "demand"],
            "section_order": section_order if section_order is not None else list(BRIEF_SECTIONS),
            "likely_questions": likely_questions,
            "prefer_exposed": prefer_exposed,
        },
        "rules": rules if rules is not None else [{"id": "r1", "text": "Lead with the biggest number."}],
        "tools": tools if tools is not None else {"market_feed": True, "account_notes": True, "analyst_notes": False},
        "guardrails": guardrails
        if guardrails is not None
        else [{"id": "cite-facts", "text": "Every claim cites a fact id.", "check": "cite_facts", "enabled": True}],
        "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1},
    }


# ---------------------------------------------------------------------------------------------
# compiler
# ---------------------------------------------------------------------------------------------
class TestCompileContext:
    def test_supersession_and_count(self):
        facts = [
            make_fact("f1", "reinsurance_rates", "yoy_change_pct", valid_from=dt(2026, 1, 1), value="6%"),
            make_fact("f2", "reinsurance_rates", "yoy_change_pct", valid_from=dt(2026, 3, 1), value="12%"),
            make_fact("f3", "reinsurance_rates", "yoy_change_pct", valid_from=dt(2026, 4, 1), value="18%"),
        ]
        cfg = make_cfg()
        ctx = compile_context(cfg, make_account(), facts, AS_OF)

        assert [f["_id"] for f in ctx["facts"]] == ["f3"]
        assert ctx["receipt"]["excluded_superseded"] == 2

    def test_as_of_cutoff(self):
        facts = [
            make_fact("past", "port_of_long_beach", "status", valid_from=dt(2026, 1, 1)),
            make_fact("future", "port_of_long_beach", "strike_risk", valid_from=dt(2026, 12, 1)),
        ]
        cfg = make_cfg()
        ctx = compile_context(cfg, make_account(), facts, AS_OF)

        ids = [f["_id"] for f in ctx["facts"]]
        assert "future" not in ids
        assert "past" in ids
        # the future fact never entered the supersession count either
        assert ctx["receipt"]["excluded_superseded"] == 0

    def test_tool_filter(self):
        facts = [
            make_fact("analyst1", "state_rate_case", source="analyst_notes", kind="regulation"),
        ]
        cfg = make_cfg(include_kinds=["regulation"], tools={"market_feed": True, "account_notes": True, "analyst_notes": False})
        ctx = compile_context(cfg, make_account(), facts, AS_OF)
        assert ctx["facts"] == []

        cfg_on = make_cfg(include_kinds=["regulation"], tools={"market_feed": True, "account_notes": True, "analyst_notes": True})
        ctx_on = compile_context(cfg_on, make_account(), facts, AS_OF)
        assert [f["_id"] for f in ctx_on["facts"]] == ["analyst1"]

    def test_kind_filter_and_account_exception(self):
        facts = [
            make_fact("reg1", "state_rate_case", kind="regulation", source="market_feed"),
            make_fact("acct1", "harbor-mutual", kind="account", source="account_notes"),
        ]
        # include_kinds deliberately omits both "regulation" and "account"
        cfg = make_cfg(include_kinds=["price"], tools={"market_feed": True, "account_notes": True, "analyst_notes": False})
        ctx = compile_context(cfg, make_account(), facts, AS_OF)

        ids = [f["_id"] for f in ctx["facts"]]
        assert "reg1" not in ids  # not in include_kinds -> dropped
        assert "acct1" in ids  # kind "account" kept because account_notes tool is on

    def test_kind_account_dropped_when_tool_off(self):
        facts = [make_fact("acct1", "harbor-mutual", kind="account", source="account_notes")]
        cfg = make_cfg(tools={"market_feed": True, "account_notes": False, "analyst_notes": False})
        ctx = compile_context(cfg, make_account(), facts, AS_OF)
        assert ctx["facts"] == []

    def test_recency_with_account_exemption(self):
        old = AS_OF - timedelta(days=400)
        facts = [
            make_fact("stale_price", "diesel", kind="price", source="market_feed", valid_from=old),
            make_fact("old_account_fact", "harbor-mutual", kind="account", source="account_notes", valid_from=old),
        ]
        cfg = make_cfg(recency_days=180)
        ctx = compile_context(cfg, make_account(), facts, AS_OF)

        ids = [f["_id"] for f in ctx["facts"]]
        assert "stale_price" not in ids  # too old, not exempt
        assert "old_account_fact" in ids  # account facts are exempt from recency

    def test_exposure_ranking(self):
        facts = [
            make_fact("old_exposed", "reinsurance_rates", "a", valid_from=dt(2026, 1, 1), kind="price"),
            make_fact("new_unexposed", "diesel", "b", valid_from=dt(2026, 5, 1), kind="price"),
            make_fact("new_exposed", "reinsurance_rates", "c", valid_from=dt(2026, 4, 1), kind="price"),
        ]
        account = make_account(exposures=["reinsurance_rates"])

        cfg = make_cfg(prefer_exposed=True, max_facts=10)
        ctx = compile_context(cfg, account, facts, AS_OF)
        assert [f["_id"] for f in ctx["facts"]] == ["new_exposed", "old_exposed", "new_unexposed"]

        cfg_no_pref = make_cfg(prefer_exposed=False, max_facts=10)
        ctx_no_pref = compile_context(cfg_no_pref, account, facts, AS_OF)
        assert [f["_id"] for f in ctx_no_pref["facts"]] == ["new_unexposed", "new_exposed", "old_exposed"]

    def test_cap_max_facts(self):
        facts = [
            make_fact(f"f{i}", f"subject{i}", valid_from=dt(2026, 1, 1) + timedelta(days=i), kind="price")
            for i in range(10)
        ]
        cfg = make_cfg(max_facts=3)
        ctx = compile_context(cfg, make_account(), facts, AS_OF)

        assert len(ctx["facts"]) == 3
        # newest-first, so the three highest-index facts win
        assert [f["_id"] for f in ctx["facts"]] == ["f9", "f8", "f7"]
        assert ctx["receipt"]["fact_ids"] == ["f9", "f8", "f7"]

    def test_receipt(self):
        facts = [make_fact("f1", "diesel", kind="price", valid_from=dt(2026, 5, 1))]
        cfg = make_cfg()
        account = make_account()
        ctx = compile_context(cfg, account, facts, AS_OF)
        receipt = ctx["receipt"]

        assert receipt["field"] == "insurance"
        assert receipt["account_id"] == account["id"]
        assert receipt["as_of"] == AS_OF
        assert receipt["versions"] == cfg["versions"]
        assert receipt["fact_ids"] == ["f1"]
        assert receipt["excluded_superseded"] == 0
        assert isinstance(receipt["context_tokens"], int)
        assert receipt["context_tokens"] > 0
        assert isinstance(receipt["config_hash"], str)
        assert len(receipt["config_hash"]) == 64  # sha256 hex digest (fallback path)

    def test_config_hash_deterministic_and_sensitive_to_body(self):
        facts = [make_fact("f1", "diesel", kind="price")]
        account = make_account()
        cfg_a = make_cfg()
        cfg_b = make_cfg()
        ctx_a = compile_context(cfg_a, account, facts, AS_OF)
        ctx_b = compile_context(cfg_b, account, facts, AS_OF)
        assert ctx_a["receipt"]["config_hash"] == ctx_b["receipt"]["config_hash"]

        cfg_c = make_cfg(recency_days=30)
        ctx_c = compile_context(cfg_c, account, facts, AS_OF)
        assert ctx_c["receipt"]["config_hash"] != ctx_a["receipt"]["config_hash"]


# ---------------------------------------------------------------------------------------------
# fake drafter
# ---------------------------------------------------------------------------------------------
class _FakeLLM:
    is_fake = True


def _build_ctx(section_order=None, likely_questions=2, prefer_exposed=True, n_facts=5):
    facts = [
        make_fact(
            f"f{i}",
            "reinsurance_rates" if i % 2 == 0 else "diesel",
            kind="price" if i % 2 else "demand",
            valid_from=dt(2026, 1, 1) + timedelta(days=i),
        )
        for i in range(n_facts)
    ]
    cfg = make_cfg(
        include_kinds=["price", "demand"],
        section_order=section_order,
        likely_questions=likely_questions,
        prefer_exposed=prefer_exposed,
        max_facts=10,
    )
    account = make_account(exposures=["reinsurance_rates"])
    return compile_context(cfg, account, facts, AS_OF)


class TestFakeDrafter:
    def test_claims_cite_ids_that_exist_in_context(self):
        ctx = _build_ctx()
        brief = draft_brief(ctx, _FakeLLM(), config_label="live")

        valid_ids = {f["_id"] for f in ctx["facts"]}
        for section_claims in brief["sections"].values():
            for claim in section_claims:
                assert claim["fact_ids"], "every claim must cite at least one fact id"
                for fid in claim["fact_ids"]:
                    assert fid in valid_ids

    def test_section_order_matches_policy(self):
        custom_order = ["watch_outs", "what_changed", "talking_points", "why_it_matters", "likely_questions"]
        ctx = _build_ctx(section_order=custom_order)
        brief = draft_brief(ctx, _FakeLLM())
        assert list(brief["sections"].keys()) == custom_order

    def test_likely_questions_count(self):
        ctx = _build_ctx(likely_questions=3, n_facts=5)
        brief = draft_brief(ctx, _FakeLLM())
        assert len(brief["sections"]["likely_questions"]) == min(3, len(ctx["facts"]))

        ctx_short = _build_ctx(likely_questions=10, n_facts=2)
        brief_short = draft_brief(ctx_short, _FakeLLM())
        assert len(brief_short["sections"]["likely_questions"]) == len(ctx_short["facts"])

    def test_markdown_contains_every_claim_text(self):
        ctx = _build_ctx()
        brief = draft_brief(ctx, _FakeLLM())
        markdown = brief["markdown"]
        for section_claims in brief["sections"].values():
            for claim in section_claims:
                assert claim["text"] in markdown

    def test_brief_metadata(self):
        ctx = _build_ctx()
        brief = draft_brief(ctx, _FakeLLM(), config_label="candidate:prop-1")
        assert brief["model"] == "fake"
        assert brief["config_label"] == "candidate:prop-1"
        assert brief["field"] == ctx["field"]
        assert brief["account_id"] == ctx["account"]["id"]
        assert brief["receipt"] is ctx["receipt"]

    def test_render_markdown_standalone(self):
        ctx = _build_ctx()
        brief = draft_brief(ctx, _FakeLLM())
        # render_markdown is separately callable and reproduces the same content
        again = render_markdown(brief["sections"], ctx)
        assert again == brief["markdown"]
