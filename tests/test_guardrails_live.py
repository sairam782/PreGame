"""Guardrails run on every live brief before it is stored or shown (Codex HDY-37, 5e35046).

A prep brief is for the advisor. A brief that gives the client investment advice must never ship: it is redrafted
once, then blocked, recorded in the ledger as `refused`, and never written to `briefs`.
"""
import re

import pytest

from pregame import loop, oracle


class AdvisingDrafter:
    """A non-fake stand-in for the live drafter that always writes allocation advice, correctly cited."""

    is_fake = False

    def __init__(self):
        self.calls = 0

    def model_id(self, role):
        return f"stub-{role}"

    def complete_json(self, role, system, prompt, max_tokens=2000):
        assert role == "drafter"
        self.calls += 1
        ids = re.findall(r"^\[([^\]]+)\] ", prompt, flags=re.M)
        order = re.search(r"Section order \(produce exactly these sections, in this order\): (.+)", prompt).group(1)
        n_q = int(re.search(r"likely_questions: produce exactly (\d+) items", prompt).group(1))
        sections = {}
        for name in [s.strip() for s in order.split(",")]:
            k = n_q if name == "likely_questions" else 1
            sections[name] = [{"text": "You should increase your equity allocation before rates fall.",
                               "fact_ids": [ids[0]]} for _ in range(k)]
        return {"sections": sections}


def _brief(text):
    return {"sections": {"talking_points": [{"text": text, "fact_ids": ["f1"]}]}}


@pytest.mark.parametrize("text", [
    "I recommend allocating more to equities.",
    "You should increase your equity allocation.",
    "Consider moving more into bonds.",
    "Reduce your bond exposure now.",
    "Rebalance into cash before the next meeting.",
])
def test_advisory_phrasings_trip_no_advice(text):
    assert oracle.check_no_advice(_brief(text), {}), text


@pytest.mark.parametrize("text", [
    "Ask whether they want to revisit their allocation after the sale closes.",
    "The client may ask whether to rebalance; the facts don't settle it.",
    "Stocks sold off 12% in March.",
    "Do not recommend moving into annuities; that is the advisor's call.",
])
def test_advisor_prep_wording_passes(text):
    assert oracle.check_no_advice(_brief(text), {}) == [], text


def test_make_brief_blocks_advice_and_never_stores_it(db):
    loop.setup(db)
    drafter = AdvisingDrafter()
    with pytest.raises(loop.BriefBlocked) as caught:
        loop.make_brief(db, "retirement", None, drafter)
    assert drafter.calls == 2                                   # one redraft, then fail closed
    assert any("advice" in v for v in caught.value.violations)
    assert db.briefs.count_documents({}) == 0
    refused = list(db.ledger.find({"kind": "refused", "actor": "guardrails"}))
    assert len(refused) == 1 and refused[0]["payload"]["what"] == "brief"


def test_market_event_with_a_blocked_brief_stores_nothing(db):
    loop.setup(db)
    from pregame.world import fields
    event_id = fields.EVENTS["retirement"][0]["id"]
    out = loop.market_event(db, event_id, AdvisingDrafter())
    assert out["brief_id"] is None and out["blocked"]
    assert db.briefs.count_documents({}) == 0
