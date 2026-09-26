"""The live-model cabinet experiment with a stub LLM (no model calls, no scorer subprocess)."""
import threading

import pytest

from pregame import cabinet, cabinet_live as cl
from pregame import cli

VISIBLE = cabinet.visible_dir()
pytestmark = pytest.mark.skipif(not (VISIBLE / "notes.json").exists(), reason="cabinet-eval visible data not present")


@pytest.fixture(scope="module")
def data():
    return cabinet.load_files(VISIBLE)


@pytest.fixture(scope="module")
def facts(data):
    return cabinet.build_facts(data)


class _Settings:
    llm_mode = "live"
    cli_concurrency = 4


class StubLLM:
    """Answers every call with a fixed reply (a mix of good and malformed claims) and records the calls."""
    is_fake = False
    settings = _Settings()

    def __init__(self, reply=None, fail_on=None):
        self.calls = []
        self._lock = threading.Lock()
        self.reply = reply
        self.fail_on = fail_on

    def model_id(self, role):
        return "stub-sonnet"

    def complete_json(self, role, system, prompt, max_tokens=2000):
        with self._lock:
            self.calls.append((role, system, prompt))
        if self.fail_on and self.fail_on in prompt:
            raise RuntimeError("stub failure")
        if self.reply is not None:
            return self.reply
        return {"text": "Prep text.", "claims": [
            {"attribute": "risk_attitude", "value": "growth", "kind": "observation", "basis": ["E-0320"]},
            {"attribute": "investing_style", "value": None, "kind": "question", "basis": ["E-0327"]},
            {"attribute": "risk_attitude", "value": "fearless", "kind": "observation"},          # not in vocabulary
            {"attribute": "mood", "value": "happy", "kind": "observation"},                      # bad attribute
            {"attribute": "risk_attitude", "value": "growth", "kind": "question"},               # question with value
            {"attribute": "risk_attitude", "value": "cautious", "kind": "observation", "as_of": "2026-05-13"},
            "not a claim",
            {"attribute": "disclosure", "value": "AS-01", "kind": "disclosure",
             "text": "Past performance is not indicative of future results."},
        ]}


def _slot(data, cid, n):
    return cl.select_slots(data, [cid])[n]


# --- prompts -----------------------------------------------------------------------------------------------------
def test_prompts_built_for_both_conditions_and_deterministic(data, facts):
    for cond in cl.CONDITIONS:
        for pid, cid, day in cl.select_slots(data):
            a = cl.build_prompt(data, facts, cid, day, pid, cond)
            b = cl.build_prompt(cabinet.load_files(VISIBLE), cabinet.build_facts(data), cid, day, pid, cond)
            assert a == b and len(a) > 500
            assert "run" not in a.lower().split("prep date")[0]            # no run index in the instructions


def test_instructions_identical_but_for_the_disclosure_rule(data, facts):
    pid, cid, day = _slot(data, "C02", 2)
    raw = cl.build_prompt(data, facts, cid, day, pid, "no_harness")
    har = cl.build_prompt(data, facts, cid, day, pid, "harness")
    head_raw, head_har = raw.split("\nMATERIAL")[0], har.split("\nMATERIAL")[0]
    assert head_raw.replace(cl.DISCLOSURE_RULES["no_harness"], "X") == head_har.replace(cl.DISCLOSURE_RULES["harness"], "X")
    assert "decision_maker" in head_raw and "as_of" in head_raw


def test_raw_prompt_carries_raw_material_as_of_the_date(data, facts):
    pid, cid, day = _slot(data, "C01", 3)                                  # P-0004, 2026-08-10
    raw = cl.build_prompt(data, facts, cid, day, pid, "no_harness")
    assert "N-0014 junior: Monthly check-in call. No changes." in raw      # raw notes, with author and date
    assert "2026-07-20 N-0014" in raw
    assert "N-0017" not in raw                                              # 2026-08-27: after the prep date
    assert "E-0320" in raw and "tool_used" in raw                           # feed events
    assert "M-0001 index_move" in raw                                       # market items for the index fund held
    assert "FEE-v1" in raw                                                  # the reference table
    assert "Past performance is not indicative of future results." in raw  # the approved texts
    assert "Current-state facts" not in raw


