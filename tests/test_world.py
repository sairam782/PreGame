"""World: simulated markets, scripted events, frozen scenarios and the Mongo store (mongomock, no network)."""
from __future__ import annotations

import copy
import re
from datetime import timedelta

import pytest

from pregame.contracts import FACT_KINDS, FIELDS, SOURCES
from pregame.world import fields as W
from pregame.world import scenarios as S
from pregame.world import store

DIGITS = re.compile(r"\d+(?:\.\d+)?")
V1_KINDS = {"price", "competitor", "demand", "account"}        # v1 include_kinds: no regulation, no disruption
V1_MAX_FACTS = 6
V1_RECENCY_DAYS = 180


@pytest.fixture(scope="module")
def scenarios():
    return S.build_scenarios()


def _oracle():
    return pytest.importorskip("pregame.oracle")


def _has(text: str, term: str) -> bool:
    """Token-boundary match, like the oracle's: "9%" is not inside "19%", "3.4" is not inside "3.45"."""
    return re.search(r"(?<![\w.])" + re.escape(term.lower()) + r"(?!\w)(?!\.\d)", text.lower()) is not None


def _by_id(sc):
    return {f["_id"]: f for f in sc["facts"]}


def _verified_current(sc):
    return [f for f in S.current_facts(sc["facts"], sc["as_of"]) if f["source"] != "analyst_notes"]


# ---------------------------------------------------------------------------------------------------------------
# fields.py
# ---------------------------------------------------------------------------------------------------------------
def test_sim_calendar():
    assert W.SIM_START.isoformat() == "2026-01-05T00:00:00+00:00"
    assert W.sim_date(0) == W.SIM_START
    assert W.sim_date(1) - W.sim_date(0) == timedelta(days=30)
    assert W.sim_date(2, 5) == W.SIM_START + timedelta(days=64)
    assert W.month_of(W.sim_date(3, 29)) == 3


def test_world_shape():
    ids = set()
    for field in FIELDS:
        assert len(W.ACCOUNTS[field]) == 2
        assert 8 <= len(W.BASE_FACTS[field]) <= 14
        assert 5 <= len(W.EVENTS[field]) <= 6
        assert {e["month"] for e in W.EVENTS[field]} == set(W.MONTHS)
        assert any(f["source"] == "account_notes" for f in W.BASE_FACTS[field])
        for account in W.ACCOUNTS[field]:
            assert account["field"] == field and account["exposures"]
            assert not DIGITS.search(account["profile"] + account["name"] + account["counterpart"])
        for e in W.EVENTS[field]:
            assert W.event_by_id(e["id"]) is e
            assert 1 <= len(e["facts"]) <= 3 and e["feedback"] and not DIGITS.search(e["title"])
            assert all(f["event_id"] == e["id"] and f["valid_from"] == W.event_time(e) for f in e["facts"])
            assert W.month_of(W.event_time(e)) == e["month"]
        for f in W.all_facts(field):
            assert f["simulated"] is True and f["field"] == field
            assert f["kind"] in FACT_KINDS and f["source"] in SOURCES
            assert f["_id"] == f"{field}:{f['subject']}:{f['relation']}@{f['valid_from'].date().isoformat()}"
            assert f["_id"] not in ids, f"duplicate fact id {f['_id']}"
            ids.add(f["_id"])
    with pytest.raises(KeyError):
        W.event_by_id("no-such-event")


def test_every_account_fact_names_its_owner():
    for field in FIELDS:
        ids = {a["id"] for a in W.ACCOUNTS[field]}
        owners = set()
        for f in W.all_facts(field):
            assert "account_id" in f, f["_id"]
            if f["kind"] == "account":
                assert f["account_id"] in ids, f["_id"]
                owner = next(a for a in W.ACCOUNTS[field] if a["id"] == f["account_id"])
                assert f["subject"] in owner["exposures"], f["_id"]
                owners.add(f["account_id"])
            else:
                assert f["account_id"] is None, f["_id"]
        assert owners == ids, f"{field}: every account has its own notes"


