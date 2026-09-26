"""Fix 7 (compliance drift): the model never writes a disclosure; code inserts the approved text word for word, and
the approved-language guardrail blocks a look-alike or a protective promise.

The source data's assistant kept rewording its disclosures until one read "This portfolio is built to protect your
capital." Here that sentence is redrafted once, then blocked, and never stored.
"""
import re
from datetime import datetime, timezone

import pytest

from pregame import defaults, drafter, gate, improver, loop, oracle
from pregame.config import Settings
from pregame.contracts import APPROVED_LANGUAGE, BRIEF_SECTIONS
from pregame.llm import get_llm

PROMISE = "This portfolio is built to protect your capital."
CLEAN = "Review the latest change with the household."


def _brief(*texts):
    return {"sections": {"talking_points": [{"text": t, "fact_ids": ["f1"]} for t in texts]}}


def _check(*texts):
    return oracle.check_approved_language(_brief(*texts), {})


def _ctx():
    return {"field": "retirement", "account": {"id": "okafor-household", "name": "The Okafor household"},
            "as_of": datetime(2026, 4, 1, tzinfo=timezone.utc), "policy": {"section_order": list(BRIEF_SECTIONS)}}


def _fake_llm():
    return get_llm(Settings(mongodb_uri="mongodb://localhost:27017", anthropic_api_key=None, llm_mode="fake",
                            models={"drafter": "fake", "reader": "fake", "improver": "fake"}, cassette_path=""))


# ---------------------------------------------------------------------------------------------------------------
# The approved text is frozen and inserted by code
# ---------------------------------------------------------------------------------------------------------------
def test_approved_language_is_the_locked_text_and_frozen():
    assert dict(APPROVED_LANGUAGE) == {
        "AS-01": "Past performance is not indicative of future results.",
        "AS-02": "The value of investments can fall as well as rise, and you may get back less than you invested.",
    }
    with pytest.raises(TypeError):
        APPROVED_LANGUAGE["AS-01"] = "Past performance is no guarantee of future results."


def test_render_markdown_ends_with_the_approved_text_word_for_word():
    sections = {"what_changed": [{"text": "Ten-year yields stand at 4.8%.", "fact_ids": ["f1"]}]}
    markdown = drafter.render_markdown(sections, _ctx())
    lines = markdown.rstrip("\n").splitlines()
    assert lines[-3:] == ["## Disclosures",
                          f"- {APPROVED_LANGUAGE['AS-01']} (AS-01)",
                          f"- {APPROVED_LANGUAGE['AS-02']} (AS-02)"]
    for text in APPROVED_LANGUAGE.values():
        assert markdown.count(text) == 1


def test_fake_brief_carries_the_disclosures_outside_its_claims_and_passes_the_guardrail(db):
    loop.setup(db)
    brief = loop.make_brief(db, "retirement", None, _fake_llm())
    for text in APPROVED_LANGUAGE.values():
        assert text in brief["markdown"]
    claim_texts = [c["text"] for claims in brief["sections"].values() for c in claims]
    assert not any(text in t for text in APPROVED_LANGUAGE.values() for t in claim_texts)
    assert oracle.check_approved_language(brief, {}) == []


def test_live_prompt_tells_the_model_not_to_write_disclosures():
    ctx = dict(_ctx(), rules=[], guardrails=[])
    system = drafter._build_system_prompt(ctx)
    assert drafter.DISCLOSURE_INSTRUCTION in system
    assert "Do not write disclosures, risk warnings or promises about outcomes" in system


# ---------------------------------------------------------------------------------------------------------------
# The check: look-alikes and promises are flagged; exact text and ordinary facts pass
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Past performance does not guarantee future results.",
    "Past performance is no guarantee of future results.",
    "Historical performance is not indicative of future results.",
    "Previous performance is not a reliable indicator of future results.",
    "Past results are not indicative of future performance.",                    # reordered
    "The value of investments can go down as well as up and you may get back less than you invested.",
    "The value of your investments may go down as well as up, and you could get back less than you put in.",
    "Investments can fall as well as rise and you may get back less than you put in.",
    "You may get back less than you invested.",                                   # half of AS-02
    "The value of investments can fall as well as rise.",
])
def test_reworded_disclosure_is_flagged(text):
    out = _check(text)
    assert len(out) == 1 and "rewords approved disclosure" in out[0], (text, out)