def test_harness_prompt_has_compiled_facts_and_no_raw_no_changes_fact(data, facts):
    pid, cid, day = _slot(data, "C04", 3)                                   # Priya: junior "No changes" after the plan
    har = cl.build_prompt(data, facts, cid, day, pid, "harness")
    assert "Current-state facts" in har
    assert "- retirement_date: 2027-06 | source" in har                     # the plan survives the "No changes"
    assert "Monthly check-in call" not in har and "No changes." not in har  # no raw junior note text
    facts_block = har.split("Current-state facts")[1].split("Compiled client file")[0]
    assert "no_changes" not in facts_block and "contact" not in facts_block
    pid, cid, day = _slot(data, "C01", 3)
    har = cl.build_prompt(data, facts, cid, day, pid, "harness")
    assert "- risk_attitude: growth | source E-0320" in har
    assert "Watch-outs" in har and "Flag for the banker" in har            # the derived flag
    assert "Disclosures:" not in har                                        # the model never sees a disclosure to copy
    pid, cid, day = _slot(data, "C05", 3)                                   # advisory: fee from the reference table
    har = cl.build_prompt(data, facts, cid, day, pid, "harness")
    assert "- fee_rate: 0.75 | source FEE-v2" in har


# --- replies -----------------------------------------------------------------------------------------------------
def test_malformed_claims_dropped_and_disclosures_appended_for_harness_only(data):
    slot = _slot(data, "C01", 1)
    reply = StubLLM().complete_json("drafter", "", "")
    raw = cl.finalize_prep(data, slot, "no_harness", reply)
    har = cl.finalize_prep(data, slot, "harness", reply)
    assert raw["malformed_claims"] == 5 and har["malformed_claims"] == 5
    assert [c["kind"] for c in raw["claims"]] == ["observation", "question", "disclosure"]
    assert raw["model_disclosures_dropped"] == 0 and "Disclosures:" not in raw["text"]
    locked = cabinet.locked_disclosures(data)
    assert har["model_disclosures_dropped"] == 1
    dis = [c for c in har["claims"] if c["kind"] == "disclosure"]
    assert [(c["value"], c["text"]) for c in dis] == [(i, locked[i]) for i in sorted(locked)]
    assert all(t in har["text"] for t in locked.values())
    for p in (raw, har):
        assert cabinet.validate_claims({k: p[k] for k in ("prep_id", "client_id", "date", "text", "claims")}, data) == []


def test_garbage_replies_do_not_crash(data):
    slot = _slot(data, "C03", 0)
    for reply in ({}, {"text": 5, "claims": "x"}, {"claims": None}, {"claims": [{"attribute": "decision_maker",
                                                                                "value": "Nobody", "kind": "observation"}]}):
        p = cl.finalize_prep(data, slot, "no_harness", reply)
        assert p["claims"] == [] and isinstance(p["text"], str)
    ok = cl.finalize_prep(data, slot, "no_harness", {"claims": [
        {"attribute": "decision_maker", "value": ["Tom Brennan", "Lisa Brennan"], "kind": "observation"}]})
    assert ok["claims"][0]["value"] == ["Tom Brennan", "Lisa Brennan"] and ok["malformed_claims"] == 0


# --- the experiment ------------------------------------------------------------------------------------------------
def test_run_experiment_asks_identical_prompts_each_run(data):
    llm = StubLLM()
    exp = cl.run_experiment(data, llm, runs=2, clients=["C01", "C03"], limit=2, concurrency=3)
    assert len(exp["slots"]) == 4 and len(llm.calls) == 4 * 2 * 2
    assert {r for r, _, _ in llm.calls} == {"drafter"}
    prompts = [p for _, _, p in llm.calls]
    assert len(set(prompts)) == 8 and all(prompts.count(p) == 2 for p in prompts)
    assert {s for _, s, _ in llm.calls} == {cl.SYSTEM}
    for key in [(c, r) for c in cl.CONDITIONS for r in (1, 2)]:
        assert len(exp["results"][key]["preps"]) == 4 and exp["results"][key]["errors"] == []


def test_fake_llm_refused(data):
    class Fake(StubLLM):
        is_fake = True
    with pytest.raises(RuntimeError):
        cl.run_experiment(data, Fake(), runs=1, clients=["C01"], limit=1)


