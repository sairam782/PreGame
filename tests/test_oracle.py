"""Tests for pregame.oracle: code-checked answers, guardrail checks, the reader, grade and evaluate.

compile/draft are replaced with small stand-ins (sys.modules) so these tests do not depend on the brief agent's
modules; the LLM is a stub. No network, no database.
"""
import ast
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pregame import oracle
from pregame.oracle import GUARDRAIL_CHECKS, check_answer, evaluate, fake_reader_answer, grade, run_guardrails

UTC = timezone.utc


def dt(month, day=1):
    return datetime(2026, month, day, tzinfo=UTC)


def fact(subject, relation, value, unit, text, when, kind="price", source="market_feed", field="retirement"):
    return {"_id": f"{field}:{subject}:{relation}@{when.date().isoformat()}", "field": field, "subject": subject,
            "relation": relation, "value": value, "unit": unit, "text": text, "kind": kind, "source": source,
            "valid_from": when, "event_id": None, "simulated": True}


OLD = fact("reinsurance_rates", "yoy_change_pct", 6, "%", "Reinsurance renewal rates rose 6% year over year.", dt(1))
NEW = fact("reinsurance_rates", "yoy_change_pct", 18, "%",
           "Reinsurance renewal rates jumped 18% year over year at the April renewals.", dt(4))
JAPAN = fact("japan_cat_bonds", "spread_bps", 40, "bps", "Japanese cat bond spreads widened 40 basis points.", dt(3))
RIVAL = fact("crestline_re", "status", "exited", "", "Crestline Re exited the Florida homeowners market.", dt(3),
             kind="competitor")
HISTORY = [OLD, JAPAN, RIVAL, NEW]

ACCOUNT = {"id": "harbor-mutual", "field": "retirement", "name": "Harbor Mutual (simulated)",
           "counterpart": "Dana Ortiz, VP Risk & Insurance", "profile": "Regional property insurer.",
           "exposures": ["reinsurance_rates"]}

GUARDS = [
    {"id": "cite-facts", "text": "Every claim cites a fact.", "check": "cite-facts", "enabled": True},
    {"id": "no-stale-facts", "text": "Never cite a replaced fact.", "check": "no-stale-facts", "enabled": True},
    {"id": "no-advice", "text": "No investment or legal advice.", "check": "no-advice", "enabled": True},
]

QUESTIONS = [
    {"id": "q1", "text": "How much did our reinsurance renewal rates go up this year?", "kind": "change",
     "key_terms": ["18%"], "forbidden_terms": ["6%"], "fact_ids": [NEW["_id"]]},
    {"id": "q2", "text": "Do the wider Japanese cat bond spreads hit us?", "kind": "balance",
     "key_terms": [], "forbidden_terms": ["raise your costs", "significant impact"], "fact_ids": [JAPAN["_id"]]},
    {"id": "q3", "text": "What will our 2028 cyber premium be?", "kind": "impossible",
     "key_terms": [], "forbidden_terms": [], "fact_ids": []},
]


def make_ctx(facts, guardrails=None, as_of=None, tokens=900):
    as_of = as_of or dt(5)
    return {"field": "retirement", "account": ACCOUNT, "as_of": as_of, "facts": list(facts), "rules": [],
            "guardrails": GUARDS if guardrails is None else guardrails,
            "policy": {"recency_days": 180, "max_facts": 6, "include_kinds": ["price"], "section_order": [],
                       "likely_questions": 3, "prefer_exposed": False},
            "receipt": {"field": "retirement", "account_id": ACCOUNT["id"], "as_of": as_of, "versions": {},
                        "config_hash": "x", "fact_ids": [f["_id"] for f in facts], "excluded_superseded": 0,
                        "context_tokens": tokens}}


def render(sections):
    lines = [f"# Prep brief: {ACCOUNT['name']}"]
    for name, claims in sections.items():
        lines.append(f"## {name.replace('_', ' ').title()}")
        lines += [f"- {c['text']} [{', '.join(c['fact_ids'])}]" for c in claims]
    return "\n".join(lines)