def test_fact_texts_hold_only_their_own_value():
    for field in FIELDS:
        for f in W.all_facts(field):
            nums = DIGITS.findall(f["text"])
            if W.is_number(f["value"]):
                assert f["value"] > 0, "values are unsigned"
                assert nums == [W.format_value(f["value"])], f["text"]
            else:
                assert nums == [] and f["value"] in f["text"], f["text"]


def test_numbers_are_unique_within_each_field():
    for field in FIELDS:
        seen: dict = {}
        for f in W.all_facts(field):
            if W.is_number(f["value"]):
                owner = seen.setdefault(f["value"], f["subject"])
                assert owner == f["subject"], f"{field}: {f['value']} used by {owner} and {f['subject']}"


def test_supersession_exists_in_every_field():
    for field in FIELDS:
        base = {(f["subject"], f["relation"]): f["value"] for f in W.BASE_FACTS[field]}
        chains: dict = {}
        for f in W.all_facts(field):
            chains.setdefault((f["subject"], f["relation"]), []).append(f["value"])
        superseded = [k for k, vals in chains.items() if len(vals) > 1 and len(set(vals)) > 1]
        assert len(superseded) >= 4, field
        assert sum(1 for k in superseded if k in base) >= 3, field


def test_analyst_notes_are_sometimes_right_and_sometimes_wrong():
    for field in FIELDS:
        verdicts = [W.ANALYST_VERDICTS[f["_id"]] for f in W.all_facts(field) if f["source"] == "analyst_notes"]
        assert "right" in verdicts and "wrong" in verdicts, field
    for f in (f for fl in FIELDS for f in W.all_facts(fl) if f["source"] == "analyst_notes"):
        assert f["relation"].startswith("expected_"), "analyst notes never supersede verified facts"


def test_worlds_mix_world_events_and_client_life_events():
    for field in FIELDS:
        life = [e["id"] for e in W.EVENTS[field] if any(f["kind"] == "account" for f in e["facts"])]
        world = [e["id"] for e in W.EVENTS[field] if any(f["kind"] != "account" for f in e["facts"])]
        assert len(life) >= 3 and len(world) == len(W.EVENTS[field]), field
        notes = [f for f in W.all_facts(field) if f["kind"] == "account" and f["event_id"]]
        replaced = [f for f in notes if any(b["subject"] == f["subject"] and b["relation"] == f["relation"]
                                            for b in W.BASE_FACTS[field])]
        assert replaced, f"{field}: a client note supersedes an older one"


def test_no_fact_trips_the_no_advice_guardrail():
    oracle = _oracle()
    for field in FIELDS:
        for f in W.all_facts(field):
            brief = {"sections": {"what_changed": [{"text": f["text"], "fact_ids": [f["_id"]]}]}}
            assert oracle.check_no_advice(brief, {}) == [], f["text"]


def test_demo_accounts_have_material_regulation_or_disruption_events():
    for field in FIELDS:
        demo = W.ACCOUNTS[field][0]
        hits = {e["id"] for e in W.EVENTS[field] for f in e["facts"]
                if f["subject"] in demo["exposures"] and f["kind"] in ("regulation", "disruption")
                and f["source"] != "analyst_notes"}
        assert len(hits) >= 2, field


# ---------------------------------------------------------------------------------------------------------------
# scenarios.py
# ---------------------------------------------------------------------------------------------------------------
def test_scenario_shape_and_split(scenarios):
    # Two seeds (1, 2) is the lead's deliberate choice (live-evaluation cost); changing it must be deliberate.
    assert {s["seed"] for s in scenarios} == {1, 2}
    assert len(scenarios) == 3 * 6 * 2
    assert len({s["_id"] for s in scenarios}) == len(scenarios)
    for s in scenarios:
        assert s["_id"] == f"{s['field']}:{s['split']}:{s['seed']}:m{s['month']}"
        assert s["split"] == ("tuning" if s["month"] <= 3 else "heldout")
        assert W.month_of(s["as_of"]) == s["month"]
        assert all(f["valid_from"] <= s["as_of"] for f in s["facts"])
        latest_event = max(W.event_time(e) for e in W.EVENTS[s["field"]] if e["month"] == s["month"])
        assert timedelta(days=1) <= s["as_of"] - latest_event <= timedelta(days=7)
        kinds = [q["kind"] for q in s["questions"]]
        assert 5 <= len(kinds) <= 7, s["_id"]
        assert kinds.count("impossible") == 1
        assert 1 <= kinds.count("balance") <= 2
        assert kinds.count("change") >= 2
        assert len({q["id"] for q in s["questions"]}) == len(kinds)
        for q in s["questions"]:
            assert not DIGITS.search(q["text"]), q["text"]


