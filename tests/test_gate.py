"""Gate tests: tiers, validation, the refusal, the decision paths, owner approval. mongomock + fakes, no network.

versions / oracle / metrics / ledger / config / world.store are replaced by small fakes in sys.modules, so these
tests pin the gate's own logic and do not depend on the other modules' state.
"""
import copy
import hashlib
import json
import sys
import types
from datetime import datetime, timezone

import pytest

import pregame
from pregame import gate
from pregame.contracts import BRIEF_SECTIONS, FACT_KINDS, WIN_MARGIN

FIELD = "insurance"
SIM = datetime(2026, 4, 1, tzinfo=timezone.utc)
CHECKS = ("cite-facts", "no-stale-facts", "no-advice")


def base_policy():
    return {"recency_days": 180, "max_facts": 6, "include_kinds": ["price", "competitor", "demand", "account"],
            "section_order": list(BRIEF_SECTIONS), "likely_questions": 3, "prefer_exposed": False}


def base_rules():
    return [{"id": "lead-with-change", "text": "Open with the one change most likely to come up on this call."},
            {"id": "cite-every-number", "text": "Every number must cite a fact."}]


def base_tools():
    return {"market_feed": True, "account_notes": True, "analyst_notes": False}


def base_guardrails():
    return [{"id": "cite-facts", "text": "Cite facts.", "check": "cite-facts", "enabled": True},
            {"id": "no-advice", "text": "No advice.", "check": "no-advice", "enabled": True},
            {"id": "no-stale-facts", "text": "No stale facts.", "check": "no-stale-facts", "enabled": False}]


class FakeLLM:
    is_fake = True


class Harness:
    """State behind the fake modules, plus call records."""

    def __init__(self):
        self.cfg = {"field": FIELD, "policy": base_policy(), "rules": base_rules(), "tools": base_tools(),
                    "guardrails": base_guardrails(),
                    "versions": {"policy": 1, "rules": 1, "tools": 1, "guardrails": 1}}
        self.commits = []
        self.evals = []
        # accuracy / tokens per (role, split); role is "champion" or "candidate"
        self.acc = {("champion", "tuning"): 0.60, ("candidate", "tuning"): 0.70,
                    ("champion", "heldout"): 0.62, ("candidate", "heldout"): 0.84}
        self.worst = {"champion": 0.50, "candidate": 0.71}
        self.tokens = {"champion": 400.0, "candidate": 440.0}
        self.commit_error = None        # set to make the next versions.commit raise


def _summary(label, split, field, k, acc, worst, tokens, scenarios):
    return {"config_label": label, "split": split, "field": field, "k": k, "n_scenarios": len(scenarios),
            "mean_accuracy": acc, "worst_accuracy": worst, "pass_k": 0.5, "missed_changes": 1.0,
            "false_alarms": 0.0, "stale_claims": 0.0, "context_tokens": tokens,
            "per_scenario": {s["_id"]: acc for s in scenarios}}