def test_failed_call_is_recorded_and_slot_dropped_from_both_conditions(data):
    llm = StubLLM(fail_on="for a call on 2026-06-20")                        # C01's second slot, both conditions
    exp = cl.run_experiment(data, llm, runs=1, clients=["C01"], limit=2, concurrency=2, attempts=2)
    assert len(exp["results"][("no_harness", 1)]["errors"]) == 1
    paired = cl.paired_preps(exp, 1)
    assert [p["prep_id"] for p in paired["harness"]] == [p["prep_id"] for p in paired["no_harness"]] == ["P-0001"]


def _fake_scorer(preps):
    n = len(preps)
    faulty = sum(1 for p in preps if not any(c["kind"] == "disclosure" for c in p["claims"]))
    return {"preps": n, "preps_with_a_fault": faulty, "preps_with_a_fault_ignoring_warnings": faulty,
            "faults_total": faulty, "faults_by_type": {"missing_disclosure": faulty}, "forbidden_promises": 0,
            "expected_actions": 2, "actions_hit": 1, "expected_actions_by_type": {"ask": 1, "flag": 1},
            "actions_hit_by_type": {"ask": 1}, "questions_unneeded": 0}


def test_table_computes_means():
    s = lambda f, hit: {"preps": 4, "preps_with_a_fault": f, "preps_with_a_fault_ignoring_warnings": f,
                        "faults_total": f * 2, "faults_by_type": {"sticky_label": f}, "forbidden_promises": 0,
                        "expected_actions": 4, "actions_hit": hit, "expected_actions_by_type": {"ask": 4},
                        "actions_hit_by_type": {"ask": hit}, "questions_unneeded": 1, "malformed_claims": f,
                        "call_errors": 0}
    scores = {("no_harness", 1): s(3, 0), ("no_harness", 2): s(2, 1), ("harness", 1): s(0, 4), ("harness", 2): s(1, 3)}
    ref = lambda f, hit: {k: v for k, v in s(f, hit).items() if k not in ("malformed_claims", "call_errors")}
    refs = {"baseline": ref(4, 0), "code_harness": ref(0, 4)}
    lines, rows = cl.table(scores, refs, runs=2)
    assert lines[0].split()[1:] == ["no-h", "#1", "no-h", "#2", "no-h", "mean", "harn", "#1", "harn", "#2", "harn",
                                    "mean", "baseline", "code", "harn"]
    byl = {r[0].strip(): r[1:] for r in rows}
    fault = byl["preps with a fault"]
    assert fault[2] == (2.5, 4, "pct") and fault[5] == (0.5, 4, "pct")
    assert cl._fmt(fault[2]) == "2.5/4 (62%)" and cl._fmt(fault[0]) == "3/4 (75%)"
    assert byl["expected actions done"][2] == (0.5, 4, "frac") and byl["expected actions done"][5] == (3.5, 4, "frac")
    assert byl["faults (total)"][2][0] == 5.0
    assert byl["malformed claims dropped"][6] is None                       # the reference columns carry no counts
    assert any(l.startswith("  sticky_label") for l in lines)


def test_score_and_store_experiment(db, data):
    exp = cl.run_experiment(data, StubLLM(), runs=2, clients=["C02"], limit=2, concurrency=2)
    scores = cl.score_experiment(exp, scorer=_fake_scorer)
    assert scores[("no_harness", 1)]["preps"] == 2 and scores[("harness", 2)]["preps_with_a_fault"] == 0
    assert scores[("no_harness", 1)]["malformed_claims"] == 10
    assert scores[("no_harness", 1)]["claims_kept"] == 6 and scores[("harness", 1)]["claims_kept"] == 8
    receipts = cl.store_experiment(db, exp, scores, data["source"])
    assert len(receipts) == 4 and len({r["_id"] for r in receipts}) == 4
    doc = db[cabinet.RUNS_COLLECTION].find_one({"condition": "harness", "run_index": 2})
    assert doc["model"] == "stub-sonnet" and doc["prep_ids"] == ["P-0005", "P-0006"] and doc["scores"]["preps"] == 2
    assert db[cabinet.PREPS_COLLECTION].count_documents({"condition": "no_harness", "run_index": 1}) == 2
    refs = cl.reference_scores(data, exp["slots"], scorer=_fake_scorer, baseline_scorer=lambda: _fake_scorer([]))
    assert refs["code_harness"]["preps"] == 2 and refs["code_harness"]["preps_with_a_fault"] == 0


