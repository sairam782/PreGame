"""Improver tests: what the view lets it read, the fake demo sequence, the live path, the tamper proposal.

versions / ledger / world.store are small fakes in sys.modules; mongomock holds the collections. No network.
"""
import ast
import copy
import hashlib
import json
import pathlib
import sys
import types
from datetime import datetime, timezone

import pytest

import pregame
from pregame import gate, improver
from pregame.contracts import BRIEF_SECTIONS, FACT_KINDS, RULES_CAP

FIELD = "insurance"
SIM = datetime(2026, 4, 1, tzinfo=timezone.utc)


def base_cfg():
    return {
        "field": FIELD,
        "policy": {"recency_days": 180, "max_facts": 6, "include_kinds": ["price", "competitor", "demand", "account"],
                   "section_order": list(BRIEF_SECTIONS), "likely_questions": 3, "prefer_exposed": False},
        "rules": [{"id": "lead-with-change", "text": "Open with the one change most likely to come up on this call."},
                  {"id": "cite-every-number", "text": "Every number must cite a fact."},
                  {"id": "plain-language", "text": "Short sentences, no jargon."}],
        "tools": {"market_feed": True, "account_notes": True, "analyst_notes": False},
        "guardrails": [{"id": "cite-facts", "text": "Cite facts.", "check": "cite-facts", "enabled": True}],
        "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1},
    }


def cfg_hash(cfg):
    return hashlib.sha256(json.dumps({k: cfg[k] for k in ("policy", "rules", "tools", "guardrails")},
                                     sort_keys=True).encode()).hexdigest()


class FakeLLM:
    is_fake = True


class ScriptedLLM:
    """A live-mode stand-in: returns scripted JSON replies and records every prompt."""
    is_fake = False

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete_json(self, role, system, prompt, max_tokens=2000):
        self.calls.append((role, system, prompt))
        return self.replies.pop(0)


@pytest.fixture
def state(monkeypatch, db):
    st = types.SimpleNamespace(cfg=base_cfg())

    versions = types.ModuleType("pregame.versions")
    versions.resolve_field_config = lambda _db, field: copy.deepcopy(st.cfg)
    versions.config_hash = cfg_hash

    ledger = types.ModuleType("pregame.ledger")

    def append(_db, kind, actor, payload, sim_time, session=None):
        seq = _db.ledger.count_documents({}) + 1
        entry = {"_id": seq, "seq": seq, "kind": kind, "actor": actor, "payload": payload, "sim_time": sim_time}
        _db.ledger.insert_one(entry)
        return entry

    ledger.append = append
    oracle = types.ModuleType("pregame.oracle")
    oracle.GUARDRAIL_CHECKS = {"cite-facts": None, "no-stale-facts": None, "no-advice": None}
    store = types.ModuleType("pregame.world.store")
    store.sim_now = lambda _db: SIM
    for name, mod in (("versions", versions), ("ledger", ledger), ("oracle", oracle)):
        monkeypatch.setitem(sys.modules, f"pregame.{name}", mod)
        monkeypatch.setattr(pregame, name, mod, raising=False)
    monkeypatch.setitem(sys.modules, "pregame.world.store", store)

    db.feedback.insert_one({"_id": "fb-ins-1", "field": FIELD, "brief_id": "b1", "event_id": "ins-reg",
                            "text": "The client asked about the regulator's new capital rule and the port strike "
                                    "that delayed their claims; the brief had nothing on either.", "sim_time": SIM})
    db.feedback.insert_one({"_id": "fb-log-1", "field": "logistics", "text": "Nothing about fuel.", "sim_time": SIM})
    return st


def file(db, p, status="rejected", decision="Rejected: test."):
    doc = dict(p, status=status, decision=decision)
    db.proposals.insert_one(doc)
    return doc


def refused(db):
    return [e for e in db.ledger.find({"kind": "refused"})]


