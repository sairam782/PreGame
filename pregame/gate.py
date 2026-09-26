"""The gate: tier a proposed harness change, validate it, measure it on held-out data, decide, commit.

This runs in the harness's trusted process. The improver only files proposals; it never evaluates, approves or
commits. The decision is code: the frozen oracle scores candidate vs champion, `metrics.compare` says who won, and
the tier (G / H / X, see DESIGN.md) says whether a win commits by itself or waits for a person to sign its hash.

The proposal's `status` field is the queue: pending -> evaluating -> rejected | awaiting_owner | committed | stale.
Every status move is filtered on the status we expect, so zero matches means someone else moved it first.

Other modules (versions, oracle, metrics, ledger, config, world.store) are imported lazily through `_mod` so tests
can swap them and so this module imports cleanly while they are being written.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from pregame.contracts import (
    BRIEF_SECTIONS,
    FACT_KINDS,
    FIELDS,
    POLICY_BOUNDS,
    RULES_CAP,
    SOURCES,
    WIN_MARGIN,
    EvalSummary,
)

CONFIG_KINDS = ("policy", "rules", "tools", "guardrails")
POLICY_KEYS = ("recency_days", "max_facts", "include_kinds", "section_order", "likely_questions", "prefer_exposed")
RULE_KEYS = ("id", "text")
GUARDRAIL_KEYS = ("id", "text", "check", "enabled")
RULE_TEXT_MAX = 300
DEFAULT_GUARDRAIL_CHECKS = frozenset({"cite-facts", "no-stale-facts", "no-advice"})
CONTEXT_GROWTH_LIMIT = 0.5          # mirrors metrics: context may grow at most +50% (used only to explain a loss)
TUNING_K = 1                        # the tuning screen is one run per scenario
MAX_FAILURES = 40                   # per-question tuning failures kept for the improver
FROZEN_DECISION = "Refused: {kind} is frozen; the harness may not change its own yardstick."
_EPS = 1e-9
_SUMMARY_KEYS = tuple(EvalSummary.__annotations__)


# ---------------------------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------------------------
def _mod(name: str):
    return importlib.import_module(f"pregame.{name}")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def content_hash(kind: str, key: str, base_version: int, body: Any) -> str:
    """sha256 of the canonical JSON of (kind, key, base_version, body).

    Used as the proposal's idem_key and as its approval_hash: the owner signs exactly this content.
    """
    return _sha([kind, key, base_version, body])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sim_now(db, fallback: Optional[datetime] = None) -> datetime:
    """The market clock, for ledger entries and commits. A missing clock is not worth failing a decision over."""
    try:
        return _mod("world.store").sim_now(db)
    except Exception:
        return fallback if fallback is not None else _now()


def _default_k() -> int:
    try:
        cfg_module = _mod("config")
    except ImportError:
        return 2
    return int(cfg_module.settings().k)


def _ledger(db, kind: str, actor: str, payload: dict, sim_time: datetime) -> dict:
    return _mod("ledger").append(db, kind, actor, payload, sim_time)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------------------------------------------
# classify (pure)
# ---------------------------------------------------------------------------------------------------------------
def classify(proposal: dict, current_body: Any) -> str:
    """Tier of a proposed change, per the table in DESIGN.md.

    policy -> G. rules -> H. tools: any source switched on -> H, otherwise (switching off) -> G.
    guardrails: only additions / enablings -> G; any removal, disabling or rewrite -> H.
    Anything else (scenarios, oracle, ledger, metrics, gate, ...) -> X.
    """
    kind = proposal.get("kind")
    body = proposal.get("body")
    if kind == "policy":
        return "G"
    if kind == "rules":
        return "H"
    if kind == "tools":
        old = current_body if isinstance(current_body, dict) else {}
        new = body if isinstance(body, dict) else {}
        switched_on = any(bool(new.get(s)) and not bool(old.get(s)) for s in set(old) | set(new))
        return "H" if switched_on else "G"
    if kind == "guardrails":
        return "G" if _guardrails_only_tighten(current_body, body) else "H"
    return "X"


def _guardrails_only_tighten(old: Any, new: Any) -> bool:
    """True when every change adds a guardrail or enables one. Unknown shapes count as loosening."""
    if not isinstance(old, list) or not isinstance(new, list):
        return False
    if not all(isinstance(g, dict) for g in old + new):
        return False
    old_by = {g.get("id"): g for g in old}
    new_by = {g.get("id"): g for g in new}
    if len(new_by) != len(new):
        return False                                    # duplicate ids: can't reason about it
    for gid, og in old_by.items():
        ng = new_by.get(gid)
        if ng is None:
            return False                                # removed
        if og.get("enabled") and not ng.get("enabled"):
            return False                                # disabled
        if og.get("check") != ng.get("check") or og.get("text") != ng.get("text"):
            return False                                # rewritten: we can't prove that tightens
    return True


def _owner_reason(kind: str, current_body: Any, body: Any) -> str:
    if kind == "rules":
        return "Drafting rules change what the bot tells people, so a person signs them by hash."
    if kind == "tools":
        on = [s for s in SOURCES if isinstance(body, dict) and body.get(s)
              and not (isinstance(current_body, dict) and current_body.get(s))]
        return f"It switches {', '.join(on) or 'a source'} on, so a person signs new tool access by hash."
    return "It removes, disables or rewrites a guardrail, so a person signs the loosening by hash."


# ---------------------------------------------------------------------------------------------------------------
# validate (pure)
# ---------------------------------------------------------------------------------------------------------------
def validate(proposal: dict, current_body: Any = None, known_checks: Optional[set] = None) -> list[str]:
    """Problems with a proposal, in plain words; [] means valid.

    `current_body` (optional) is the head's body for kind:key; when given, a body that changes nothing is invalid.
    `known_checks` (optional) is the set of guardrail check names; when None it comes from oracle.GUARDRAIL_CHECKS
    (or the three built-in checks if the oracle is not importable).
    """
    kind = proposal.get("kind")
    if kind not in CONFIG_KINDS:
        return [f"unknown kind {kind!r}; the harness may change only {', '.join(CONFIG_KINDS)}"]
    errors: list[str] = []
    field = proposal.get("field")
    if field not in FIELDS:
        errors.append(f"unknown field {field!r}")
    expected_key = "global" if kind == "guardrails" else field
    if proposal.get("key") != expected_key:
        errors.append(f"key for {kind} must be {expected_key!r}, got {proposal.get('key')!r}")
    base = proposal.get("base_version")
    if not _is_int(base) or base < 1:
        errors.append(f"base_version must be a positive integer, got {base!r}")

    body = proposal.get("body")
    if kind == "policy":
        errors += _policy_errors(body)
    elif kind == "rules":
        errors += _rules_errors(body)
    elif kind == "tools":
        errors += _tools_errors(body)
    else:
        errors += _guardrails_errors(body, known_checks if known_checks is not None else _known_checks())

    if current_body is not None and canonical_json(body) == canonical_json(current_body):
        errors.append(f"the proposed {kind} is the same as the current version; nothing would change")
    return errors


def _known_checks() -> frozenset:
    try:
        return frozenset(getattr(_mod("oracle"), "GUARDRAIL_CHECKS"))
    except (ImportError, AttributeError):
        return DEFAULT_GUARDRAIL_CHECKS


def _policy_errors(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return ["policy body must be an object of knobs"]
    errs = []
    missing = [k for k in POLICY_KEYS if k not in body]
    extra = sorted(str(k) for k in body if k not in POLICY_KEYS)
    if missing:
        errs.append(f"policy is missing {', '.join(missing)}")
    if extra:
        errs.append(f"policy has unknown knobs {', '.join(extra)}")
    for knob, (lo, hi) in POLICY_BOUNDS.items():
        if knob in body and not (_is_int(body[knob]) and lo <= body[knob] <= hi):
            errs.append(f"{knob} must be a whole number from {lo} to {hi}, got {body[knob]!r}")
    if "include_kinds" in body:
        kinds = body["include_kinds"]
        if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
            errs.append("include_kinds must be a non-empty list of fact kinds")
        else:
            unknown = [k for k in kinds if k not in FACT_KINDS]
            if unknown:
                errs.append(f"include_kinds has unknown kinds {', '.join(unknown)} (known: {', '.join(FACT_KINDS)})")
            if len(set(kinds)) != len(kinds):
                errs.append("include_kinds lists a kind twice")
    if "section_order" in body:
        order = body["section_order"]
        if not (isinstance(order, list) and all(isinstance(s, str) for s in order)
                and len(order) == len(BRIEF_SECTIONS) and set(order) == set(BRIEF_SECTIONS)):
            errs.append(f"section_order must be a permutation of {', '.join(BRIEF_SECTIONS)}")
    if "prefer_exposed" in body and not isinstance(body["prefer_exposed"], bool):
        errs.append("prefer_exposed must be true or false")
    return errs


def _rules_errors(body: Any) -> list[str]:
    if not isinstance(body, list):
        return ["rules body must be a list of {id, text}"]
    errs = []
    if len(body) > RULES_CAP:
        errs.append(f"{len(body)} rules is over the cap of {RULES_CAP}; replace a rule instead of appending")
    ids: list[str] = []
    for i, rule in enumerate(body, 1):
        if not (isinstance(rule, dict) and isinstance(rule.get("id"), str) and rule["id"].strip()
                and isinstance(rule.get("text"), str) and rule["text"].strip()):
            errs.append(f"rule {i} needs a non-empty id and text")
            continue
        extra = sorted(str(k) for k in rule if k not in RULE_KEYS)
        if extra:
            errs.append(f"rule {rule['id']} has unknown keys {', '.join(extra)}")
        if len(rule["text"]) > RULE_TEXT_MAX:
            errs.append(f"rule {rule['id']} is {len(rule['text'])} characters; the limit is {RULE_TEXT_MAX}")
        ids.append(rule["id"])
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        errs.append(f"duplicate rule ids: {', '.join(dupes)}")
    return errs


def _tools_errors(body: Any) -> list[str]:
    if not isinstance(body, dict):
        return ["tools body must be an object of source switches"]
    errs = []
    if set(body) != set(SOURCES):
        errs.append(f"tools must switch exactly these sources: {', '.join(SOURCES)}")
    bad = sorted(str(k) for k, v in body.items() if not isinstance(v, bool))
    if bad:
        errs.append(f"tool switches must be true or false: {', '.join(bad)}")
    return errs


def _guardrails_errors(body: Any, checks) -> list[str]:
    if not isinstance(body, list):
        return ["guardrails body must be a list of {id, text, check, enabled}"]
    errs = []
    ids: list[str] = []
    for i, g in enumerate(body, 1):
        if not (isinstance(g, dict) and isinstance(g.get("id"), str) and g["id"].strip()
                and isinstance(g.get("text"), str) and isinstance(g.get("check"), str)
                and isinstance(g.get("enabled"), bool)):
            errs.append(f"guardrail {i} needs id, text, check and enabled (true/false)")
            continue
        extra = sorted(str(k) for k in g if k not in GUARDRAIL_KEYS)
        if extra:
            errs.append(f"guardrail {g['id']} has unknown keys {', '.join(extra)}")
        if g["check"] not in checks:
            errs.append(f"guardrail {g['id']} names unknown check {g['check']!r} (known: {', '.join(sorted(checks))})")
        ids.append(g["id"])
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        errs.append(f"duplicate guardrail ids: {', '.join(dupes)}")
    return errs


# ---------------------------------------------------------------------------------------------------------------
# The queue: filing and status moves
# ---------------------------------------------------------------------------------------------------------------
def _get(db, proposal_id: str) -> dict:
    doc = db.proposals.find_one({"_id": proposal_id})
    if doc is None:
        raise KeyError(f"no proposal {proposal_id!r}")
    return doc


def _move(db, proposal_id: str, expected: str, fields: dict) -> bool:
    """Update a proposal only if it is still at `expected` status. False = someone else moved it."""
    res = db.proposals.update_one({"_id": proposal_id, "status": expected}, {"$set": fields})
    return res.matched_count == 1


def file_proposal(db, proposal: dict) -> dict:
    """Insert a proposal (idempotent on idem_key) and append a ledger `proposal` entry.

    Filing always starts the proposal at `pending` with no tier, decision, summaries or approval hash: those are
    the gate's to set, whatever the caller passed in.
    """
    doc = dict(proposal)
    doc["idem_key"] = content_hash(doc["kind"], doc["key"], doc["base_version"], doc["body"])
    existing = db.proposals.find_one({"idem_key": doc["idem_key"]})
    if existing is not None:
        return existing
    doc.setdefault("_id", "prop-" + uuid.uuid4().hex[:8])
    doc.setdefault("diff", [])
    doc.setdefault("rationale", "")
    doc.setdefault("evidence", [])
    doc.setdefault("filed_by", "improver")
    doc.setdefault("created_at", _now())
    if doc.get("created_sim") is None:
        doc["created_sim"] = _sim_now(db, doc["created_at"])
    doc.update(status="pending", tier=None, decision=None, tuning=None,
               heldout_candidate=None, heldout_champion=None, approval_hash=None)
    try:
        db.proposals.insert_one(doc)
    except DuplicateKeyError:
        existing = db.proposals.find_one({"idem_key": doc["idem_key"]})
        if existing is not None:
            return existing
        raise
    _ledger(db, "proposal", doc["filed_by"], {
        "proposal_id": doc["_id"], "field": doc.get("field"), "kind": doc["kind"], "key": doc["key"],
        "base_version": doc["base_version"], "diff": list(doc["diff"]), "rationale": doc["rationale"],
        "idem_key": doc["idem_key"],
    }, doc["created_sim"])
    return doc


def pending(db, field: Optional[str] = None) -> list[dict]:
    """Proposals waiting for the gate, oldest first."""
    query: dict = {"status": "pending"}
    if field is not None:
        query["field"] = field
    return list(db.proposals.find(query).sort([("created_at", 1)]))


def evaluate_pending(db, llm, field: Optional[str] = None, k: Optional[int] = None) -> list[dict]:
    """Run every pending proposal through the gate, oldest first."""
    return [evaluate_proposal(db, p["_id"], llm, k=k) for p in pending(db, field)]


# ---------------------------------------------------------------------------------------------------------------
# Evaluation with a champion cache in eval_runs
# ---------------------------------------------------------------------------------------------------------------
def _llm_tag(llm) -> str:
    if getattr(llm, "is_fake", False):
        return "fake"
    try:
        return "|".join(str(llm.model_id(role)) for role in ("drafter", "reader"))
    except Exception:
        return type(llm).__name__


def _scenarios(db, field: str, split: str) -> list[dict]:
    rows = list(db.eval_scenarios.find({"field": field, "split": split}).sort([("_id", 1)]))
    if not rows:
        raise RuntimeError(f"no {split} scenarios for {field} in eval_scenarios; load the scenarios before gating")
    return rows


def _clean(summary: dict, label: str) -> dict:
    out = {k: summary[k] for k in _SUMMARY_KEYS if k in summary}
    out["config_label"] = label
    return out


def _failures(grades: Any, scenarios: list[dict]) -> list[dict]:
    """Wrong answers on the tuning split, with the question text, for the improver to read."""
    if isinstance(grades, dict):
        items = [(sid, g) for sid, runs in grades.items() for g in (runs if isinstance(runs, list) else [runs])]
    elif isinstance(grades, list):
        items = [(g.get("scenario_id"), g) for g in grades if isinstance(g, dict)]
    else:
        return []
    text = {(s["_id"], q.get("id")): q.get("text", "") for s in scenarios for q in s.get("questions", [])}
    seen, out = set(), []
    for sid, g in items:
        for r in (g or {}).get("results", []) or []:
            if r.get("correct") or (sid, r.get("question_id")) in seen:
                continue
            seen.add((sid, r.get("question_id")))
            out.append({"scenario_id": sid, "question_id": r.get("question_id"), "kind": r.get("kind"),
                        "question": text.get((sid, r.get("question_id")), ""),
                        "answer": str(r.get("answer", ""))[:200], "reason": str(r.get("reason", ""))})
    return out[:MAX_FAILURES]


def _summary(db, cfg: dict, field: str, split: str, scenarios: list[dict], llm, k: int, label: str) -> dict:
    """EvalSummary for cfg on these scenarios, cached in eval_runs by (config_hash, split, k, scenario set, model).

    Only tuning rows keep per-question failures; the improver may read tuning rows and nothing else.
    """
    chash = _mod("versions").config_hash(cfg)
    set_hash = _sha(sorted(s["_id"] for s in scenarios))[:12]
    run_key = f"summary:{field}:{split}:{set_hash}:{_sha(_llm_tag(llm))[:8]}"
    row_id = f"{chash[:24]}:{run_key}:k{k}"
    hit = db.eval_runs.find_one({"_id": row_id})
    if hit is not None:
        return _clean(hit["summary"], label)

    oracle = _mod("oracle")
    grades = None
    if hasattr(oracle, "evaluate_grades"):          # oracle extra: per-run grades, so tuning failures have reasons
        grades = oracle.evaluate_grades(cfg, scenarios, llm, k=k, config_label=label)
        raw = _mod("metrics").summarize(label, split, field, k, grades)
    else:
        raw = oracle.evaluate(cfg, scenarios, llm, k=k, config_label=label)
        grades = raw.get("grades")
    summary = _clean(raw, label)
    db.eval_runs.replace_one({"_id": row_id}, {
        "_id": row_id, "row": "summary", "config_hash": chash, "scenario_id": run_key, "run": k,
        "field": field, "split": split, "k": k, "config_label": label, "summary": summary,
        "failures": _failures(grades, scenarios) if split == "tuning" else [],
        "created_at": _now(),
    }, upsert=True)
    return dict(summary)


def tuning_baseline(db, field: str, llm) -> dict:
    """Score the current champion on the tuning split (cached), so the improver has failure reasons to read
    before the first proposal. Trusted code: call it from the loop, never from the improver."""
    cfg = _mod("versions").resolve_field_config(db, field)
    return _summary(db, cfg, field, "tuning", _scenarios(db, field, "tuning"), llm, TUNING_K, "champion")


# ---------------------------------------------------------------------------------------------------------------
# Decision sentences
# ---------------------------------------------------------------------------------------------------------------
def _f(s: dict, key: str) -> float:
    return float(s.get(key, 0.0) or 0.0)


def _gains(cand: dict, champ: dict) -> str:
    return (f"held-out accuracy {_f(champ, 'mean_accuracy'):.2f} -> {_f(cand, 'mean_accuracy'):.2f}, "
            f"worst scenario {_f(champ, 'worst_accuracy'):.2f} -> {_f(cand, 'worst_accuracy'):.2f}")


def _loss_reason(cand: dict, champ: dict, cmp: dict) -> str:
    c, h = _f(cand, "mean_accuracy"), _f(champ, "mean_accuracy")
    others = []
    if _f(cand, "worst_accuracy") + _EPS < _f(champ, "worst_accuracy"):
        others.append(f"worst scenario fell {_f(champ, 'worst_accuracy'):.2f} -> {_f(cand, 'worst_accuracy'):.2f}")
    if _f(cand, "false_alarms") > _f(champ, "false_alarms") + _EPS:
        others.append(f"false alarms rose {_f(champ, 'false_alarms'):.2f} -> {_f(cand, 'false_alarms'):.2f} per run")
    if _f(cand, "stale_claims") > _f(champ, "stale_claims") + _EPS:
        others.append(f"stale claims rose {_f(champ, 'stale_claims'):.2f} -> {_f(cand, 'stale_claims'):.2f} per run")
    ct, ht = _f(cand, "context_tokens"), _f(champ, "context_tokens")
    if ht > 0 and ct > ht * (1 + CONTEXT_GROWTH_LIMIT) + _EPS:
        others.append(f"context grew {ht:.0f} -> {ct:.0f} tokens (+{ct / ht - 1:.0%}, over the "
                      f"+{CONTEXT_GROWTH_LIMIT:.0%} allowed)")
    delta = c - h
    if delta + _EPS < WIN_MARGIN:
        if delta < -_EPS:
            head = f"held-out accuracy {c:.2f} is below champion {h:.2f}"
        else:
            head = f"held-out accuracy {c:.2f} vs champion {h:.2f} is inside the {WIN_MARGIN:.2f} margin"
        return head + ("; also " + "; ".join(others) if others else "")
    if not others:
        others = [str(r) for r in (cmp.get("reasons") or [])] or ["it did not pass the gate's other checks"]
    return f"held-out accuracy {h:.2f} -> {c:.2f}, but " + "; ".join(others)


def _numbers(summary: Optional[dict]) -> Optional[dict]:
    if not summary:
        return None
    return {k: summary.get(k) for k in ("mean_accuracy", "worst_accuracy", "pass_k", "missed_changes",
                                        "false_alarms", "stale_claims", "context_tokens", "n_scenarios", "k")}


# ---------------------------------------------------------------------------------------------------------------
# evaluate_proposal
# ---------------------------------------------------------------------------------------------------------------
def evaluate_proposal(db, proposal_id: str, llm, k: Optional[int] = None) -> dict:
    """Gate one proposal. Returns the proposal as stored afterwards.

    X -> rejected + ledger `refused`. Invalid or stale -> rejected / stale. Otherwise a tuning screen (k=1), then
    held-out candidate vs champion (k = settings().k); lose -> rejected; G win -> committed by the gate;
    H win -> awaiting_owner with approval_hash. A proposal that is not `pending` is returned unchanged.
    """
    p = _get(db, proposal_id)
    if p.get("status") != "pending":
        return p
    if not _move(db, proposal_id, "pending", {"status": "evaluating"}):
        return _get(db, proposal_id)                    # someone else took it
    sim = _sim_now(db, p.get("created_sim"))
    kind = p.get("kind")

    if kind not in CONFIG_KINDS:
        tier = classify(p, None)
        decision = FROZEN_DECISION.format(kind=kind)
        if _move(db, proposal_id, "evaluating", {"status": "rejected", "tier": tier, "decision": decision}):
            _ledger(db, "refused", "gate", {"proposal_id": proposal_id, "field": p.get("field"), "kind": kind,
                                            "key": p.get("key"), "decision": decision}, sim)
        return _get(db, proposal_id)

    try:
        _evaluate_config_change(db, p, llm, k, sim)
    except Exception:
        # Hand it back untouched, then fail loudly.
        _move(db, proposal_id, "evaluating", {"status": "pending", "tier": None, "decision": None, "tuning": None,
                                              "heldout_candidate": None, "heldout_champion": None})
        raise
    return _get(db, proposal_id)


def _reject(db, p: dict, sim: datetime, fields: dict, *, status: str = "rejected",
            evaluation: Optional[dict] = None) -> None:
    """Move the proposal to rejected/stale; only if that move happened, append the ledger receipts for it
    (the `eval` entry first when there are numbers, then `reject`). The ledger never runs ahead of the database."""
    if _move(db, p["_id"], "evaluating", {**fields, "status": status}):
        if evaluation is not None:
            _eval_entry(db, p, sim, outcome=status, decision=fields["decision"], **evaluation)
        _ledger(db, "reject", "gate", {"proposal_id": p["_id"], "field": p["field"], "kind": p["kind"],
                                       "key": p["key"], "status": status, "decision": fields["decision"]}, sim)


def _eval_entry(db, p: dict, sim: datetime, *, tier: str, outcome: str, decision: str,
                tuning: tuple, heldout: Optional[tuple]) -> None:
    _ledger(db, "eval", "gate", {
        "proposal_id": p["_id"], "field": p["field"], "kind": p["kind"], "key": p["key"], "tier": tier,
        "tuning": {"candidate": _numbers(tuning[0]), "champion": _numbers(tuning[1])},
        "heldout": None if heldout is None else {"candidate": _numbers(heldout[0]), "champion": _numbers(heldout[1]),
                                                 "win": bool(heldout[2].get("win")),
                                                 "reasons": [str(r) for r in heldout[2].get("reasons") or []]},
        "outcome": outcome, "decision": decision,
    }, sim)


def _evaluate_config_change(db, p: dict, llm, k: Optional[int], sim: datetime) -> None:
    versions = _mod("versions")
    pid, kind, key, field, base = p["_id"], p["kind"], p["key"], p["field"], p["base_version"]
    champ_cfg = versions.resolve_field_config(db, field)
    current_body = champ_cfg[kind]
    head = int(champ_cfg["versions"][kind])
    tier = classify(p, current_body)

    errors = validate(p, current_body)
    if errors:
        _reject(db, p, sim, {"tier": tier, "decision": "Rejected: invalid proposal: " + "; ".join(errors) + "."})
        return
    if head != base:
        _reject(db, p, sim, {"tier": tier, "decision": f"Stale: {kind}:{key} moved from v{base} to v{head} before "
                                                     f"the gate ran; propose again against v{head}."},
                status="stale")
        return

    tuning_scen = _scenarios(db, field, "tuning")
    heldout_scen = _scenarios(db, field, "heldout")
    cand_cfg = versions.with_change(champ_cfg, kind, p["body"])
    label = f"candidate:{pid}"

    # 1. Tuning screen: drop anything that is worse where the improver could see.
    champ_t = _summary(db, champ_cfg, field, "tuning", tuning_scen, llm, TUNING_K, "champion")
    cand_t = _summary(db, cand_cfg, field, "tuning", tuning_scen, llm, TUNING_K, label)
    if _f(cand_t, "mean_accuracy") + _EPS < _f(champ_t, "mean_accuracy"):
        decision = (f"Rejected: did not improve on tuning (accuracy {_f(cand_t, 'mean_accuracy'):.2f} vs champion "
                    f"{_f(champ_t, 'mean_accuracy'):.2f}); held-out data was not used.")
        _reject(db, p, sim, {"tier": tier, "tuning": cand_t, "decision": decision},
                evaluation={"tier": tier, "tuning": (cand_t, champ_t), "heldout": None})
        return

    # 2. Held-out: candidate vs champion on questions the improver has never seen.
    k = int(k) if k is not None else _default_k()
    champ_h = _summary(db, champ_cfg, field, "heldout", heldout_scen, llm, k, "champion")
    cand_h = _summary(db, cand_cfg, field, "heldout", heldout_scen, llm, k, label)
    cmp = _mod("metrics").compare(cand_h, champ_h)
    stored = {"tier": tier, "tuning": cand_t, "heldout_candidate": cand_h, "heldout_champion": champ_h}
    heldout = (cand_h, champ_h, cmp)

    evaluation = {"tier": tier, "tuning": (cand_t, champ_t), "heldout": heldout}

    if not cmp.get("win"):
        decision = "Rejected: " + _loss_reason(cand_h, champ_h, cmp) + "."
        _reject(db, p, sim, {**stored, "decision": decision}, evaluation=evaluation)
        return

    if tier == "H":
        decision = f"Awaiting owner approval (tier H): {_gains(cand_h, champ_h)}. " \
                   f"{_owner_reason(kind, current_body, p['body'])}"
        ahash = content_hash(kind, key, base, p["body"])
        if _move(db, pid, "evaluating", {**stored, "status": "awaiting_owner", "decision": decision,
                                         "approval_hash": ahash}):
            _eval_entry(db, p, sim, outcome="awaiting_owner", decision=decision, **evaluation)
        return

    # Tier G win. The summaries are stored first (status stays `evaluating`); the ledger records the outcome only
    # AFTER versions.commit returns, so it can never claim a commit the database does not have.
    decision = f"Committed automatically (tier G): {_gains(cand_h, champ_h)}."
    if not _move(db, pid, "evaluating", {**stored}):
        return
    try:
        versions.commit(db, kind, key, base, p["body"], rationale=p.get("rationale") or decision, sim_time=sim,
                        proposal_id=pid, approved_by="gate")
    except versions.StaleVersion as exc:
        _reject(db, p, sim, {"decision": f"Stale: it won on held-out data ({_gains(cand_h, champ_h)}) but the "
                                         f"commit was refused ({exc}); propose again against the new head."},
                status="stale", evaluation=evaluation)
        return
    # Any other failure propagates: evaluate_proposal hands the proposal back to pending and nothing claims success.
    db.proposals.update_one({"_id": pid, "status": {"$in": ["evaluating", "committed"]}},
                            {"$set": {"status": "committed", "decision": decision}})
    _eval_entry(db, p, sim, outcome="committed", decision=decision, **evaluation)


# ---------------------------------------------------------------------------------------------------------------
# Owner actions
# ---------------------------------------------------------------------------------------------------------------
def approve(db, proposal_id: str, approval_hash: str, owner: str, sim_time: datetime) -> dict:
    """Commit a tier-H proposal the owner signed. The hash must match the stored approval_hash (which must still
    match the proposal's content) and the head must still be at base_version, else the proposal is `stale`."""
    p = _get(db, proposal_id)
    actor = f"owner:{owner}"
    if p.get("status") != "awaiting_owner":
        raise ValueError(f"{proposal_id} is {p.get('status')}, not awaiting_owner; nothing to approve")
    stored = p.get("approval_hash") or ""
    recomputed = content_hash(p["kind"], p["key"], p["base_version"], p["body"])
    if not (isinstance(approval_hash, str) and stored
            and hmac.compare_digest(approval_hash, stored) and hmac.compare_digest(stored, recomputed)):
        _ledger(db, "refused", actor, {"proposal_id": proposal_id,
                                       "reason": "approval hash does not match the proposal"}, sim_time)
        raise PermissionError(f"approval hash does not match {proposal_id}; nothing was committed")

    versions = _mod("versions")
    kind, key, base = p["kind"], p["key"], p["base_version"]
    head = int(versions.head(db, kind, key))
    if head != base:
        _mark_stale(db, p, head, actor, sim_time)
        raise versions.StaleVersion(f"{kind}:{key} is at v{head}, not v{base}; {proposal_id} is stale")
    try:
        cv = versions.commit(db, kind, key, base, p["body"], rationale=p.get("rationale") or p.get("decision") or "",
                             sim_time=sim_time, proposal_id=proposal_id, approval_hash=stored, approved_by=actor)
    except versions.StaleVersion:
        _mark_stale(db, p, int(versions.head(db, kind, key)), actor, sim_time)
        raise
    _move(db, proposal_id, "awaiting_owner", {"status": "committed"})   # no-op when commit already marked it
    version_id = cv.get("_id", f"{kind}:{key}@v{base + 1}")
    before = p.get("decision") or ""
    signed = f"Committed with owner approval (tier H, signed by {owner} as {version_id})"
    decision = before.replace("Awaiting owner approval (tier H)", signed, 1) if before else signed + "."
    if decision == before:
        decision = f"{before} {signed}."
    db.proposals.update_one({"_id": proposal_id, "status": "committed"}, {"$set": {"decision": decision}})
    _ledger(db, "approve", actor, {"proposal_id": proposal_id, "version_id": cv.get("_id"),
                                   "approval_hash": stored}, sim_time)
    return cv


def _mark_stale(db, p: dict, head: int, actor: str, sim_time: datetime) -> None:
    decision = (f"Stale: {p['kind']}:{p['key']} moved from v{p['base_version']} to v{head} while this waited for "
                f"approval; propose again against v{head}.")
    if _move(db, p["_id"], "awaiting_owner", {"status": "stale", "decision": decision}):
        _ledger(db, "reject", actor, {"proposal_id": p["_id"], "status": "stale", "decision": decision}, sim_time)


def reject(db, proposal_id: str, owner: str, reason: str, sim_time: datetime) -> dict:
    """The owner turns down a proposal that is pending or awaiting approval."""
    p = _get(db, proposal_id)
    status = p.get("status")
    if status not in ("pending", "awaiting_owner"):
        raise ValueError(f"{proposal_id} is {status}; only pending or awaiting_owner proposals can be rejected")
    reason = (reason or "no reason given").strip().rstrip(".")
    decision = f"Rejected by owner {owner}: {reason}."
    if not _move(db, proposal_id, status, {"status": "rejected", "decision": decision}):
        raise ValueError(f"{proposal_id} changed status while being rejected; reload and try again")
    _ledger(db, "reject", f"owner:{owner}", {"proposal_id": proposal_id, "status": "rejected",
                                             "decision": decision}, sim_time)
    return _get(db, proposal_id)
