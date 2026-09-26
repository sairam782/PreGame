"""Checks for score_preps.py against data version 2: the baseline reproduces the answer key, and harness-mode
scoring follows the data's rules (as_of history, expected ask / flag / brief_both actions, warning level).
The version 3 checks (contact preference, life events, required disclosures, splits) follow the v2 ones."""
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import score_preps as sp  # noqa: E402

SIDE = sp.AnswerSide(sp.DEFAULT_DATA)
D = sp.D
HOLDERS = {"C03": ["Tom Brennan", "Lisa Brennan"]}


def perfect_prep(p):
    """States the truth on its date, quotes the fee from reference data, copies both approved sentences, and
    carries every action the answer key expects for this prep."""
    cid, day = p["client_id"], D(p["date"])
    expected = SIDE.expected_for(p)
    claims = []
    for a in sp.TRUTH_ATTRS:
        value = SIDE.truth_at(cid, a, day)
        if a == "decision_maker" and (a, "brief_both") in expected:
            value = HOLDERS[cid]
        claims.append({"attribute": a, "value": value, "kind": "observation"})
    if cid == "C05":
        claims.append({"attribute": "fee_rate", "value": SIDE.fee_at(day)[0], "kind": "fee_quote"})
    claims += [{"attribute": "disclosure", "value": k, "kind": "disclosure", "text": t} for k, t in SIDE.approved.items()]
    claims += [{"attribute": a, "value": None, "kind": sp.ACTION_KIND[act]} for a, act in expected if act in sp.ACTION_KIND]
    return {"prep_id": p["prep_id"], "client_id": cid, "date": p["date"], "text": "", "claims": claims}


def test_data_is_version_2_with_expected_actions():
    assert SIDE.answer_key.get("expected"), "answer key has no expected list"
    assert len(SIDE.answer_key["expected"]) == 10


def test_baseline_matches_answer_key():
    results = [sp.score_prep(SIDE, p) for p in sp.baseline_preps(SIDE)]
    ok, missing, extra = sp.check_against_answer_key(SIDE, results)
    assert ok, (missing, extra)
    s = sp.summarize(results)
    assert (s["preps"], s["preps_with_a_fault"], s["faults_total"]) == (24, 17, 33)
    assert s["expected_actions"] == 10 and s["actions_hit"] == 0


def test_perfect_preps_score_clean_and_do_every_action():
    results = [sp.score_prep(SIDE, perfect_prep(p)) for p in SIDE.preps_baseline]
    s = sp.summarize(results)
    assert s["faults_total"] == 0 and s["trusted_prep_rate"] == 1.0
    assert s["action_recall"] == 1.0 and s["questions_unneeded"] == 0


def test_as_of_history_is_scored_on_its_own_date():
    p = next(p for p in SIDE.preps_baseline if p["prep_id"] == "P-0007")   # C02, 2026-07-19
    prep = perfect_prep(p)
    prep["claims"].append({"attribute": "investing_style", "value": "index_only", "kind": "observation",
                           "as_of": "2021-03-10"})
    r = sp.score_prep(SIDE, prep)
    assert r["faults"] == [] and r["history_claims"] == 1
    prep["claims"][-1].pop("as_of")
    r = sp.score_prep(SIDE, prep)
    assert [f["fault_type"] for f in r["faults"]] == ["said_vs_did"]


def test_reworded_disclosure_levels_and_missing_one():
    p = perfect_prep(SIDE.preps_baseline[0])
    p["claims"] = [c for c in p["claims"] if c["attribute"] != "disclosure"]
    p["claims"].append({"attribute": "disclosure", "value": "AS-01", "kind": "disclosure",
                        "text": "Past performance is no guarantee of future results."})
    p["claims"].append({"attribute": "disclosure", "value": "AS-02", "kind": "disclosure",
                        "text": "This portfolio is built to protect your capital."})
    r = sp.score_prep(SIDE, p)
    drift = sorted((f["severity"], f["level"]) for f in r["faults"] if f["fault_type"] == "compliance_drift")
    assert drift == [(1, "warning"), (4, "fault")]