# ---------------------------------------------------------------------------------------------------------------
# ImproverView
# ---------------------------------------------------------------------------------------------------------------
def test_view_refuses_eval_scenarios_and_other_collections(state, db):
    view = improver.ImproverView(db, sim_time=SIM)
    with pytest.raises(PermissionError):
        view["eval_scenarios"]
    with pytest.raises(PermissionError):
        view["ledger"]
    with pytest.raises(PermissionError):
        view.eval_scenarios
    with pytest.raises(PermissionError):
        view.db
    attempts = [e["payload"]["attempted"] for e in refused(db)]
    assert attempts == ["eval_scenarios", "ledger", "eval_scenarios", "db"]
    assert all(e["actor"] == "improver" for e in refused(db))


def test_view_refuses_heldout_rows(state, db):
    db.eval_runs.insert_many([
        {"_id": "t", "field": FIELD, "split": "tuning", "config_hash": "h", "summary": {"mean_accuracy": 0.6}},
        {"_id": "x", "field": FIELD, "split": "heldout", "config_hash": "h", "summary": {"mean_accuracy": 0.7}},
    ])
    view = improver.ImproverView(db, sim_time=SIM)
    with pytest.raises(PermissionError):
        view.eval_runs(FIELD, split="heldout")
    assert refused(db)[0]["payload"]["attempted"] == "eval_runs split='heldout'"
    assert [r["_id"] for r in view.tuning_results(FIELD)] == ["t"]
    assert [r["_id"] for r in view["eval_runs"](FIELD)] == ["t"]


def test_view_refuses_the_question_bank_and_heldout_runs_on_the_ledger(state, db):
    """HDY-31 (3): both yardstick reads raise PermissionError and each leaves a ledger `refused` receipt."""
    view = improver.ImproverView(db, sim_time=SIM)
    with pytest.raises(PermissionError, match="eval_scenarios"):
        view["eval_scenarios"]
    with pytest.raises(PermissionError, match="heldout"):
        view.eval_runs(FIELD, split="heldout")
    entries = refused(db)
    assert [e["payload"]["attempted"] for e in entries] == ["eval_scenarios", "eval_runs split='heldout'"]
    assert all(e["kind"] == "refused" and e["actor"] == "improver" and e["sim_time"] == SIM for e in entries)


def test_view_strips_heldout_summaries_and_hashes_from_proposals(state, db):
    db.proposals.insert_one({
        "_id": "prop-1", "field": FIELD, "kind": "policy", "status": "rejected", "tier": "G", "diff": ["max_facts"],
        "tuning": {"mean_accuracy": 0.7}, "heldout_candidate": {"mean_accuracy": 0.71},
        "heldout_champion": {"mean_accuracy": 0.68}, "approval_hash": "abc", "idem_key": "abc",
        "decision": "Rejected: held-out accuracy 0.71 vs champion 0.68 is inside the 0.05 margin.",
        "filed_by": "improver", "created_at": SIM})
    (p,) = improver.ImproverView(db).proposals(FIELD)
    assert not {"heldout_candidate", "heldout_champion", "approval_hash", "idem_key"} & set(p)
    assert p["tuning"] == {"mean_accuracy": 0.7} and p["status"] == "rejected" and p["tier"] == "G"
    assert p["decision"] == "Rejected: held-out accuracy # vs champion # is inside the # margin."


def test_view_reads_only_live_briefs_and_this_fields_feedback(state, db):
    db.briefs.insert_many([{"_id": "b-live", "field": FIELD, "config_label": "live", "as_of": SIM},
                           {"_id": "b-eval", "field": FIELD, "config_label": "champion", "as_of": SIM}])
    view = improver.ImproverView(db)
    assert [b["_id"] for b in view.briefs(FIELD)] == ["b-live"]
    assert [f["_id"] for f in view.feedback(FIELD)] == ["fb-ins-1"]