def test_question_terms_match_the_facts(scenarios):
    for s in scenarios:
        facts = _by_id(s)
        current_ids = {f["_id"] for f in S.current_facts(s["facts"], s["as_of"])}
        exposed_texts = [f["text"] for f in _verified_current(s) if f["subject"] in s["account"]["exposures"]]
        all_texts = [f["text"] for f in _verified_current(s)]
        for q in s["questions"]:
            low = q["text"].lower()
            assert not any(_has(low, t) for t in q["key_terms"] + q["forbidden_terms"]), q
            if q["kind"] == "impossible":
                assert q["key_terms"] == q["forbidden_terms"] == q["fact_ids"] == []
                continue
            assert q["fact_ids"] and all(fid in facts for fid in q["fact_ids"]), q
            fact = facts[q["fact_ids"][0]]
            if q["kind"] == "change":
                assert fact["_id"] in current_ids and fact["subject"] in s["account"]["exposures"], q
                assert q["key_terms"] == [W.key_term(fact["value"], fact["unit"])]
                assert _has(fact["text"], q["key_terms"][0])
                older = {W.key_term(g["value"], g["unit"]) for g in s["facts"]
                         if g["subject"] == fact["subject"] and g["_id"] != fact["_id"]}
                assert set(q["forbidden_terms"]) <= older, q
                for t in q["forbidden_terms"]:
                    assert not any(_has(text, t) for text in all_texts), (s["_id"], t)
            else:  # balance
                for t in q["forbidden_terms"]:
                    assert not any(_has(text, t) for text in exposed_texts), (s["_id"], t)
                for t in q["key_terms"]:
                    assert _has(fact["text"], t)


def test_questions_against_the_oracle(scenarios):
    """The frozen oracle agrees with the question bank: current facts pass, stale / rumoured / other-client ones
    fail, and the fake reader can find each change in a brief made of the account's current facts."""
    oracle = _oracle()
    for s in scenarios:
        facts = _by_id(s)
        exposed = [f for f in _verified_current(s) if f["subject"] in s["account"]["exposures"]]
        markdown = "\n".join(f"- {f['text']}" for f in exposed)
        for q in s["questions"]:
            if q["kind"] == "impossible":
                assert oracle.check_answer(q, "Unknown - I need to follow up on that.")[0]
                continue
            fact = facts[q["fact_ids"][0]]
            if q["kind"] == "change":
                assert oracle.check_answer(q, fact["text"])[0], q
                for old in (g for g in s["facts"] if g["subject"] == fact["subject"] and g is not fact
                            and W.key_term(g["value"], g["unit"]) in q["forbidden_terms"]):
                    assert not oracle.check_answer(q, old["text"])[0], (q, old["text"])
                md = markdown + (f"\n- {fact['text']}" if fact["source"] == "analyst_notes" else "")
                answer = oracle.fake_reader_answer(md, q["text"])
                assert oracle.check_answer(q, answer)[0], (s["_id"], q["text"], answer)
            elif q["key_terms"]:  # stable
                assert oracle.check_answer(q, fact["text"])[0], q
                assert not oracle.check_answer(q, "unknown")[0]
            else:  # rumour or unaffected: repeating the fact is a false alarm, not raising it is fine
                assert not oracle.check_answer(q, fact["text"])[0], q
                assert oracle.check_answer(q, "That doesn't apply to you; nothing has changed there.")[0]


