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

    field = "retirement"

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
    assert "rejected" in statuses, f"expected a rejected proposal, got statuses={statuses}"
    assert "committed" in statuses, f"expected a committed proposal, got statuses={statuses}"

    # -- a brief made after a commit should use the bumped config version in its receipt.
    committed = [p for p in (proposal1, proposal2) if p is not None and p["status"] == "committed"]
    assert committed, f"expected a committed proposal, got statuses={statuses}"
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
    loop.make_brief(db, "retirement", None, llm)

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
    loop.make_brief(db, "retirement", None, llm)

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


def test_api_cabinet_empty_returns_empty_dict(db):
    from fastapi.testclient import TestClient

    from pregame.web.app import app, get_database

    app.dependency_overrides[get_database] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/cabinet")
        assert resp.status_code == 200
        assert resp.json() == {}
    finally:
        app.dependency_overrides.pop(get_database, None)


def test_api_cabinet_returns_the_latest_run(db):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from pregame.cabinet import RUNS_COLLECTION
    from pregame.web.app import app, get_database

    db[RUNS_COLLECTION].insert_many([
        {"_id": "CR-1", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "scores": {"harness": {"faults_total": 5}}},
        {"_id": "CR-2", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
         "scores": {"harness": {"faults_total": 0}}},
    ])

    app.dependency_overrides[get_database] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/cabinet")
        assert resp.status_code == 200
        body = resp.json()
        assert body["_id"] == "CR-2"
        assert body["scores"]["harness"]["faults_total"] == 0
        json.dumps(body)  # JSON-safe end to end (created_at is a datetime in the stored doc)
    finally:
        app.dependency_overrides.pop(get_database, None)


def test_setup_refuses_a_db_name_that_does_not_look_like_pregame(llm):
    import mongomock

    db = mongomock.MongoClient(tz_aware=True)["some_other_db"]
    with pytest.raises(loop.SetupRefused):
        loop.setup(db, llm)
    assert db.list_collection_names() == []  # refused before any write


def test_setup_yes_bypasses_the_name_check(llm):
    import mongomock

    db = mongomock.MongoClient(tz_aware=True)["some_other_db"]
    counts = loop.setup(db, llm, yes=True)
    assert counts["facts"] > 0


def test_setup_allows_a_pregame_prefixed_name_without_yes(llm):
    import mongomock

    db = mongomock.MongoClient(tz_aware=True)["pregame_anything"]
    counts = loop.setup(db, llm)  # no SetupRefused
    assert counts["facts"] > 0


def test_run_demo_resets_an_odd_database_only_with_yes(db, llm, capsys):
    """`demo` resets its database first, so it honours the same name fence as `setup` (Sol pre-recording review):
    a database not named pregame* is reset only with an explicit yes (`demo --yes`)."""
    import mongomock

    odd_db = mongomock.MongoClient(tz_aware=True)["not-named-pregame"]
    loop.run_demo(odd_db, llm, yes=True)
    assert odd_db.facts.count_documents({}) > 0


def test_status_survives_a_ledger_verify_exception(db, llm, monkeypatch):
    from pregame import ledger

    loop.setup(db, llm)

    def boom(_db):
        raise RuntimeError("simulated ledger hiccup")

    monkeypatch.setattr(ledger, "verify", boom)

    state = loop.status(db)  # must not raise
    assert state["ledger_verified"] is False
    assert state["ledger_checked"] == 0
    assert "simulated ledger hiccup" in (state["ledger_problem"] or "")


def test_api_state_survives_ledger_verify_exception(db, llm, monkeypatch):
    from fastapi.testclient import TestClient

    from pregame import ledger
    from pregame.web.app import app, get_database

    loop.setup(db, llm)

    def boom(_db):
        raise RuntimeError("simulated ledger hiccup")

    monkeypatch.setattr(ledger, "verify", boom)

    app.dependency_overrides[get_database] = lambda: db
    try:
        client = TestClient(app)
        resp = client.get("/api/state")
        assert resp.status_code == 200  # never a 500, even when ledger.verify blows up
        body = resp.json()
        assert body["ledger_verified"] is False
        assert "simulated ledger hiccup" in (body.get("ledger_problem") or "")
    finally:
        app.dependency_overrides.pop(get_database, None)


def test_improver_prompt_is_identical_across_fresh_runs(llm):
    """Replay serves recorded model answers by an exact hash of the prompt, so nothing random (brief ids, proposal
    ids, wall-clock time) may reach the improver's prompt: two fresh runs of the same script must build the same one."""
    import mongomock

    from pregame import improver, versions
    from pregame.world import fields

    field = "retirement"

    def run() -> str:
        db = mongomock.MongoClient(tz_aware=True)["pregame_replay_check"]
        loop.setup(db, llm)
        loop.make_brief(db, field, None, llm)
        for event in fields.EVENTS[field][:2]:
            loop.market_event(db, event["id"], llm)
        view = improver.ImproverView(db)
        cfg = versions.resolve_field_config(db, field)
        assert view.feedback(field, 12), "the script should have filed feedback"
        return improver._live_prompt(field, cfg, view.feedback(field, 12), None, [], view.briefs(field, 1))

    assert run() == run()


def test_demo_honours_the_database_name_fence(llm):
    import mongomock

    wrong = mongomock.MongoClient(tz_aware=True)["production"]
    with pytest.raises(loop.SetupRefused):
        loop.run_demo(wrong, llm)
    assert wrong.list_collection_names() == []                       # nothing was dropped or written