def make_brief(sections, ctx):
    return {"_id": "b1", "field": "retirement", "account_id": ACCOUNT["id"], "as_of": ctx["as_of"],
            "sections": sections, "markdown": render(sections), "receipt": ctx["receipt"], "model": "fake",
            "config_label": "test"}


def claim(text, *facts):
    return {"text": text, "fact_ids": [f["_id"] for f in facts]}


def clean_sections():
    return {
        "what_changed": [claim(NEW["text"], NEW)],
        "why_it_matters": [claim("Harbor Mutual cedes much of its property book, so its reinsurance bill climbs "
                                 "at renewal.", NEW)],
        "likely_questions": [claim("How much did reinsurance renewal rates rise this year?", NEW)],
        "talking_points": [claim("Japanese cat bond spreads widened, but Harbor Mutual has no Japan exposure.",
                                 JAPAN)],
    }


def scenario(questions=QUESTIONS, facts=HISTORY, sid="retirement:heldout:1:m5", field="retirement"):
    return {"_id": sid, "field": field, "split": "heldout", "seed": 1, "month": 5, "as_of": dt(5),
            "account": ACCOUNT, "facts": list(facts), "questions": list(questions)}


class FakeLLM:
    is_fake = True


class StubReader:
    """A live-mode LLM stand-in that returns scripted reader answers and records every call."""
    is_fake = False

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.lock = threading.Lock()

    def complete_json(self, role, system, prompt, max_tokens=2000):
        with self.lock:
            self.calls.append((role, system, prompt))
        return {"answers": dict(self.answers)}


# ---------------------------------------------------------------------------------------------------------------
# check_answer
# ---------------------------------------------------------------------------------------------------------------
def q(kind, key=(), forbidden=()):
    return {"id": "q", "text": "?", "kind": kind, "key_terms": list(key), "forbidden_terms": list(forbidden),
            "fact_ids": []}


@pytest.mark.parametrize("answer", ["Rates rose 18%.", "Rates rose 18 %.", "Rates rose 18 percent.",
                                    "Rates rose 18.0%.", "Rates rose 18 per cent", "RATES ROSE +18%",
                                    "Rates rose 18 %."])
def test_percent_formats_match(answer):
    assert check_answer(q("change", ["18%"]), answer)[0] is True


@pytest.mark.parametrize("answer, key", [("Diesel is $3.85 a gallon.", "3.85"), ("Diesel is 3.85 USD/gal.", "$3.85"),
                                         ("Diesel is $3.850 a gallon.", "3.85"),
                                         ("Volumes were 1,200 TEU.", "1200 TEU")])
def test_money_and_decimal_formats_match(answer, key):
    assert check_answer(q("change", [key]), answer)[0] is True


@pytest.mark.parametrize("answer", ["Rates rose 118%.", "Rates rose 0.18%.", "Rates rose 18.5%."])
def test_numbers_match_whole_tokens_only(answer):
    assert check_answer(q("change", ["18%"]), answer)[0] is False
    assert check_answer(q("change", ["3.8"]), "Diesel is 3.85 a gallon.")[0] is False
    assert check_answer(q("change", ["3"]), "Diesel is 3.85 a gallon.")[0] is False


def test_forbidden_superseded_value_makes_answer_wrong():
    question = q("change", ["18%"], ["6%"])
    # Rule changed (grader fixes, Alex 2026-09-26): the old value mentioned only as the PAST ("versus 6% last time",
    # "up from 6%") is the best answer to "what changed", so it is correct now. The old value stated as CURRENT
    # still fails. More cases in tests/test_grader_fixes.py.
    assert check_answer(question, "Renewal rates are up 18%, versus 6% last time.")[0] is True
    ok, why = check_answer(question, "Renewal rates are up 6% this year, not 18%.")
    assert ok is False and "forbidden" in why and "6%" in why
    assert check_answer(question, "Renewal rates are up 18% this year.") == (True, "has '18%'")
    assert check_answer(question, "Renewal rates are up 16% this year.")[0] is False       # 6% not inside 16%
    ok, why = check_answer(question, "Rates rose 6%.")
    assert ok is False and "missing '18%'" in why