def test_client_memory_questions_forbid_the_stale_label(scenarios):
    """The stale-label failure: once a client note is replaced ("paused" -> "ready"), repeating the old label is
    wrong, and the question is keyed to the CURRENT note."""
    oracle = _oracle()
    seen = set()
    for s in scenarios:
        facts = _by_id(s)
        for q in s["questions"]:
            if q["kind"] != "change":
                continue
            fact = facts[q["fact_ids"][0]]
            older = [g for g in s["facts"] if (g["subject"], g["relation"]) == (fact["subject"], fact["relation"])
                     and g["_id"] != fact["_id"] and not W.is_number(g["value"])]
            if fact["kind"] == "account" and older:
                assert fact["account_id"] == s["account"]["id"]
                assert all(g["value"] in q["forbidden_terms"] for g in older), q
                assert not oracle.check_answer(q, older[-1]["text"])[0]
                assert oracle.check_answer(q, fact["text"])[0]
                seen.add(fact["subject"])
    assert {"okafor_risk_preference", "castellan_annuity_stance", "castellan_sale"} <= seen, seen


def test_seeds_change_wording_and_numbers(scenarios):
    by = {(s["field"], s["seed"], s["month"]): s for s in scenarios}
    for field in FIELDS:
        for month in W.MONTHS:
            a, b = by[(field, 1, month)], by[(field, 2, month)]
            assert a["account"]["id"] != b["account"]["id"]
            assert {q["text"] for q in a["questions"]} != {q["text"] for q in b["questions"]}
            fa, fb = _by_id(a), _by_id(b)
            common = [i for i in fa if i in fb and W.is_number(fa[i]["value"])]
            assert sum(fa[i]["value"] != fb[i]["value"] for i in common) >= len(common) // 2
            canonical = {f["_id"]: f["value"] for f in W.all_facts(field)}
            assert any(fa[i]["value"] != canonical[i] for i in common)
    # the same subject's wording differs between seeds
    s1 = S._variant(S._Q["retirement"]["treasury_yields"]["change"], 1, "x")
    s2 = S._variant(S._Q["retirement"]["treasury_yields"]["change"], 2, "x")
    assert s1 != s2


def test_nudges_are_consistent_inside_a_seed(scenarios):
    values: dict = {}
    for s in scenarios:
        for f in s["facts"]:
            key = (s["seed"], f["_id"])
            assert values.setdefault(key, f["value"]) == f["value"]
            if W.is_number(f["value"]):
                assert DIGITS.findall(f["text"]) == [W.format_value(f["value"])]


def test_build_is_deterministic(scenarios):
    again = S.build_scenarios()
    assert [(s["_id"], s["questions"], [f["value"] for f in s["facts"]]) for s in again] == \
           [(s["_id"], s["questions"], [f["value"] for f in s["facts"]]) for s in scenarios]


def test_other_clients_account_notes_stay_out(scenarios):
    for s in scenarios:
        for f in s["facts"]:
            if f["kind"] == "account":
                assert f["account_id"] == s["account"]["id"]
                assert f["subject"] in s["account"]["exposures"]


def _v1_context_ids(sc):
    """v1 default policy, inline: newest per (subject, relation); market_feed + account_notes; no regulation or
    disruption; within 180 days (account facts exempt); newest first; top 6."""
    kept = [f for f in S.current_facts(sc["facts"], sc["as_of"])
            if f["source"] in ("market_feed", "account_notes") and f["kind"] in V1_KINDS
            and (f["kind"] == "account" or sc["as_of"] - f["valid_from"] <= timedelta(days=V1_RECENCY_DAYS))]
    kept.sort(key=lambda f: f["valid_from"], reverse=True)
    return {f["_id"] for f in kept[:V1_MAX_FACTS]}


def test_v1_default_policy_misses_changes_for_demo_accounts(scenarios):
    for field in FIELDS:
        demo = W.ACCOUNTS[field][0]["id"]
        late = [s for s in scenarios if s["field"] == field and s["account"]["id"] == demo and s["month"] >= 3]
        assert {s["month"] for s in late} == {3, 4, 5, 6}
        for s in late:
            v1 = _v1_context_ids(s)
            missed = [q for q in s["questions"] if q["kind"] == "change" and not set(q["fact_ids"]) <= v1]
            assert missed, f"v1 would answer every change question in {s['_id']}"
            relevant = [f for f in _verified_current(s) if f["subject"] in s["account"]["exposures"]]
            assert len(relevant) > V1_MAX_FACTS, s["_id"]