def test_store_refuses_a_non_pregame_database(data):
    mongomock = pytest.importorskip("mongomock")
    exp = cl.run_experiment(data, StubLLM(), runs=1, clients=["C06"], limit=1)
    with pytest.raises(ValueError):
        cl.store_experiment(mongomock.MongoClient()["cabinet"], exp, cl.score_experiment(exp, _fake_scorer), "x")


def test_select_slots_and_cli_flags(data):
    assert [s[0] for s in cl.select_slots(data, ["C01"], 1)] == ["P-0001"]
    assert len(cl.select_slots(data)) == 24
    with pytest.raises(ValueError):
        cl.select_slots(data, ["C99"])
    args = cli.build_parser().parse_args(["cabinet-live", "--runs", "3", "--clients", "C01,C02", "--limit", "1",
                                          "--concurrency", "2"])
    assert (args.runs, args.clients, args.limit, args.concurrency) == (3, "C01,C02", 1, 2)
    args = cli.build_parser().parse_args(["cabinet-live"])
    assert (args.runs, args.clients, args.limit, args.concurrency) == (2, None, None, 4)


# --- code-owned claims (harness arm, post-processing only) --------------------------------------------------------
_SINGLE_DM = {"text": "Prep text.", "claims": [
    {"attribute": "decision_maker", "value": "Lisa Brennan", "kind": "observation", "basis": ["N-0029"]},
    {"attribute": "decision_maker", "value": None, "kind": "question", "basis": ["N-0028"]}]}


def test_harness_decision_maker_is_code_owned_on_a_conflict(data, facts):
    slot = _slot(data, "C03", 1)                                             # P-0010: the Brennans, brief both
    code = cabinet.write_prep(data, facts, slot[1], slot[2], cabinet.HARNESS_POLICY, slot[0])
    code_dm = [c for c in code["claims"] if c["attribute"] == "decision_maker"][0]
    har = cl.finalize_prep(data, slot, "harness", _SINGLE_DM, facts=facts)
    dm = [c for c in har["claims"] if c["attribute"] == "decision_maker"]
    assert [c for c in dm if c["kind"] == "observation"] == [code_dm]          # the harness's list, not the model's
    assert code_dm["value"] == ["Tom Brennan", "Lisa Brennan"]
    assert [c["kind"] for c in dm] == ["question", "observation"]            # the model's question stays
    assert har["code_owned"] == ["disclosure", "decision_maker"]
    assert har["model_decision_maker_replaced"] == ["Lisa Brennan"]
    assert "Decision maker: brief both holders (Tom Brennan, Lisa Brennan)." in har["text"]
    assert har["text"].index("Decision maker: brief both") < har["text"].index("Disclosures:")
    assert cabinet.validate_claims({k: har[k] for k in ("prep_id", "client_id", "date", "text", "claims")}, data) == []
    # the model wrote no decision maker at all: code inserts it
    ins = cl.finalize_prep(data, slot, "harness", {"text": "x", "claims": []}, facts=facts)
    assert [c["value"] for c in ins["claims"] if c["attribute"] == "decision_maker"] == [code_dm["value"]]
    # the old behaviour stays reproducible: disclosures only
    old = cl.finalize_prep(data, slot, "harness", _SINGLE_DM, code_owned="disclosures", facts=facts)
    assert [c["value"] for c in old["claims"] if c["attribute"] == "decision_maker"] == ["Lisa Brennan", None]
    assert old["code_owned"] == ["disclosure"] and "brief both" not in old["text"]
    # the no-harness arm is untouched
    raw = cl.finalize_prep(data, slot, "no_harness", _SINGLE_DM, facts=facts)
    assert [c["value"] for c in raw["claims"]] == ["Lisa Brennan", None] and raw["code_owned"] == []
    assert raw["text"] == "Prep text."


def test_no_conflict_leaves_the_models_decision_maker(data, facts):
    for slot in (_slot(data, "C03", 0), _slot(data, "C01", 1)):             # P-0009 (no conflict yet), P-0002
        reply = {"text": "t", "claims": [{"attribute": "decision_maker", "value": "Lisa Brennan" if slot[1] == "C03"
                                          else "Daniel Reyes", "kind": "observation", "basis": []}]}
        har = cl.finalize_prep(data, slot, "harness", reply, facts=facts)
        assert har["code_owned"] == ["disclosure"] and har["model_decision_maker_replaced"] == []
        assert [c["value"] for c in har["claims"] if c["attribute"] == "decision_maker"] == [reply["claims"][0]["value"]]