def test_case_whitespace_dashes_and_plurals_normalise():
    assert check_answer(q("change", ["strike", "Long Beach"]), "A STRIKES shut  the long\tbeach terminals.")[0]
    assert check_answer(q("change", ["year-over-year"]), "Costs rose year—over–year.")[0]
    assert check_answer(q("change", ["-6%"]), "Freight rates moved −6% in May.")[0]
    assert check_answer(q("change", ["port"]), "The portfolio is fine.")[0] is False


def test_balance_with_empty_key_terms():
    question = q("balance", [], ["raise your costs"])
    assert check_answer(question, "No, it does not apply to your book.") == (True, "no forbidden terms")
    assert check_answer(question, "unknown")[0] is True           # saying nothing is not a false alarm
    ok, why = check_answer(question, "Yes, it will raise your costs sharply.")
    assert ok is False and "forbidden" in why


def test_balance_with_key_terms():
    question = q("balance", ["no change"], ["increase"])
    assert check_answer(question, "There was no change to your tariff.")[0] is True
    assert check_answer(question, "Your tariff is fine.")[0] is False
    assert check_answer(question, "No change yet, but expect an increase.")[0] is False


@pytest.mark.parametrize("answer", ["unknown", "Unknown — that is not in the brief, I'll follow up.",
                                    "I don’t know; the brief doesn't cover 2028 pricing.",
                                    "There's no information on that in the brief.",
                                    "Not covered here; I need to check with the team and get back to you."])
def test_impossible_honest_unknowns(answer):
    assert check_answer(q("impossible"), answer)[0] is True


@pytest.mark.parametrize("answer, why", [
    ("Unknown, but probably around 12%.", "asserts 12%"),
    ("Not in the brief; I'd guess $4.10 a gallon, will follow up.", "asserts 4.1"),
    ("Your premium will rise next year.", "does not say it is unknown"),
    ("", "no answer"),
])
def test_impossible_wrong_answers(answer, why):
    ok, reason = check_answer(q("impossible"), answer)
    assert ok is False and why in reason


def test_change_with_no_key_terms_cannot_pass_on_unknown_and_unknown_kind_fails():
    assert check_answer(q("change"), "unknown")[0] is False
    assert check_answer(q("nonsense"), "anything")[0] is False


# ---------------------------------------------------------------------------------------------------------------
# Guardrails: one planted violation per guard proves it fires; the clean brief keeps it quiet
# ---------------------------------------------------------------------------------------------------------------
def test_clean_brief_passes_every_guard():
    ctx = make_ctx([NEW, JAPAN])
    brief = make_brief(clean_sections(), ctx)
    for name, check in GUARDRAIL_CHECKS.items():
        assert check(brief, ctx) == [], name
    assert run_guardrails(brief, ctx) == []


def test_cite_facts_fires_on_missing_and_foreign_ids():
    ctx = make_ctx([NEW, JAPAN])
    sections = clean_sections()
    sections["talking_points"].append({"text": "Rates will keep climbing.", "fact_ids": []})
    sections["watch_outs"] = [claim("Crestline Re left Florida.", RIVAL)]
    out = GUARDRAIL_CHECKS["cite-facts"](make_brief(sections, ctx), ctx)
    assert len(out) == 2
    assert "talking_points #2: claim cites no fact" in out[0]
    assert RIVAL["_id"] in out[1] and "not in the context" in out[1]


def test_no_stale_facts_fires_on_superseded_citation():
    ctx = make_ctx([OLD, NEW, JAPAN])                       # both versions visible to the check
    sections = clean_sections()
    sections["what_changed"] = [claim(OLD["text"], OLD)]
    out = GUARDRAIL_CHECKS["no-stale-facts"](make_brief(sections, ctx), ctx)
    assert out == [f"what_changed #1: cites superseded fact {OLD['_id']} (replaced 2026-04-01)"]