def test_live_scenario_uses_the_facts_as_given():
    field = "families"
    account = W.ACCOUNTS[field][0]
    as_of = W.sim_date(3, 12)
    facts = [f for f in W.all_facts(field) if f["valid_from"] <= as_of]
    sc = S.live_scenario(field, account, facts, as_of)
    assert sc["split"] == "live" and sc["seed"] == 0 and sc["month"] == 3
    canonical = {f["_id"]: f["value"] for f in facts}
    assert all(f["value"] == canonical[f["_id"]] for f in sc["facts"])
    changes = [q for q in sc["questions"] if q["kind"] == "change"]
    assert changes and sum(q["kind"] == "impossible" for q in sc["questions"]) == 1
    assert any("college_savings_rules" in q["fact_ids"][0] for q in changes)  # this month's event is asked about
    empty = S.live_scenario(field, account, list(W.BASE_FACTS[field]), W.sim_date(0, 3))
    assert all(q["kind"] != "change" for q in empty["questions"])


# ---------------------------------------------------------------------------------------------------------------
# store.py
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def ledger_calls(monkeypatch):
    """Record ledger appends; pass them through to pregame.ledger when it is importable."""
    calls = []
    try:
        from pregame import ledger as real
    except ImportError:
        real = None

    def record(db, kind, actor, payload, sim_time):
        calls.append((kind, actor, payload, sim_time))
        return real.append(db, kind, actor, payload, sim_time) if real else None

    monkeypatch.setattr(store, "_ledger_append", record)
    return calls


def test_load_world(db, ledger_calls):
    store.load_world(db)
    assert db.facts.count_documents({}) == sum(len(W.BASE_FACTS[f]) for f in FIELDS)
    assert db.events.count_documents({"fired": False}) == sum(len(W.EVENTS[f]) for f in FIELDS)
    assert store.sim_now(db) == W.sim_date(0)
    assert ledger_calls[-1][0] == "event" and ledger_calls[-1][2]["action"] == "load_world"
    store.load_world(db)                                   # a second load is a clean restart
    assert db.facts.count_documents({}) == sum(len(W.BASE_FACTS[f]) for f in FIELDS)


def test_fire_event_inserts_facts_and_moves_the_clock(db, ledger_calls):
    store.load_world(db)
    event = W.EVENTS["retirement"][0]
    before = len(store.facts_until(db, "retirement", W.sim_date(6, 30)))
    inserted = store.fire_event(db, event["id"])
    assert [f["_id"] for f in inserted] == [f["_id"] for f in event["facts"]]
    assert store.sim_now(db) == W.event_time(event)
    after = store.facts_until(db, "retirement", store.sim_now(db))
    assert len(after) == before + len(event["facts"])
    assert all(f["valid_from"].tzinfo is not None for f in after)
    assert ledger_calls[-1][0] == "event" and ledger_calls[-1][2]["event_id"] == event["id"]
    listed = {e["id"]: e for e in store.list_events(db, "retirement")}
    assert listed[event["id"]]["fired"] and not listed[W.EVENTS["retirement"][1]["id"]]["fired"]
    assert store.fire_event(db, event["id"]) == []          # idempotent
    assert len(store.facts_until(db, "retirement", W.sim_date(6, 30))) == len(after)
    # facts_until respects as_of and the clock never runs backwards
    later = W.EVENTS["retirement"][2]
    store.fire_event(db, later["id"])
    store.fire_event(db, W.EVENTS["retirement"][1]["id"])
    assert store.sim_now(db) == W.event_time(later)
    assert all(f["valid_from"] <= W.event_time(event) for f in store.facts_until(db, "retirement",
                                                                                  W.event_time(event)))
    with pytest.raises(KeyError):
        store.fire_event(db, "nope")