# ---------------------------------------------------------------------------------------------------------------
# fake improver: (a) broad -> (b) targeted -> (c) rule -> (d) tool -> nothing
# ---------------------------------------------------------------------------------------------------------------
def test_fake_improver_demo_order(state, db):
    view = improver.ImproverView(db, sim_time=SIM)
    llm = FakeLLM()

    a = improver.propose(view, FIELD, llm, SIM)
    assert a["kind"] == "policy" and a["body"]["max_facts"] == 30 and a["body"]["recency_days"] == 365
    assert a["body"]["include_kinds"] == list(FACT_KINDS)
    assert a["_id"].startswith("prop-") and a["status"] == "pending" and a["base_version"] == 1
    assert a["idem_key"] == gate.content_hash("policy", FIELD, 1, a["body"])
    assert gate.validate(a, state.cfg["policy"]) == [] and gate.classify(a, state.cfg["policy"]) == "G"
    file(db, a)

    b = improver.propose(view, FIELD, llm, SIM)
    assert b["kind"] == "policy" and b["body"]["max_facts"] == 8 and b["body"]["prefer_exposed"] is True
    assert {"regulation", "disruption"} <= set(b["body"]["include_kinds"])
    assert b["body"]["recency_days"] == 180 and "fb-ins-1" in b["evidence"]
    assert "include_kinds: + regulation, + disruption" in b["diff"] and "max_facts: 6 -> 8" in b["diff"]
    assert gate.validate(b, state.cfg["policy"]) == []
    file(db, b, status="committed", decision="Committed automatically (tier G): held-out accuracy 0.62 -> 0.84.")
    state.cfg["policy"], state.cfg["versions"]["policy"] = b["body"], 2

    c = improver.propose(view, FIELD, llm, SIM)
    assert c["kind"] == "rules" and c["base_version"] == 1
    ids = [r["id"] for r in c["body"]]
    assert "lead-with-budget" in ids and "lead-with-change" not in ids and len(ids) == 3   # replaced, not appended
    assert "rule lead-with-change: replaced by lead-with-budget" in c["diff"]
    assert gate.classify(c, state.cfg["rules"]) == "H" and gate.validate(c, state.cfg["rules"]) == []
    file(db, c, status="awaiting_owner")

    d = improver.propose(view, FIELD, llm, SIM)
    assert d["kind"] == "tools" and d["body"]["analyst_notes"] is True and d["diff"] == ["analyst_notes: off -> on"]
    assert gate.classify(d, state.cfg["tools"]) == "H"
    file(db, d)

    assert improver.propose(view, FIELD, llm, SIM) is None


def test_fake_rule_replaces_rather_than_growing_past_the_cap(state, db):
    state.cfg["rules"] = [{"id": f"r{i}", "text": "Keep it short."} for i in range(RULES_CAP)]
    for p in ({"kind": "policy", "body": {"max_facts": 30, "recency_days": 365}},
              {"kind": "policy", "body": {"max_facts": 10, "recency_days": 180}}):
        db.proposals.insert_one(dict(p, _id=p["body"]["max_facts"], field=FIELD, filed_by="improver",
                                     created_at=SIM))
    c = improver.propose(improver.ImproverView(db), FIELD, FakeLLM(), SIM)
    assert c["kind"] == "rules" and len(c["body"]) == RULES_CAP
    assert gate.validate(c, state.cfg["rules"]) == []


