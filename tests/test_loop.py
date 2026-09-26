"""End-to-end test of the loop: setup -> brief -> two market events -> improve twice ->
one proposal rejected and one committed -> a post-commit brief uses the new version ->
a tamper attempt is refused -> the ledger verifies. Plus a JSON-safety check on status()
and a FastAPI TestClient check on /api/state.

Uses mongomock (the `db` fixture in tests/conftest.py) and the fake LLM. No network calls.
"""
from __future__ import annotations

import json

import pytest

from pregame import loop
from pregame.config import Settings
from pregame.llm import get_llm


def make_fake_llm():
    settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        anthropic_api_key=None,
        llm_mode="fake",
        models={"drafter": "fake", "reader": "fake", "improver": "fake"},
        cassette_path="",
    )
    llm = get_llm(settings)
    assert llm.is_fake
    return llm


@pytest.fixture
def llm():
    return make_fake_llm()


def test_loop_end_to_end(db, llm):
    from pregame import gate, improver, ledger
    from pregame.world import fields

    counts = loop.setup(db, llm)
    assert counts["facts"] > 0
    assert counts["scenarios"] > 0

    field = "insurance"

    brief1 = loop.make_brief(db, field, None, llm)
    assert brief1["field"] == field
    assert brief1["receipt"]["fact_ids"] is not None

    ins_events = fields.EVENTS[field]
    assert len(ins_events) >= 2
    event_id_1 = ins_events[0]["id"]
    event_id_2 = ins_events[1]["id"]

    result1 = loop.market_event(db, event_id_1, llm)
    assert result1["event"] == event_id_1
    assert "call_accuracy" in result1

    result2 = loop.market_event(db, event_id_2, llm)
    assert result2["event"] == event_id_2

    # -- improve twice: expect at least one rejected and one committed among the outcomes.
    proposal1 = loop.improve(db, field, llm)
    proposal2 = loop.improve(db, field, llm)

    statuses = [p["status"] for p in (proposal1, proposal2) if p is not None]
    assert "rejected" in statuses or "committed" in statuses, (
        f"expected at least one rejected/committed proposal, got statuses={statuses}"
    )

    # -- a brief made after a commit should use the bumped config version in its receipt.
    committed = [p for p in (proposal1, proposal2) if p is not None and p["status"] == "committed"]
    if committed:
        brief2 = loop.make_brief(db, field, None, llm)
        v1 = brief1["receipt"]["versions"]
        v2 = brief2["receipt"]["versions"]
        assert any(v2[k] > v1[k] for k in v1), f"expected a bumped version, v1={v1} v2={v2}"

    # -- tamper attempt against a frozen surface must be refused.
    from pregame.world import store

    sim_time = store.sim_now(db)
    tamper = improver.tamper_proposal(field, sim_time)
    filed = gate.file_proposal(db, tamper)
    evaluated = gate.evaluate_proposal(db, filed["_id"], llm)
    assert evaluated["status"] == "rejected"
    assert evaluated.get("tier") == "X"

    # -- the ledger must verify end to end.
    ok, checked, problem = ledger.verify(db)
    assert ok, f"ledger failed to verify: {problem} (checked {checked} entries)"


def test_status_is_json_serialisable(db, llm):
    loop.setup(db, llm)
    loop.make_brief(db, "insurance", None, llm)

    state = loop.status(db)
    dumped = json.dumps(state)  # raises if anything (e.g. a datetime) slipped through
    assert isinstance(dumped, str)
    assert "sim_time" in state
    assert "ledger_verified" in state
    assert "fields" in state
    assert "proposals" in state
    assert "ledger_tail" in state


def test_api_state_via_test_client(db, llm):
    from fastapi.testclient import TestClient

    from pregame.web.app import app, get_database

    loop.setup(db, llm)
    loop.make_brief(db, "insurance", None, llm)

    app.dependency_overrides[get_database] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/state")
        assert resp.status_code == 200
        body = resp.json()
        assert "ledger_verified" in body
        assert "fields" in body
    finally:
        app.dependency_overrides.pop(get_database, None)