@pytest.fixture
def h(monkeypatch, db):
    w = Harness()

    class StaleVersion(Exception):
        pass

    versions = types.ModuleType("pregame.versions")
    versions.StaleVersion = StaleVersion
    versions.resolve_field_config = lambda _db, field: copy.deepcopy(w.cfg)
    versions.head = lambda _db, kind, key: w.cfg["versions"][kind]
    versions.with_change = lambda cfg, kind, body: {**copy.deepcopy(cfg), kind: copy.deepcopy(body)}
    versions.config_hash = lambda cfg: hashlib.sha256(json.dumps(
        {k: cfg[k] for k in ("policy", "rules", "tools", "guardrails")}, sort_keys=True).encode()).hexdigest()

    def commit(_db, kind, key, base_version, body, *, rationale, sim_time, proposal_id=None, approval_hash=None,
               approved_by="gate", restores=None):
        if w.commit_error is not None:
            raise w.commit_error
        if w.cfg["versions"][kind] != base_version:
            raise StaleVersion(f"{kind}:{key} is not at v{base_version}")
        n = base_version + 1
        w.cfg[kind] = copy.deepcopy(body)
        w.cfg["versions"][kind] = n
        cv = {"_id": f"{kind}:{key}@v{n}", "kind": kind, "key": key, "version": n, "body": body,
              "rationale": rationale, "proposal_id": proposal_id, "approval_hash": approval_hash,
              "approved_by": approved_by, "supersedes": base_version, "restores": restores}
        w.commits.append(cv)
        if proposal_id:
            _db.proposals.update_one({"_id": proposal_id, "status": {"$in": ["evaluating", "awaiting_owner"]}},
                                     {"$set": {"status": "committed"}})
        append(_db, "rollback" if restores else "commit", approved_by, {"proposal_id": proposal_id}, sim_time)
        return cv

    versions.commit = commit

    oracle = types.ModuleType("pregame.oracle")
    oracle.GUARDRAIL_CHECKS = {name: (lambda brief, ctx: []) for name in CHECKS}

    def evaluate(cfg, scenarios, llm, k=2, config_label="champion"):
        role = "champion" if config_label == "champion" else "candidate"
        split = scenarios[0]["split"]
        w.evals.append((role, split, k))
        return _summary(config_label, split, cfg["field"], k, w.acc[(role, split)], w.worst[role],
                        w.tokens[role], scenarios)

    oracle.evaluate = evaluate

    metrics = types.ModuleType("pregame.metrics")

    def compare(cand, champ):
        reasons = []
        if cand["mean_accuracy"] + 1e-9 < champ["mean_accuracy"] + WIN_MARGIN:
            reasons.append("mean accuracy short of the margin")
        if cand["worst_accuracy"] + 1e-9 < champ["worst_accuracy"]:
            reasons.append("worst scenario fell")
        if cand["context_tokens"] > champ["context_tokens"] * 1.5:
            reasons.append("context grew too much")
        return {"win": not reasons, "delta_accuracy": cand["mean_accuracy"] - champ["mean_accuracy"],
                "reasons": reasons}

    metrics.compare = compare

    ledger = types.ModuleType("pregame.ledger")

    def append(_db, kind, actor, payload, sim_time, session=None):
        seq = _db.ledger.count_documents({}) + 1
        entry = {"_id": seq, "seq": seq, "kind": kind, "actor": actor, "payload": payload, "sim_time": sim_time}
        _db.ledger.insert_one(entry)
        return entry

    ledger.append = append

    config = types.ModuleType("pregame.config")
    config.settings = lambda: types.SimpleNamespace(k=2)
    store = types.ModuleType("pregame.world.store")
    store.sim_now = lambda _db: SIM

    for name, mod in (("versions", versions), ("oracle", oracle), ("metrics", metrics), ("ledger", ledger),
                      ("config", config)):
        monkeypatch.setitem(sys.modules, f"pregame.{name}", mod)
        monkeypatch.setattr(pregame, name, mod, raising=False)
    monkeypatch.setitem(sys.modules, "pregame.world.store", store)

    for split, months in (("tuning", (1, 2, 3)), ("heldout", (4, 5, 6))):
        for m in months:
            db.eval_scenarios.insert_one({
                "_id": f"{FIELD}:{split}:1:m{m}", "field": FIELD, "split": split, "seed": 1, "month": m,
                "questions": [{"id": f"q{m}", "text": f"What changed for us in month {m}?", "kind": "change"}]})
    w.StaleVersion = StaleVersion
    return w


def proposal(kind, body, *, key=None, base=1, field=FIELD):
    return {"field": field, "kind": kind, "key": key or ("global" if kind == "guardrails" else field),
            "base_version": base, "body": body, "diff": [f"{kind} change"], "rationale": "test",
            "evidence": [], "filed_by": "improver", "created_sim": SIM}


def ledger_kinds(db):
    return [e["kind"] for e in db.ledger.find().sort("seq", 1)]


def expected_hash(kind, key, base, body):
    return hashlib.sha256(json.dumps([kind, key, base, body], sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# classify: every row of the tier table
# ---------------------------------------------------------------------------------------------------------------
def _guardrails(**changes):
    g = base_guardrails()
    if changes.get("add"):
        g.append({"id": "cite-twice", "text": "Cite twice.", "check": "cite-facts", "enabled": True})
    if changes.get("enable"):
        g[2]["enabled"] = True
    if changes.get("remove"):
        g = g[1:]
    if changes.get("disable"):
        g[0]["enabled"] = False
    if changes.get("reword"):
        g[0]["text"] = "Cite facts when convenient."
    return g


@pytest.mark.parametrize("kind, body, current, tier", [
    ("policy", dict(base_policy(), max_facts=10), base_policy(), "G"),
    ("tools", dict(base_tools(), account_notes=False), base_tools(), "G"),
    ("tools", dict(base_tools(), analyst_notes=True), base_tools(), "H"),
    ("tools", {"market_feed": False, "account_notes": True, "analyst_notes": True}, base_tools(), "H"),
    ("rules", base_rules()[:1], base_rules(), "H"),
    ("guardrails", _guardrails(add=True), base_guardrails(), "G"),
    ("guardrails", _guardrails(enable=True), base_guardrails(), "G"),
    ("guardrails", _guardrails(remove=True), base_guardrails(), "H"),
    ("guardrails", _guardrails(disable=True), base_guardrails(), "H"),
    ("guardrails", _guardrails(reword=True), base_guardrails(), "H"),
    ("guardrails", _guardrails(add=True, disable=True), base_guardrails(), "H"),
    ("scenarios", {"rewrite": True}, None, "X"),
    ("oracle", {}, None, "X"),
    ("ledger", {}, None, "X"),
    ("metrics", {}, None, "X"),
    ("gate", {}, None, "X"),
])
def test_classify_table(kind, body, current, tier):
    assert gate.classify(proposal(kind, body), current) == tier


# ---------------------------------------------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind, body", [
    ("policy", dict(base_policy(), max_facts=12, include_kinds=["price", "regulation"])),
    ("rules", base_rules() + [{"id": "lead-with-budget", "text": "Lead with the budget."}]),
    ("tools", dict(base_tools(), analyst_notes=True)),
    ("guardrails", _guardrails(add=True)),
])
def test_validate_accepts_good_bodies(h, kind, body):
    assert gate.validate(proposal(kind, body)) == []