@pytest.mark.parametrize("text, pattern", [
    (PROMISE, "protect your capital"),
    ("The annuity protects their principal.", "protects their principal"),
    ("This plan protects your savings.", "protects your savings"),
    ("Their principal is safe in this fund.", "principal is safe"),
    ("The fund guarantees returns of 5% a year.", "guarantees returns"),
    ("The annuity offers guaranteed income for life.", "guaranteed income"),
    ("The note has guaranteed growth.", "guaranteed growth"),
    ("Tell them the ladder brings guaranteed 5% gains.", "guaranteed 5% gains"),
    ("With this allocation you can't lose.", "can't lose"),
    ("You cannot lose money in a CD ladder.", "cannot lose"),
    ("The client won't lose anything here.", "won't lose"),
    ("It is a risk-free way to grow the account.", "risk-free"),
    ("There is no risk in the bond ladder.", "no risk"),
])
def test_protective_promise_is_flagged(text, pattern):
    out = _check(text)
    assert any("promise" in v and pattern in v for v in out), (text, out)


@pytest.mark.parametrize("aid", sorted(APPROVED_LANGUAGE))
def test_exact_approved_text_in_a_claim_is_flagged(aid):
    """Codex (6096450): a drafter that writes the approved text itself duplicates the code-inserted block."""
    out = _check(APPROVED_LANGUAGE[aid])
    assert len(out) == 1 and f"writes approved disclosure {aid} in a claim" in out[0], out
    out = _check(f"Remind them: {APPROVED_LANGUAGE[aid]}")
    assert len(out) == 1 and aid in out[0], out


def test_approved_text_is_flagged_in_a_claim_but_never_in_the_inserted_block():
    clean = {"talking_points": [{"text": CLEAN, "fact_ids": ["f1"]}]}
    brief = {"sections": clean, "markdown": drafter.render_markdown(clean, _ctx())}
    assert all(text in brief["markdown"] for text in APPROVED_LANGUAGE.values())
    assert oracle.check_approved_language(brief, {}) == []

    drafted = {"talking_points": [{"text": CLEAN, "fact_ids": ["f1"]},
                                  {"text": APPROVED_LANGUAGE["AS-01"], "fact_ids": ["f1"]}]}
    brief = {"sections": drafted, "markdown": drafter.render_markdown(drafted, _ctx())}
    out = oracle.check_approved_language(brief, {})
    assert out == ["talking_points #2: writes approved disclosure AS-01 in a claim (disclosures are inserted by "
                   "code, never drafted)"]


@pytest.mark.parametrize("text", [
    # Codex (6096450): these share most of a disclosure's words but disclaim nothing
    "Past performance and future results are shown in the appendix.",
    "Future results for the fund depend on the past performance of its holdings.",
    "Past performance of the Pinecrest fund was strong, and future results will be reviewed in May.",
    "Results from the past quarter show strong performance; future plans are on hold.",
    "The past performance report and future results summary are attached.",
    "The value of their investments rose 8%, and they may ask whether to invest more.",
    "They may get back to us on the value of the investments they invested last year.",
    "The value of investments they invested in March may rise with rates.",
    "Stocks may fall further, and the value of their investments may not rise soon.",
])
def test_sentences_sharing_a_disclosures_words_but_not_its_meaning_pass(text):
    assert _check(text) == [], text


@pytest.mark.parametrize("text", [
    "Their FDIC-guaranteed deposits are covered up to $250,000.",
    "The buyer plans to buy the shares.",
    "The value of Evelyn's IRA has fallen 8% since March.",
    "The fund's past performance lags its benchmark.",
    "The value of their investments fell 8% in the sell-off, and they may ask what comes next.",
    "Renewal terms are not guaranteed until binding.",
    "The fund is not risk-free.",
    "Mortgage rates are expected to fall as inflation cools.",
    "Will the new portfolio protect my capital?",                                 # a likely client question
    "Is past performance indicative of future results?",
    "Ten-year Treasury yields stand at 4.8%.",
])
def test_exact_text_and_legitimate_facts_pass(text):
    assert _check(text) == [], text


def test_the_worlds_own_fact_texts_never_trip_the_check():
    from pregame.world import fields
    texts = [f["text"] for fl in fields.BASE_FACTS.values() for f in fl]
    texts += [f["text"] for evs in fields.EVENTS.values() for e in evs for f in e["facts"]]
    assert texts and _check(*texts) == []