# ---------------------------------------------------------------------------------------------------------------
# live path (scripted LLM)
# ---------------------------------------------------------------------------------------------------------------
def test_live_improver_sees_tuning_failures_never_heldout_and_retries_invalid(state, db):
    chash = cfg_hash(state.cfg)
    db.eval_runs.insert_many([
        {"_id": "tun", "field": FIELD, "split": "tuning", "config_hash": chash, "config_label": "champion",
         "created_at": SIM, "summary": {"mean_accuracy": 0.6, "false_alarms": 0.0},
         "failures": [{"scenario_id": "insurance:tuning:1:m2", "question_id": "q3", "kind": "change",
                       "question": "What did the regulator change about capital rules?",
                       "reason": "missing key term '12%'"}]},
        {"_id": "held", "field": FIELD, "split": "heldout", "config_hash": chash, "config_label": "champion",
         "created_at": SIM, "summary": {"mean_accuracy": 0.7},
         "failures": [{"question": "SECRET-HELDOUT-QUESTION", "reason": "x"}]},
    ])
    db.eval_scenarios.insert_one({"_id": "insurance:heldout:1:m5", "field": FIELD, "split": "heldout",
                                  "questions": [{"id": "q9", "text": "SECRET-HELDOUT-QUESTION"}]})
    invalid = {"kind": "policy", "body": dict(state.cfg["policy"], max_facts=99), "diff": ["max_facts: 6 -> 99"],
               "rationale": "more", "evidence": []}
    good_body = dict(state.cfg["policy"], include_kinds=state.cfg["policy"]["include_kinds"] + ["regulation"])
    valid = {"kind": "policy", "body": good_body, "diff": ["whatever the model says"],
             "rationale": "Regulation facts were missing.", "evidence": ["fb-ins-1", "insurance:tuning:1:m2"]}
    llm = ScriptedLLM(invalid, valid)

    p = improver.propose(improver.ImproverView(db, sim_time=SIM), FIELD, llm, SIM)

    assert len(llm.calls) == 2 and all(role == "improver" for role, _, _ in llm.calls)
    first_prompt, retry_prompt = llm.calls[0][2], llm.calls[1][2]
    assert "What did the regulator change about capital rules?" in first_prompt
    assert "missing key term '12%'" in first_prompt and "fb-ins-1" in first_prompt
    assert "SECRET-HELDOUT-QUESTION" not in first_prompt + retry_prompt
    assert "max_facts" in retry_prompt.split("not a valid proposal")[1]
    assert "never propose changes to evaluation" in llm.calls[0][1].lower()
    assert p["kind"] == "policy" and p["body"] == good_body and p["base_version"] == 1
    assert p["diff"] == ["include_kinds: + regulation"]           # rendered by code, not by the model
    assert p["evidence"] == ["fb-ins-1", "insurance:tuning:1:m2"] and refused(db) == []


def test_live_improver_returns_none_on_a_reply_without_a_body(state, db):
    llm = ScriptedLLM({"thoughts": "hmm"}, {"still": "no"})
    assert improver.propose(improver.ImproverView(db), FIELD, llm, SIM) is None


# ---------------------------------------------------------------------------------------------------------------
# tamper
# ---------------------------------------------------------------------------------------------------------------
def test_tamper_proposal_targets_a_frozen_surface(state):
    p = improver.tamper_proposal(FIELD, SIM)
    assert p["kind"] == "scenarios" and p["key"] == "insurance:heldout" and p["base_version"] >= 1
    assert p["status"] == "pending" and p["filed_by"] == "improver"
    assert gate.classify(p, None) == "X" and gate.validate(p)


# ---------------------------------------------------------------------------------------------------------------
# HDY-31: the improver cannot reach its yardstick
# ---------------------------------------------------------------------------------------------------------------
ROOT = pathlib.Path(__file__).resolve().parents[1]
YARDSTICK_MODULES = {"pregame.oracle", "pregame.metrics"}
ALLOWED_GATE_HELPERS = {"validate", "content_hash"}             # pure; the improver passes known_checks to validate
ALLOWED_DYNAMIC = {"versions", "ledger", "world.store"}         # the only modules improver._mod() may load
FORBIDDEN_NAMES = {"evaluate_proposal", "evaluate_pending", "evaluate", "evaluate_grades", "tuning_baseline",
                   "approve", "reject", "file_proposal", "commit", "grade", "check_answer", "summarize", "compare",
                   "GUARDRAIL_CHECKS", "eval_scenarios"}


def _call_name(node: ast.Call) -> str:
    fn = node.func
    return fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""