@pytest.mark.parametrize("kind, body, needle", [
    ("policy", dict(base_policy(), max_facts=31), "max_facts"),
    ("policy", dict(base_policy(), max_facts=2), "max_facts"),
    ("policy", dict(base_policy(), recency_days=400), "recency_days"),
    ("policy", dict(base_policy(), likely_questions=1), "likely_questions"),
    ("policy", dict(base_policy(), max_facts=True), "max_facts"),
    ("policy", dict(base_policy(), include_kinds=[]), "include_kinds"),
    ("policy", dict(base_policy(), include_kinds=["price", "gossip"]), "gossip"),
    ("policy", dict(base_policy(), section_order=list(BRIEF_SECTIONS)[:-1]), "section_order"),
    ("policy", dict(base_policy(), section_order=list(BRIEF_SECTIONS)[:-1] + ["what_changed"]), "section_order"),
    ("policy", dict(base_policy(), temperature=0.2), "unknown knobs"),
    ("rules", [{"id": f"r{i}", "text": "x"} for i in range(9)], "cap"),
    ("rules", [{"id": "long", "text": "x" * 301}], "301"),
    ("rules", [{"id": "same", "text": "a"}, {"id": "same", "text": "b"}], "duplicate"),
    ("tools", {"market_feed": True, "account_notes": True}, "exactly"),
    ("tools", dict(base_tools(), web_search=True), "exactly"),
    ("guardrails", base_guardrails() + [{"id": "vibes", "text": "t", "check": "vibe-check", "enabled": True}],
     "vibe-check"),
])
def test_validate_rejects_out_of_bounds(h, kind, body, needle):
    errors = gate.validate(proposal(kind, body))
    assert errors and any(needle in e for e in errors), errors


def test_validate_requires_a_real_change_and_right_key(h):
    assert any("same as the current" in e for e in gate.validate(proposal("policy", base_policy()), base_policy()))
    assert any("key" in e for e in gate.validate(proposal("guardrails", base_guardrails(), key=FIELD)))
    assert gate.validate(proposal("scenarios", {}))[0].startswith("unknown kind")


def test_validate_falls_back_to_builtin_checks_without_oracle(monkeypatch):
    monkeypatch.setitem(sys.modules, "pregame.oracle", None)       # import pregame.oracle -> ImportError
    ok = proposal("guardrails", _guardrails(add=True))
    assert gate.validate(ok) == []
    bad = proposal("guardrails", base_guardrails() + [{"id": "x", "text": "t", "check": "nope", "enabled": True}])
    assert gate.validate(bad)


# ---------------------------------------------------------------------------------------------------------------
# filing and the X refusal
# ---------------------------------------------------------------------------------------------------------------
def test_file_proposal_is_idempotent_and_starts_pending(h, db):
    p = proposal("policy", dict(base_policy(), max_facts=10))
    p["status"], p["approval_hash"] = "committed", "forged"
    first = gate.file_proposal(db, p)
    again = gate.file_proposal(db, dict(p, _id="prop-other"))
    assert first["_id"] == again["_id"] and db.proposals.count_documents({}) == 1
    assert first["status"] == "pending" and first["approval_hash"] is None
    assert first["idem_key"] == expected_hash("policy", FIELD, 1, p["body"])
    assert ledger_kinds(db) == ["proposal"]