def test_flag_is_distinct_from_ask():
    p = next(p for p in SIDE.preps_baseline if p["prep_id"] == "P-0004")   # C01: ask and flag on risk_attitude
    prep = perfect_prep(p)
    prep["claims"] = [c for c in prep["claims"] if c.get("kind") != "flag"]
    r = sp.score_prep(SIDE, prep)
    assert ["risk_attitude", "ask"] in r["actions_hit"] and ["risk_attitude", "flag"] not in r["actions_hit"]


def test_control_client_expects_nothing():
    for p in SIDE.preps_baseline:
        if p["client_id"] == "C06":
            assert SIDE.expected_for(p) == []


# ---------------------------------------------------------------------------------------------- data version 3
DATA = sp.HERE / "data"
V3_SETS = {   # folder -> (preps, preps_with_a_fault, ignoring warnings, expected actions by type), from each answer key
    "pregamev0_abhi_cabinet20": (73, 48, 47, {"ask": 32, "flag": 11, "brief_both": 2}),
    "pregamev0_abhi_cabinet80": (291, 183, 182, {"ask": 121, "flag": 43, "brief_both": 7}),
}
_SIDES = {}


def side3(name):
    root = DATA / name / "out"
    if not root.exists():
        pytest.skip(f"{name} not present")
    if name not in _SIDES:
        _SIDES[name] = sp.AnswerSide(root)
    return _SIDES[name]


def baseline3(name):
    s = side3(name)
    return s, [sp.score_prep(s, p) for p in sp.baseline_preps(s)]


def perfect_prep_v3(side, p):
    """Current truth for every label (contact_preference included), the fee from reference data, the required
    disclosures word for word, every due life event mentioned, and every action the answer key expects."""
    cid, day = p["client_id"], sp.D(p["date"])
    expected = side.expected_for(p)
    account = side.clients[cid]["accounts"][0]
    claims = []
    for a in side.truth_attrs:
        value = side.truth_at(cid, a, day)
        if a == "decision_maker" and (a, "brief_both") in expected:
            value = list(account["holders"])
        claims.append({"attribute": a, "value": value, "kind": "observation"})
    if account.get("advisory"):
        claims.append({"attribute": "fee_rate", "value": side.fee_at(day)[0], "kind": "fee_quote"})
    claims += [{"attribute": "disclosure", "value": k, "kind": "disclosure", "text": side.approved[k]}
               for k in side.required_disclosures(cid, day)]
    claims += [{"attribute": "life_event", "value": e["event_id"], "kind": "observation"}
               for e in side.life_events_due(cid, day)]
    claims += [{"attribute": a, "value": None, "kind": sp.ACTION_KIND[act]} for a, act in expected if act in sp.ACTION_KIND]
    return {"prep_id": p["prep_id"], "client_id": cid, "date": p["date"], "text": "", "claims": claims}


@pytest.mark.parametrize("name", list(V3_SETS) + ["banker_sim_v3"])
def test_v3_baseline_matches_answer_key(name):
    side, results = baseline3(name)
    assert side.version == 3 and "contact_preference" in side.truth_attrs
    ok, missing, extra = sp.check_against_answer_key(side, results)
    assert ok, (missing, extra)


@pytest.mark.parametrize("name", list(V3_SETS))
def test_v3_baseline_counts(name):
    side, results = baseline3(name)
    s = sp.summarize(results, side.expected_source)
    key, exp_rows = side.answer_key["summary"], side.answer_key["expected"]
    preps, faulty, strict, by_type = V3_SETS[name]
    assert (s["preps"], s["preps_with_a_fault"], s["preps_with_a_fault_ignoring_warnings"]) == (preps, faulty, strict)
    assert (key["preps"], key["preps_with_a_fault"], key["preps_with_a_fault_ignoring_warnings"]) == (preps, faulty, strict)
    by_type = dict(sorted(by_type.items()))
    assert s["expected_actions_by_type"] == by_type == dict(sorted(Counter(r["action"] for r in exp_rows).items()))
    assert s["expected_actions"] == len(exp_rows) and s["actions_hit"] == 0
    assert side.ladder_source == "answer key"          # no gen.py beside these exports


@pytest.mark.parametrize("name", list(V3_SETS))
def test_v3_baseline_has_no_missing_disclosure(name):
    side, results = baseline3(name)
    assert not [f for r in results for f in r["faults"] if f["fault_type"] == "missing_disclosure"]
    ok, bad = sp.check_required_disclosures(side)        # the scorer requires exactly what the generator wrote
    assert ok, bad[:5]
    required = [side.required_disclosures(p["client_id"], sp.D(p["date"])) for p in side.preps_baseline]
    assert any("AS-09" in r for r in required) and any("AS-08" in r for r in required)