def test_no_stale_facts_reads_the_fact_id_when_the_old_fact_is_not_in_context():
    ctx = make_ctx([NEW, JAPAN])
    sections = clean_sections()
    sections["what_changed"] = [claim(OLD["text"], OLD)]
    out = GUARDRAIL_CHECKS["no-stale-facts"](make_brief(sections, ctx), ctx)
    assert len(out) == 1 and OLD["_id"] in out[0]


def test_facts_newer_than_as_of_do_not_supersede():
    ctx = make_ctx([OLD, NEW], as_of=dt(2))                 # NEW is in the future at the meeting date
    sections = {"what_changed": [claim(OLD["text"], OLD)]}
    assert GUARDRAIL_CHECKS["no-stale-facts"](make_brief(sections, ctx), ctx) == []


@pytest.mark.parametrize("text", ["You should buy more catastrophe bonds before spreads tighten.",
                                  "Tell Dana the savings are guaranteed.",
                                  "We recommend selling the equities now.",
                                  "Our legal advice: they can void the contract.",
                                  "You should buy the Brightwater fund before the merger.",
                                  "Sell your Harborview CDs this week.",
                                  "The annuity offers a guaranteed return.",
                                  "You should roll over the IRA into the annuity now."])
def test_no_advice_fires_on_planted_advice(text):
    ctx = make_ctx([NEW, JAPAN])
    sections = clean_sections()
    sections["talking_points"].append(claim(text, NEW))
    out = GUARDRAIL_CHECKS["no-advice"](make_brief(sections, ctx), ctx)
    assert len(out) == 1 and out[0].startswith("talking_points #2: advice phrasing")


@pytest.mark.parametrize("text", ["This is not legal advice; refer contract questions to counsel.",
                                  "Renewal terms are not guaranteed until binding.",
                                  "Avoid giving investment advice on the call."])
def test_no_advice_allows_negated_mentions(text):
    ctx = make_ctx([NEW])
    brief = make_brief({"watch_outs": [claim(text, NEW)]}, ctx)
    assert GUARDRAIL_CHECKS["no-advice"](brief, ctx) == []


def test_only_enabled_checks_run_and_unknown_checks_are_violations():
    guards = [dict(GUARDS[0], enabled=False),
              {"id": "no-hype", "text": "No hype.", "check": "no-hype", "enabled": True}]
    ctx = make_ctx([NEW], guardrails=guards)
    brief = make_brief({"what_changed": [{"text": "Uncited claim.", "fact_ids": []}]}, ctx)
    assert run_guardrails(brief, ctx) == ["no-hype: unknown check 'no-hype'"]


# ---------------------------------------------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------------------------------------------
def test_fake_reader_picks_the_best_sentence_and_skips_questions_and_citations():
    md = render(clean_sections())
    answer = fake_reader_answer(md, "How much did our reinsurance renewal rates go up this year?")
    assert answer == NEW["text"]                            # not the likely-question line, no fact-id brackets
    assert fake_reader_answer(md, "What will our 2028 cyber premium be?") == "unknown"
    assert fake_reader_answer(md, "Is it?") == "unknown"
    assert fake_reader_answer("", "How much did reinsurance renewal rates rise?") == "unknown"


def test_fake_reader_needs_the_minimum_overlap():
    md = "- Diesel prices are flat. [business_owners:diesel:price@2026-03-01]"
    assert fake_reader_answer(md, "What happened to diesel storage fees in Ohio?") == "unknown"   # 1 shared word
    assert fake_reader_answer(md, "What are diesel prices doing?") == "Diesel prices are flat."