def test_frozen_surface_is_refused_with_a_ledger_entry(h, db):
    from pregame.improver import tamper_proposal

    filed = gate.file_proposal(db, tamper_proposal(FIELD, SIM))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "rejected" and out["tier"] == "X"
    assert out["decision"] == "Refused: scenarios is frozen; the harness may not change its own yardstick."
    refused = db.ledger.find_one({"kind": "refused"})
    assert refused["actor"] == "gate" and refused["payload"]["proposal_id"] == filed["_id"]
    assert h.evals == [] and h.commits == []


# ---------------------------------------------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------------------------------------------
def test_worse_on_tuning_is_rejected_before_heldout(h, db):
    h.acc[("candidate", "tuning")] = 0.50
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "rejected"
    assert out["decision"].startswith("Rejected: did not improve on tuning (accuracy 0.50 vs champion 0.60)")
    assert all(split == "tuning" for _, split, _ in h.evals)
    assert out["tuning"]["mean_accuracy"] == 0.50 and out["heldout_candidate"] is None
    assert h.commits == [] and ledger_kinds(db) == ["proposal", "eval", "reject"]


def test_heldout_loss_inside_margin_is_rejected(h, db):
    h.acc[("candidate", "heldout")] = 0.65
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "rejected" and out["tier"] == "G"
    assert out["decision"] == "Rejected: held-out accuracy 0.65 vs champion 0.62 is inside the 0.05 margin."
    assert out["heldout_candidate"]["mean_accuracy"] == 0.65 and out["heldout_champion"]["mean_accuracy"] == 0.62
    assert out["heldout_candidate"]["k"] == 2 and out["tuning"]["k"] == 1
    assert h.commits == []


def test_bloated_context_loses_even_with_higher_accuracy(h, db):
    h.tokens["candidate"] = 1600.0
    body = dict(base_policy(), max_facts=30, include_kinds=list(FACT_KINDS), recency_days=365)
    out = gate.evaluate_proposal(db, gate.file_proposal(db, proposal("policy", body))["_id"], FakeLLM(), k=2)
    assert out["status"] == "rejected"
    assert out["decision"] == ("Rejected: held-out accuracy 0.62 -> 0.84, but context grew 400 -> 1600 tokens "
                               "(+300%, over the +50% allowed).")


def test_g_win_commits_automatically(h, db):
    body = dict(base_policy(), max_facts=10, include_kinds=["price", "competitor", "demand", "account", "regulation"])
    filed = gate.file_proposal(db, proposal("policy", body))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "committed" and out["tier"] == "G"
    assert out["decision"] == ("Committed automatically (tier G): held-out accuracy 0.62 -> 0.84, "
                               "worst scenario 0.50 -> 0.71.")
    assert len(h.commits) == 1 and h.commits[0]["approved_by"] == "gate"
    assert h.commits[0]["proposal_id"] == filed["_id"] and h.commits[0]["body"] == body
    assert ledger_kinds(db) == ["proposal", "commit", "eval"]   # the outcome is logged only after the commit
    assert db.ledger.find_one({"kind": "eval"})["payload"]["outcome"] == "committed"


def _eval_outcomes(db):
    return [e["payload"]["outcome"] for e in db.ledger.find({"kind": "eval"}).sort("seq", 1)]


def test_g_win_that_loses_the_commit_race_is_logged_stale_not_committed(h, db):
    h.commit_error = h.StaleVersion("policy:insurance is not at base_version 1")
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "stale" and out["decision"].startswith("Stale: it won on held-out data")
    assert ledger_kinds(db) == ["proposal", "eval", "reject"] and _eval_outcomes(db) == ["stale"]
    assert db.ledger.find_one({"kind": "reject"})["payload"]["status"] == "stale"
    assert h.commits == []


def test_g_win_whose_commit_fails_logs_nothing_claiming_success(h, db):
    h.commit_error = RuntimeError("the database went away mid-commit")
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    with pytest.raises(RuntimeError, match="went away"):
        gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    after = db.proposals.find_one({"_id": filed["_id"]})
    assert after["status"] == "pending" and after["decision"] is None and after["heldout_candidate"] is None
    assert ledger_kinds(db) == ["proposal"] and h.commits == []
    h.commit_error = None                                        # the retry then commits, and only then logs it
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "committed" and ledger_kinds(db) == ["proposal", "commit", "eval"]


def test_h_win_waits_for_the_owner_with_an_approval_hash(h, db):
    body = [{"id": "lead-with-budget", "text": "Lead with the budget change and its number."}, base_rules()[1]]
    filed = gate.file_proposal(db, proposal("rules", body))
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "awaiting_owner" and out["tier"] == "H"
    assert out["approval_hash"] == expected_hash("rules", FIELD, 1, body)
    assert out["decision"].startswith("Awaiting owner approval (tier H): held-out accuracy 0.62 -> 0.84")
    assert h.commits == []