def test_parse_code_owned():
    assert cl.parse_code_owned("disclosures") == ("disclosure",)
    assert cl.parse_code_owned("disclosures,decision_maker") == ("disclosure", "decision_maker")
    assert cl.parse_code_owned(["decision_maker", "disclosure"]) == ("disclosure", "decision_maker")
    for bad in ("decision_maker", "disclosures,fee_rate", ""):
        with pytest.raises(ValueError):
            cl.parse_code_owned(bad)


def test_experiment_code_owned_scores_note_and_store(db, data):
    exp = cl.run_experiment(data, StubLLM(reply=_SINGLE_DM), runs=2, clients=["C03"], limit=2, concurrency=2)
    assert exp["code_owned"] == ["disclosure", "decision_maker"]
    by_id = {p["prep_id"]: p for p in exp["results"][("harness", 1)]["preps"]}
    assert by_id["P-0009"]["code_owned"] == ["disclosure"]
    assert by_id["P-0010"]["code_owned"] == ["disclosure", "decision_maker"]
    assert all(p["code_owned"] == [] for p in exp["results"][("no_harness", 1)]["preps"])
    scores = cl.score_experiment(exp, scorer=_fake_scorer)
    assert scores[("harness", 1)]["code_owned_by_claim"] == {"disclosure": 2, "decision_maker": 1}
    assert scores[("no_harness", 1)]["code_owned_by_claim"] == {"disclosure": 0, "decision_maker": 0}
    lines, rows = cl.table(scores, runs=2)
    byl = {r[0].strip(): r[1:] for r in rows}
    assert byl["code-owned decision_maker"][3] == (1, None, "n") and byl["code-owned decision_maker"][0] == (0, None, "n")
    note = cl.code_owned_note(exp)
    assert "decision_maker (preps P-0010)" in note and "disclosure (every prep)" in note
    cl.store_experiment(db, exp, scores, data["source"])
    doc = db[cabinet.PREPS_COLLECTION].find_one({"condition": "harness", "run_index": 1, "prep_id": "P-0010"})
    assert doc["code_owned"] == ["disclosure", "decision_maker"] and doc["model_decision_maker_replaced"] == ["Lisa Brennan"]
    assert db[cabinet.RUNS_COLLECTION].find_one({"condition": "harness", "run_index": 1})["code_owned"] == \
        ["disclosure", "decision_maker"]
    assert db[cabinet.RUNS_COLLECTION].find_one({"condition": "no_harness", "run_index": 1})["code_owned"] == []
    # the old setting, and the prompts do not change with it
    old = cl.run_experiment(data, StubLLM(reply=_SINGLE_DM), runs=1, clients=["C03"], limit=2, code_owned="disclosures")
    assert old["prompts"] == exp["prompts"] and cl.prompt_digest(old) == cl.prompt_digest(exp)
    assert all(p["code_owned"] == ["disclosure"] for p in old["results"][("harness", 1)]["preps"])
    assert "decision_maker" not in cl.code_owned_note(old)


def test_harness_arm_alone(data):
    llm = StubLLM(reply=_SINGLE_DM)
    exp = cl.run_experiment(data, llm, runs=2, clients=["C03"], limit=2, conditions=("harness",))
    assert len(llm.calls) == 2 * 2 and set(exp["results"]) == {("harness", 1), ("harness", 2)}
    assert set(cl.paired_preps(exp, 1)) == {"harness"}
    scores = cl.score_experiment(exp, scorer=_fake_scorer)
    lines, _ = cl.table(scores, runs=2)
    assert "no-h" not in lines[0] and "harn" in lines[0]
    with pytest.raises(ValueError):
        cl.run_experiment(data, llm, runs=1, clients=["C03"], limit=1, conditions=("nope",))
    args = cli.build_parser().parse_args(["cabinet-live", "--condition", "harness", "--code-owned", "disclosures"])
    assert (args.condition, args.code_owned) == ("harness", "disclosures")
    args = cli.build_parser().parse_args(["cabinet-live"])
    assert (args.condition, args.code_owned) == ("both", "disclosures,decision_maker")