@pytest.mark.parametrize("name", list(V3_SETS))
def test_v3_perfect_preps_are_clean(name):
    side = side3(name)
    results = [sp.score_prep(side, perfect_prep_v3(side, p)) for p in side.preps_baseline]
    s = sp.summarize(results, side.expected_source)
    assert s["faults_total"] == 0 and s["trusted_prep_rate"] == 1.0, [r["faults"] for r in results if r["faults"]][:3]
    assert s["action_recall"] == 1.0 and s["questions_unneeded"] == 0


def test_v3_missed_and_wrong_life_events():
    side = side3("pregamev0_abhi_cabinet20")
    p = next(p for p in side.preps_baseline if side.life_events_due(p["client_id"], sp.D(p["date"])))
    due = [e["event_id"] for e in side.life_events_due(p["client_id"], sp.D(p["date"]))]
    prep = perfect_prep_v3(side, p)
    prep["claims"] = [c for c in prep["claims"] if not (c["attribute"] == "life_event" and c["value"])]
    r = sp.score_prep(side, prep)
    assert sorted(f["truth"] for f in r["faults"] if f["fault_type"] == "missed_life_event") == sorted(due)
    assert ["life_event", "ask"] in r["actions_hit"]      # asking without naming the event is still the action
    prep["claims"].append({"attribute": "life_event", "value": due[0], "kind": "question"})    # a question mentions it
    prep["claims"].append({"attribute": "life_event", "value": "E-99999", "kind": "question"})  # not the client's event
    r = sp.score_prep(side, prep)
    assert [f["claimed"] for f in r["faults"] if f["fault_type"] == "missed_life_event"] == ["E-99999"]
    event_day = next(e["date"] for e in side.life_events[p["client_id"]] if e["event_id"] == due[0])
    prep["claims"][-1] = {"attribute": "life_event", "value": due[0], "kind": "observation", "as_of": "2026-01-01"}
    r = sp.score_prep(side, prep)                           # mentioned as of a date before it happened
    assert event_day > "2026-01-01" and [f["claimed"] for f in r["faults"]] == [due[0]]


def test_v3_contact_preference_is_a_label():
    side = side3("pregamev0_abhi_cabinet20")
    p = next(p for p in side.preps_baseline
             if side.truth_at(p["client_id"], "contact_preference", sp.D(p["date"])) == "email_only")
    prep = perfect_prep_v3(side, p)
    for c in prep["claims"]:
        if c["attribute"] == "contact_preference":
            c["value"] = "phone_ok"
    r = sp.score_prep(side, prep)
    assert [(f["fault_type"], f["attribute"]) for f in r["faults"]] == [("sticky_label", "contact_preference")]


def test_v3_required_disclosures_and_optional_extra():
    side = side3("pregamev0_abhi_cabinet20")
    p = side.preps_baseline[0]
    prep = perfect_prep_v3(side, p)
    required = side.required_disclosures(p["client_id"], sp.D(p["date"]))
    assert "AS-10" not in required
    prep["claims"].append({"attribute": "disclosure", "value": "AS-10", "kind": "disclosure", "text": side.approved["AS-10"]})
    assert sp.score_prep(side, prep)["faults"] == []        # an optional sentence copied exactly is fine
    prep["claims"] = [c for c in prep["claims"] if c.get("value") != required[-1]]
    r = sp.score_prep(side, prep)
    assert [(f["fault_type"], f["required"]) for f in r["faults"]] == [("missing_disclosure", required[-1])]


@pytest.mark.parametrize("name", list(V3_SETS))
def test_v3_by_split_adds_up(name):
    side, results = baseline3(name)
    s = sp.summarize(results, side.expected_source)
    tune, test = s["by_split"]["tune"], s["by_split"]["test"]
    assert "by_split" not in tune and "by_split" not in test
    assert tune["preps"] > 0 and test["preps"] > 0
    for k in ("preps", "preps_with_a_fault", "faults_total", "expected_actions"):
        assert tune[k] + test[k] == s[k], k
    tune_clients = {c for c, split in side.splits.items() if split == "tune"}
    assert set(tune["faulty_preps_by_client"]) == tune_clients