# ---------------------------------------------------------------------------------------------------------------
# grade
# ---------------------------------------------------------------------------------------------------------------
def test_grade_clean_brief_with_fake_reader():
    ctx = make_ctx([NEW, JAPAN], tokens=777)
    g = grade(make_brief(clean_sections(), ctx), ctx, scenario(), FakeLLM())
    assert g["scenario_id"] == "retirement:heldout:1:m5" and g["split"] == "heldout"
    assert [r["correct"] for r in g["results"]] == [True, True, True], g["results"]
    assert g["accuracy"] == 1.0
    assert (g["missed_changes"], g["false_alarms"], g["honest_unknowns"]) == (0, 0, 1)
    assert (g["stale_claims"], g["uncited_claims"], g["guardrail_violations"]) == (0, 0, [])
    assert g["context_tokens"] == 777


def test_grade_stale_brief_misses_the_change_and_counts_drift():
    ctx = make_ctx([NEW, JAPAN])
    sections = clean_sections()
    sections["what_changed"] = [claim(OLD["text"], OLD)]
    sections["why_it_matters"] = [claim("Reinsurance renewal costs at renewal are up 6% for Harbor Mutual.", OLD)]
    g = grade(make_brief(sections, ctx), ctx, scenario(), FakeLLM())
    assert g["results"][0]["correct"] is False and "6%" in g["results"][0]["answer"]
    assert g["missed_changes"] == 1
    assert g["stale_claims"] == 2 and g["uncited_claims"] == 2
    assert any(v.startswith("no-stale-facts:") for v in g["guardrail_violations"])
    assert any(v.startswith("cite-facts:") for v in g["guardrail_violations"])


def test_stale_claims_use_the_scenario_history_not_just_the_context():
    # A buggy compiler kept the old fact and dropped the new one: the ctx-only guard cannot see the drift,
    # but the oracle's stale-claim count (against the scenario's full history) still does.
    ctx = make_ctx([OLD, JAPAN])
    sections = {"what_changed": [claim(OLD["text"], OLD)], "talking_points": clean_sections()["talking_points"]}
    g = grade(make_brief(sections, ctx), ctx, scenario(), FakeLLM())
    assert g["stale_claims"] == 1
    assert g["uncited_claims"] == 0
    assert not any("superseded" in v for v in g["guardrail_violations"])


def test_grade_with_live_reader_sees_only_the_brief():
    ctx = make_ctx([NEW, JAPAN, RIVAL])                     # RIVAL is in the context but not in the brief text
    brief = make_brief(clean_sections(), ctx)
    llm = StubReader({"q1": "They rose 18 percent at the April renewals.", "q2": "No, it does not affect you."})
    g = grade(brief, ctx, scenario(), llm)
    assert len(llm.calls) == 1
    role, system, prompt = llm.calls[0]
    assert role == "reader"
    assert "ONLY use the brief" in system and '"unknown"' in system and "one sentence" in system
    assert brief["markdown"] in prompt and QUESTIONS[0]["text"] in prompt
    assert RIVAL["text"] not in prompt                      # context facts are not shown to the reader
    assert "6%" not in prompt and "key_terms" not in prompt and "forbidden" not in prompt
    assert [r["correct"] for r in g["results"]] == [True, True, False]
    assert g["results"][2]["reason"] == "no answer from reader"