# ---------------------------------------------------------------------------------------------------------------
# Registered wherever the build lists its checks, and on by default
# ---------------------------------------------------------------------------------------------------------------
def test_check_is_registered_everywhere_and_enabled_by_default():
    assert oracle.GUARDRAIL_CHECKS["approved-language"] is oracle.check_approved_language
    assert "approved-language" in gate.DEFAULT_GUARDRAIL_CHECKS
    assert "approved-language" in improver.BUILTIN_CHECKS
    g = next(g for g in defaults.DEFAULT_GUARDRAILS if g["check"] == "approved-language")
    assert g["enabled"] is True
    assert set(oracle.GUARDRAIL_CHECKS) == gate.DEFAULT_GUARDRAIL_CHECKS == improver.BUILTIN_CHECKS \
        == {g["check"] for g in defaults.DEFAULT_GUARDRAILS}


def test_run_guardrails_includes_it_by_default():
    as_of = datetime(2026, 4, 1, tzinfo=timezone.utc)
    fact = {"_id": "f1", "subject": "treasury_yields", "relation": "ten_year_pct", "valid_from": as_of}
    out = oracle.run_guardrails(_brief(PROMISE), {"facts": [fact], "as_of": as_of,
                                                  "guardrails": defaults.DEFAULT_GUARDRAILS})
    assert len(out) == 1 and any(v.startswith("approved-language: ") and "protect your capital" in v for v in out), out


# ---------------------------------------------------------------------------------------------------------------
# Live path: a promising brief is redrafted once, then blocked and never stored
# ---------------------------------------------------------------------------------------------------------------
class PromisingDrafter:
    """A non-fake stand-in for the live drafter: writes `texts[call]` into every claim (the last one repeats)."""

    is_fake = False

    def __init__(self, *texts):
        self.texts = texts
        self.calls = 0
        self.systems = []

    def model_id(self, role):
        return f"stub-{role}"

    def complete_json(self, role, system, prompt, max_tokens=2000):
        assert role == "drafter"
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        self.systems.append(system)
        ids = re.findall(r"^\[([^\]]+)\] ", prompt, flags=re.M)
        order = re.search(r"Section order \(produce exactly these sections, in this order\): (.+)", prompt).group(1)
        n_q = int(re.search(r"likely_questions: produce exactly (\d+) items", prompt).group(1))
        return {"sections": {name: [{"text": text, "fact_ids": [ids[0]]}
                                    for _ in range(n_q if name == "likely_questions" else 1)]
                             for name in [s.strip() for s in order.split(",")]}}


def test_live_brief_promising_to_protect_capital_is_blocked_and_never_stored(db):
    loop.setup(db)
    stub = PromisingDrafter(PROMISE)
    with pytest.raises(loop.BriefBlocked) as caught:
        loop.make_brief(db, "retirement", None, stub)
    assert stub.calls == 2                                          # one redraft, then fail closed
    assert all(drafter.DISCLOSURE_INSTRUCTION in s for s in stub.systems)
    assert any(v.startswith("approved-language: ") and "protect your capital" in v for v in caught.value.violations)
    assert db.briefs.count_documents({}) == 0
    refused = list(db.ledger.find({"kind": "refused", "actor": "guardrails"}))
    assert len(refused) == 1 and refused[0]["payload"]["what"] == "brief"


def test_live_brief_that_writes_the_disclosure_itself_is_blocked(db):
    loop.setup(db)
    stub = PromisingDrafter(APPROVED_LANGUAGE["AS-02"])
    with pytest.raises(loop.BriefBlocked) as caught:
        loop.make_brief(db, "retirement", None, stub)
    assert stub.calls == 2
    assert any("writes approved disclosure AS-02 in a claim" in v for v in caught.value.violations)
    assert db.briefs.count_documents({}) == 0


def test_live_brief_is_redrafted_and_the_clean_redraft_ships_with_the_approved_text(db):
    loop.setup(db)
    stub = PromisingDrafter(PROMISE, CLEAN)
    brief = loop.make_brief(db, "retirement", None, stub)
    assert stub.calls == 2
    assert PROMISE not in brief["markdown"]
    for text in APPROVED_LANGUAGE.values():
        assert text in brief["markdown"]
    assert db.briefs.count_documents({}) == 1