def test_champion_results_are_cached_across_proposals(h, db):
    for n in (10, 11):
        filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=n)))
        gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
        h.cfg["policy"], h.cfg["versions"]["policy"] = base_policy(), 1   # undo the commit: same champion again
    champion_runs = [e for e in h.evals if e[0] == "champion"]
    assert sorted(champion_runs) == [("champion", "heldout", 2), ("champion", "tuning", 1)]
    assert db.eval_runs.count_documents({"split": "tuning"}) == 3            # champion + two candidates


def test_invalid_and_stale_proposals_do_not_run_the_oracle(h, db):
    bad = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=99)))
    out = gate.evaluate_proposal(db, bad["_id"], FakeLLM(), k=2)
    assert out["status"] == "rejected" and out["decision"].startswith("Rejected: invalid proposal: max_facts")
    h.cfg["versions"]["tools"] = 2
    old = gate.file_proposal(db, proposal("tools", dict(base_tools(), account_notes=False)))
    out = gate.evaluate_proposal(db, old["_id"], FakeLLM(), k=2)
    assert out["status"] == "stale" and "moved from v1 to v2" in out["decision"]
    assert h.evals == []


def test_missing_scenarios_fail_loudly_and_hand_the_proposal_back(h, db):
    db.eval_scenarios.delete_many({"split": "heldout"})
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    with pytest.raises(RuntimeError, match="no heldout scenarios"):
        gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert db.proposals.find_one({"_id": filed["_id"]})["status"] == "pending"


def test_evaluate_leaves_a_proposal_someone_else_moved(h, db):
    filed = gate.file_proposal(db, proposal("policy", dict(base_policy(), max_facts=10)))
    db.proposals.update_one({"_id": filed["_id"]}, {"$set": {"status": "evaluating"}})
    out = gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)
    assert out["status"] == "evaluating" and h.evals == []


# ---------------------------------------------------------------------------------------------------------------
# owner approval
# ---------------------------------------------------------------------------------------------------------------
def _awaiting(h, db):
    body = dict(base_tools(), analyst_notes=True)
    filed = gate.file_proposal(db, proposal("tools", body))
    return gate.evaluate_proposal(db, filed["_id"], FakeLLM(), k=2)


def test_approve_with_the_wrong_hash_is_refused(h, db):
    p = _awaiting(h, db)
    with pytest.raises(PermissionError):
        gate.approve(db, p["_id"], "0" * 64, "alex", SIM)
    assert h.commits == [] and db.proposals.find_one({"_id": p["_id"]})["status"] == "awaiting_owner"
    assert db.ledger.find_one({"kind": "refused"})["actor"] == "owner:alex"


def test_approve_refuses_a_proposal_edited_after_the_gate(h, db):
    p = _awaiting(h, db)
    db.proposals.update_one({"_id": p["_id"]}, {"$set": {"body.market_feed": False}})
    with pytest.raises(PermissionError):
        gate.approve(db, p["_id"], p["approval_hash"], "alex", SIM)
    assert h.commits == []


def test_approve_with_the_right_hash_commits_as_owner(h, db):
    p = _awaiting(h, db)
    cv = gate.approve(db, p["_id"], p["approval_hash"], "alex", SIM)
    assert cv["_id"] == "tools:insurance@v2" and cv["approved_by"] == "owner:alex"
    assert cv["approval_hash"] == p["approval_hash"]
    after = db.proposals.find_one({"_id": p["_id"]})
    assert after["status"] == "committed" and "signed by alex as tools:insurance@v2" in after["decision"]
    assert ledger_kinds(db)[-1] == "approve"


def test_approve_after_the_head_moved_marks_stale(h, db):
    p = _awaiting(h, db)
    h.cfg["versions"]["tools"] = 2
    with pytest.raises(h.StaleVersion):
        gate.approve(db, p["_id"], p["approval_hash"], "alex", SIM)
    after = db.proposals.find_one({"_id": p["_id"]})
    assert after["status"] == "stale" and h.commits == []


def test_owner_reject(h, db):
    p = _awaiting(h, db)
    out = gate.reject(db, p["_id"], "alex", "analyst notes are too noisy", SIM)
    assert out["status"] == "rejected" and out["decision"] == "Rejected by owner alex: analyst notes are too noisy."
    with pytest.raises(ValueError):
        gate.approve(db, p["_id"], p["approval_hash"], "alex", SIM)