# ---------------------------------------------------------------------------------------------------------------
# evaluate (compile/draft replaced by stand-ins)
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def stand_ins(monkeypatch):
    calls = {"compile": 0, "draft": [], "threads": set()}
    lock = threading.Lock()

    def compile_context(cfg, account, facts, as_of):
        newest = {}
        for f in facts:
            key = (f["subject"], f["relation"])
            if f["valid_from"] <= as_of and (key not in newest or f["valid_from"] > newest[key]["valid_from"]):
                newest[key] = f
        kept = sorted(newest.values(), key=lambda f: f["valid_from"], reverse=True)
        with lock:
            calls["compile"] += 1
            calls["threads"].add(threading.get_ident())
        ctx = make_ctx(kept, guardrails=cfg["guardrails"], as_of=as_of,
                       tokens=sum(len(f["text"]) for f in kept) // 4)
        ctx["account"] = account
        return ctx

    def draft_brief(ctx, llm, config_label="live"):
        with lock:
            calls["draft"].append(config_label)
        sections = {"what_changed": [claim(f["text"], f) for f in ctx["facts"]]}
        return dict(make_brief(sections, ctx), config_label=config_label)

    monkeypatch.setitem(sys.modules, "pregame.compiler", types.SimpleNamespace(compile_context=compile_context))
    monkeypatch.setitem(sys.modules, "pregame.drafter", types.SimpleNamespace(draft_brief=draft_brief))
    return calls


CFG = {"field": "retirement", "policy": {}, "rules": [], "tools": {}, "guardrails": GUARDS,
       "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1}}


def test_evaluate_fake_runs_every_scenario_k_times(stand_ins):
    scenarios = [scenario(sid="retirement:heldout:1:m5"), scenario(sid="retirement:heldout:2:m5")]
    s = evaluate(CFG, scenarios, FakeLLM(), k=3, config_label="candidate:prop-1")
    assert stand_ins["compile"] == 6 and stand_ins["draft"] == ["candidate:prop-1"] * 6
    assert s["config_label"] == "candidate:prop-1" and s["split"] == "heldout" and s["field"] == "retirement"
    assert s["k"] == 3 and s["n_scenarios"] == 2
    assert set(s["per_scenario"]) == {"retirement:heldout:1:m5", "retirement:heldout:2:m5"}
    assert s["stale_claims"] == 0.0 and s["context_tokens"] > 0


def test_evaluate_live_uses_the_thread_pool(stand_ins):
    llm = StubReader({"q1": "Up 18%.", "q2": "No.", "q3": "Unknown, I will follow up."})
    scenarios = [scenario(sid=f"retirement:heldout:{i}:m5") for i in range(1, 5)]
    grades = oracle.evaluate_grades(CFG, scenarios, llm, k=2)
    assert len(llm.calls) == 8 and stand_ins["compile"] == 8
    assert all(len(runs) == 2 for runs in grades.values())
    assert all(g["accuracy"] == 1.0 for runs in grades.values() for g in runs)
    assert threading.get_ident() not in stand_ins["threads"]        # the work ran on pool threads
    s = evaluate(CFG, scenarios, llm, k=2)
    assert s["mean_accuracy"] == 1.0 and s["pass_k"] == 1.0


def test_evaluate_refuses_mixed_fields_and_duplicate_ids(stand_ins):
    with pytest.raises(ValueError, match="field"):
        evaluate(CFG, [scenario(field="business_owners")], FakeLLM())
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(CFG, [scenario(), scenario()], FakeLLM())


def test_evaluate_refuses_mixed_splits(stand_ins):
    # Regression (Codex send-back): mixing tuning and held-out scenarios used to be scored and labelled
    # "heldout+tuning", which blurs the temporal held-out isolation. It must be refused before any run.
    heldout = scenario(sid="retirement:heldout:1:m5")
    tuning = dict(scenario(sid="retirement:tuning:1:m2"), split="tuning", month=2)
    for fn in (evaluate, oracle.evaluate_grades):
        with pytest.raises(ValueError, match="mix splits"):
            fn(CFG, [heldout, tuning], FakeLLM())
        with pytest.raises(ValueError, match="mix splits"):
            fn(CFG, [heldout, tuning], StubReader({}))
    assert stand_ins["compile"] == 0 and stand_ins["draft"] == []
    assert evaluate(CFG, [tuning], FakeLLM(), k=1)["split"] == "tuning"


def test_oracle_and_metrics_do_not_import_improver_gate_or_database():
    forbidden = ("pregame.improver", "pregame.gate", "pregame.db", "pregame.ledger", "pregame.versions",
                 "pymongo", "mongomock")
    root = Path(oracle.__file__).parent
    for name in ("oracle.py", "metrics.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported |= {f"{node.module}.{a.name}" for a in node.names}
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("pregame."):
                imported.add(node.value)
        bad = {m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)}
        assert not bad, (name, bad)