def _pregame_imports(tree: ast.AST) -> set:
    """Every pregame module a source file imports, statically or through a constant _mod()/import_module() arg."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("pregame")}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("pregame"):
            found.add(node.module)
            for a in node.names:                                # `from pregame import ledger` imports a module
                sub = f"{node.module}.{a.name}"
                if (ROOT / (sub.replace(".", "/") + ".py")).exists() or (ROOT / sub.replace(".", "/")).is_dir():
                    found.add(sub)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            arg = node.args[0].value
            if _call_name(node) == "_mod" and isinstance(arg, str):
                found.add(f"pregame.{arg}")
            elif _call_name(node) == "import_module" and isinstance(arg, str) and arg.startswith("pregame"):
                found.add(arg)
    return found


def test_improver_source_never_references_the_yardstick():
    """HDY-31 (1): static check of pregame/improver.py."""
    tree = ast.parse((ROOT / "pregame" / "improver.py").read_text(encoding="utf-8"))
    imported = _pregame_imports(tree)
    assert not imported & YARDSTICK_MODULES, imported

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pregame.gate":
            assert {a.name for a in node.names} <= ALLOWED_GATE_HELPERS, ast.dump(node)
        if isinstance(node, ast.ImportFrom) and node.module == "pregame":
            assert not {a.name for a in node.names} & {"oracle", "metrics", "gate"}, ast.dump(node)
        if isinstance(node, ast.Import):
            assert not any(a.name in YARDSTICK_MODULES | {"pregame.gate"} for a in node.names), ast.dump(node)
        if isinstance(node, ast.Call) and _call_name(node) == "_mod":       # every dynamic load is allowed
            assert isinstance(node.args[0], ast.Constant) and node.args[0].value in ALLOWED_DYNAMIC, ast.dump(node)
        if isinstance(node, ast.Call) and _call_name(node) == "import_module":   # only inside _mod itself
            assert isinstance(node.args[0], ast.JoinedStr), ast.dump(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "eval_scenarios" not in node.value
            assert node.value not in {"oracle", "metrics", "pregame.oracle", "pregame.metrics"}
        if isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES | {"oracle", "metrics"}, node.id
    assert len([n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == "import_module"]) == 1


def test_modules_the_improver_loads_do_not_import_the_yardstick():
    """HDY-31 (1, transitive): follow every pregame import the improver can make, except the gate, whose two pure
    helpers are covered by the booby-trap test below."""
    start = _pregame_imports(ast.parse((ROOT / "pregame" / "improver.py").read_text(encoding="utf-8")))
    seen, todo = set(), sorted(start - {"pregame.gate"})
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        path = ROOT / (name.replace(".", "/") + ".py")
        if not path.exists():
            path = ROOT / name.replace(".", "/") / "__init__.py"
        if not path.exists():
            continue                                             # not written yet
        todo += sorted(_pregame_imports(ast.parse(path.read_text(encoding="utf-8"))) - seen)
    assert {"pregame.versions", "pregame.ledger"} <= seen
    assert not seen & (YARDSTICK_MODULES | {"pregame.gate"}), sorted(seen)


class _Tripwire(types.ModuleType):
    """A module that fails the test the moment anything reads from it."""

    def __init__(self, name, touched):
        super().__init__(name)
        self._touched = touched

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        self._touched.append(f"{self.__name__}.{attr}")
        raise AssertionError(f"the improver reached {self.__name__}.{attr}")


def test_improver_never_touches_the_booby_trapped_yardstick(state, db, monkeypatch):
    """HDY-31 (dynamic): with the oracle, metrics and the gate's evaluation functions booby-trapped, the whole fake
    sequence and a live proposal still run, and nothing was touched."""
    touched = []
    for name in YARDSTICK_MODULES:
        trap = _Tripwire(name, touched)
        monkeypatch.setitem(sys.modules, name, trap)
        monkeypatch.setattr(pregame, name.split(".")[1], trap, raising=False)

    def tripped(fn):
        def boom(*args, **kwargs):
            touched.append(f"gate.{fn}")
            raise AssertionError(f"the improver reached gate.{fn}")
        return boom

    for fn in ("evaluate_proposal", "evaluate_pending", "tuning_baseline", "approve", "reject", "file_proposal",
               "_summary", "_known_checks"):
        monkeypatch.setattr(gate, fn, tripped(fn))
    view = improver.ImproverView(db, sim_time=SIM)
    for _ in range(4):
        file(db, improver.propose(view, FIELD, FakeLLM(), SIM))
    narrower = {"kind": "tools", "body": dict(state.cfg["tools"], account_notes=False), "rationale": "narrower"}
    assert improver.propose(view, FIELD, ScriptedLLM(narrower), SIM)["kind"] == "tools"
    assert touched == []


def test_heldout_marker_never_reaches_the_live_improver(state, db):
    """HDY-31 (2): plant a held-out marker everywhere held-out data lives; the live improver's model must never see
    it, and neither may the proposal it returns."""
    marker = "HELDOUT-MARKER-7f3a"
    chash = cfg_hash(state.cfg)
    db.eval_scenarios.insert_one({"_id": "insurance:heldout:9:m5", "field": FIELD, "split": "heldout", "seed": 9,
                                  "month": 5, "questions": [{"id": "q1", "text": f"{marker}: what changed?",
                                                             "kind": "change", "key_terms": [marker]}]})
    db.eval_runs.insert_many([
        {"_id": "held", "field": FIELD, "split": "heldout", "config_hash": chash, "config_label": "champion",
         "created_at": SIM, "summary": {"mean_accuracy": 0.7, "per_scenario": {marker: 0.7}},
         "failures": [{"scenario_id": marker, "question": f"{marker}?", "reason": marker}]},
        {"_id": "tun", "field": FIELD, "split": "tuning", "config_hash": chash, "config_label": "champion",
         "created_at": SIM, "summary": {"mean_accuracy": 0.6},
         "failures": [{"scenario_id": "insurance:tuning:1:m2", "question": "What did the regulator change?",
                       "reason": "missing '12%'"}]},
    ])
    db.proposals.insert_one({"_id": "prop-old", "field": FIELD, "kind": "policy", "status": "rejected", "tier": "G",
                             "diff": ["max_facts: 6 -> 7"], "filed_by": "improver", "created_at": SIM,
                             "body": dict(state.cfg["policy"], max_facts=7),
                             "decision": "Rejected: held-out accuracy 0.63 vs champion 0.62 is inside the 0.05 "
                                         "margin.",
                             "heldout_candidate": {"per_scenario": {marker: 0.6}},
                             "heldout_champion": {"per_scenario": {marker: 0.6}}, "approval_hash": marker})
    db.briefs.insert_one({"_id": "eval-brief", "field": FIELD, "config_label": "candidate:prop-old", "as_of": SIM,
                          "markdown": f"Graded brief for {marker}"})

    captured = []

    class CapturingLLM:
        is_fake = False

        def complete_json(self, role, system, prompt, max_tokens=2000):
            captured.append(system + "\n" + prompt)
            return {"kind": "policy", "rationale": "Regulation facts were missing.", "evidence": ["fb-ins-1"],
                    "diff": [], "body": dict(state.cfg["policy"], include_kinds=["price", "regulation"])}

    p = improver.propose(improver.ImproverView(db, sim_time=SIM), FIELD, CapturingLLM(), SIM)
    assert len(captured) == 1 and p is not None and p["kind"] == "policy"
    assert "What did the regulator change?" in captured[0]          # tuning failures do get through
    assert "max_facts: 6 -> 7" in captured[0]                       # and so does the improver's own history
    assert all(marker not in text for text in captured)
    assert marker not in json.dumps(p, default=str)
    assert refused(db) == []                                        # the honest path needs no refusals