def test_list_events_and_accounts(db):
    events = store.list_events(db)
    assert len(events) == sum(len(W.EVENTS[f]) for f in FIELDS)
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    assert not any(e["fired"] for e in events)
    assert all({"id", "field", "title", "month", "at", "kinds", "fired"} <= set(e) for e in events)
    assert store.get_account("business_owners")["id"] == W.ACCOUNTS["business_owners"][0]["id"]
    assert store.get_account("business_owners", "adeyemi-practice")["name"] == "Dr. Funmi Adeyemi"
    with pytest.raises(KeyError):
        store.get_account("business_owners", "nobody")


def test_load_scenarios_round_trip(db, scenarios):
    store.load_scenarios(db, scenarios)
    store.load_scenarios(db, scenarios)
    assert db.eval_scenarios.count_documents({}) == len(scenarios)
    assert db.eval_scenarios.count_documents({"split": "heldout"}) == len(scenarios) // 2
    doc = db.eval_scenarios.find_one({"_id": "business_owners:heldout:2:m6"})
    assert doc["questions"] == next(s for s in scenarios if s["_id"] == "business_owners:heldout:2:m6")["questions"]


def test_fire_event_happens_exactly_once(db, ledger_calls):
    store.load_world(db)
    event = W.EVENTS["families"][1]
    first = store.fire_event(db, event["id"])
    second = store.fire_event(db, event["id"])
    assert [f["_id"] for f in first] == [f["_id"] for f in event["facts"]] and second == []
    fired = [c for c in ledger_calls if c[2].get("action") == "fire_event" and c[2].get("event_id") == event["id"]]
    assert len(fired) == 1
    if "ledger" in db.list_collection_names():               # the real pregame.ledger is importable
        assert db.ledger.count_documents({"payload.event_id": event["id"]}) == 1
    assert db.facts.count_documents({"event_id": event["id"]}) == len(event["facts"])


def test_fire_event_that_lost_the_claim_writes_nothing(db, ledger_calls):
    """A concurrent caller that already claimed the event wins: this caller inserts no facts and no ledger entry."""
    store.load_world(db)
    event = W.EVENTS["business_owners"][0]
    db.events.update_one({"_id": event["id"]}, {"$set": {"fired": True, "fired_at": W.event_time(event)}})
    n_calls = len(ledger_calls)
    assert store.fire_event(db, event["id"]) == []
    assert db.facts.count_documents({"event_id": event["id"]}) == 0
    assert len(ledger_calls) == n_calls
    assert store.sim_now(db) == W.sim_date(0)


def test_fire_event_never_overwrites_a_stored_fact(db, ledger_calls):
    store.load_world(db)
    event = W.EVENTS["retirement"][0]
    planted = dict(event["facts"][0], text="planted", value=1)
    db.facts.insert_one(planted)
    inserted = store.fire_event(db, event["id"])
    assert [f["_id"] for f in inserted] == [f["_id"] for f in event["facts"][1:]]
    assert db.facts.find_one({"_id": planted["_id"]})["text"] == "planted"


def test_fire_event_without_a_loaded_world(db, ledger_calls):
    event = W.EVENTS["business_owners"][2]
    assert len(store.fire_event(db, event["id"])) == len(event["facts"])
    assert store.fire_event(db, event["id"]) == []
    assert store.sim_now(db) == W.event_time(event)


def test_frozen_scenarios_cannot_be_rewritten(db, scenarios):
    store.load_scenarios(db, scenarios)
    target = next(s for s in scenarios if s["_id"] == "retirement:heldout:1:m4")
    stored_before = db.eval_scenarios.find_one({"_id": target["_id"]})
    changed = copy.deepcopy(target)
    changed["questions"][0]["key_terms"] = ["99%"]
    with pytest.raises(store.FrozenScenarioError, match="frozen scenario differs"):
        store.load_scenarios(db, [changed])
    relabelled = dict(copy.deepcopy(target), split="tuning")
    with pytest.raises(store.FrozenScenarioError):
        store.load_scenarios(db, [relabelled])
    assert db.eval_scenarios.find_one({"_id": target["_id"]}) == stored_before
    assert db.eval_scenarios.count_documents({}) == len(scenarios)
