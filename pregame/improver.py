"""The improver: reads what it is allowed to read and proposes ONE small change to the harness.

It reads through `ImproverView` only: a plain-data snapshot of feedback, live briefs, config versions and heads,
TUNING eval rows, and its own past proposals with the held-out summaries removed. The view holds no database handle.
It never sees held-out rows or scenarios, never imports the oracle, and never commits: the gate decides.
"""
from __future__ import annotations

import copy
import importlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pregame.contracts import BRIEF_SECTIONS, FACT_KINDS, FIELDS, POLICY_BOUNDS, RULES_CAP, SOURCES

CONFIG_KINDS = ("policy", "rules", "tools", "guardrails")
READABLE = ("feedback", "briefs", "config_versions", "config_heads", "eval_runs", "proposals")
HIDDEN_PROPOSAL_KEYS = ("heldout_candidate", "heldout_champion", "approval_hash", "idem_key")
BUILTIN_CHECKS = frozenset({"cite-facts", "no-stale-facts", "no-advice", "approved-language"})

# Feedback words -> the fact kind the brief was probably missing (fake improver).
FEEDBACK_KINDS = (
    ("regulation", ("regulat", "rule", "law", "compliance", "mandate", "legislat", "ruling", "statute",
                    "401(k)", "limit", "rmd", "required minimum", "exemption", "exclusion", "tax", "credit",
                    "contribution", "medicare", "irs")),
    ("disruption", ("sell-off", "selloff", "market drop", "crash", "downturn", "bank fail", "failure", "failed",
                    "collapse", "gate", "frozen", "freeze", "disrupt", "shock", "panic")),
    ("competitor", ("fund fee", "fee", "fund closure", "merger", "new product", "new offering", "payout",
                    "annuity rate")),
    ("demand", ("inflation", "unemployment", "jobs", "cola", "cost-of-living", "tuition", "economy")),
    ("price", ("rate", "yield", "price", "premium", "cost")),
)
# The targeted policy raises max_facts by about a third (6 -> 8), not to 10: with the v1 world data 6 -> 10 facts
# grows the context +52..59%, over metrics' +50% companion limit, so it would lose for bloat like the broad policy.
BUDGET_RULE = {
    "id": "lead-with-budget",
    "text": "Open what_changed with the change that moves this household's money the most, and state its number "
            "(the new rate, limit or date) in that first line.",
}
_LEAD_RULE = re.compile(r"\b(lead|open|start|begin|first|summar|overview)", re.I)
_HELDOUT_NUMBER = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?%?")


def _mod(name: str):
    return importlib.import_module(f"pregame.{name}")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------------------------------------------
# The view: a snapshot of what the improver may read (a data-transfer boundary)
# ---------------------------------------------------------------------------------------------------------------
SNAPSHOT_TAG = "pregame.improver-snapshot/1"
FEEDBACK_LIMIT = 200
BRIEFS_PER_FIELD = 3
REFUSAL_REASON = ("the improver may read only feedback, live briefs, config versions and heads, tuning eval rows "
                  "and its own past proposals with held-out results removed")
_VIEW_STATE = ("_data", "_sim_time", "refusals")


def _plain(value: Any) -> Any:
    """Deep copy into plain Python data (dict, list, str, int, float, bool, None, UTC datetime); anything else
    becomes its string. Nothing a database driver hands back (cursors, handles, ObjectIds, tz classes) survives."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, datetime):
        utc = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return datetime(utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second, utc.microsecond,
                        tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def build_snapshot(db, fields: Optional[list] = None) -> dict:
    """TRUSTED code: read exactly what the improver may see, as plain data. The question bank, held-out eval rows,
    held-out summaries, approval hashes and the ledger are never read here, so they cannot be in the snapshot."""
    fields = [str(f) for f in (fields or FIELDS)]
    versions = _mod("versions")
    configs, hashes = {}, {}
    for f in fields:
        cfg = versions.resolve_field_config(db, f)
        configs[f], hashes[f] = cfg, versions.config_hash(cfg)
    heads = [f"{kind}:{f}" for f in fields for kind in ("policy", "rules", "tools")] + ["guardrails:global"]
    live = {"$or": [{"config_label": "live"}, {"config_label": {"$exists": False}}]}
    briefs = []
    for f in fields:
        briefs += list(db.briefs.find({"field": f, **live}).sort([("as_of", -1)]).limit(BRIEFS_PER_FIELD))
    return _plain({
        "tag": SNAPSHOT_TAG,
        "fields": fields,
        "configs": configs,
        "config_hashes": hashes,
        "feedback": list(db.feedback.find({"field": {"$in": fields}}).sort([("sim_time", -1)]).limit(FEEDBACK_LIMIT)),
        "briefs": briefs,
        "config_versions": list(db.config_versions.find({"key": {"$in": fields + ["global"]}})
                                .sort([("kind", 1), ("version", 1)])),
        "config_heads": list(db.config_heads.find({"_id": {"$in": heads}})),
        "eval_runs": list(db.eval_runs.find({"field": {"$in": fields}, "split": "tuning"})
                          .sort([("created_at", -1)])),
        "proposals": [_redact(p) for p in db.proposals.find({"field": {"$in": fields}}).sort([("created_at", 1)])],
    })


class ImproverView:
    """What the improver may read, as a snapshot of plain data. It holds NO database, client, collection or cursor.

    Built by trusted code: `ImproverView(db)` (or `ImproverView.from_db(db)`) reads the permitted data through
    `build_snapshot` and keeps only the copy. Allowed: feedback, live briefs, config versions and heads, eval rows
    with split == "tuning" (aggregates and per-question failure reasons), and past proposals without their held-out
    summaries or hashes. Anything else raises PermissionError and is appended to `view.refusals`; the trusted
    caller writes those to the ledger with `record_refusals(db, view, sim_time)`.
    """

    def __init__(self, source, sim_time: Optional[datetime] = None, fields: Optional[list] = None):
        is_snapshot = isinstance(source, dict) and source.get("tag") == SNAPSHOT_TAG
        snapshot = _plain(source) if is_snapshot else build_snapshot(source, fields)
        object.__setattr__(self, "_data", snapshot)
        object.__setattr__(self, "_sim_time", _plain(sim_time))
        object.__setattr__(self, "refusals", [])

    @classmethod
    def from_db(cls, db, sim_time: Optional[datetime] = None, fields: Optional[list] = None) -> "ImproverView":
        return cls(build_snapshot(db, fields), sim_time=sim_time)

    # -- allowed reads ------------------------------------------------------------------------------------------
    def _known_field(self, field: str) -> None:
        if field not in self._data["fields"]:
            raise KeyError(f"field {field!r} is not in this view's snapshot")

    def _rows(self, name: str, field: str) -> list[dict]:
        self._known_field(field)
        return [copy.deepcopy(r) for r in self._data[name] if r.get("field") == field]

    def feedback(self, field: str, n: int = 20) -> list[dict]:
        """Advisor feedback for the segment, newest first."""
        return self._rows("feedback", field)[:n]

    def briefs(self, field: str, n: int = 3) -> list[dict]:
        """Recent LIVE briefs for the field, newest first (never the gate's candidate/champion eval briefs)."""
        return self._rows("briefs", field)[:n]

    def current_config(self, field: str) -> dict:
        """The FieldConfig the harness is running now."""
        self._known_field(field)
        return copy.deepcopy(self._data["configs"][field])

    def current_config_hash(self, field: str) -> str:
        self._known_field(field)
        return self._data["config_hashes"][field]

    def config_history(self, field: str) -> list[dict]:
        """Every version of policy/rules/tools for the field and of the global guardrails."""
        self._known_field(field)
        return [copy.deepcopy(v) for v in self._data["config_versions"] if v.get("key") in (field, "global")]

    def config_heads(self, field: str) -> list[dict]:
        self._known_field(field)
        ids = {f"{kind}:{field}" for kind in ("policy", "rules", "tools")} | {"guardrails:global"}
        return [copy.deepcopy(h) for h in self._data["config_heads"] if h.get("_id") in ids]

    def eval_runs(self, field: str, split: str = "tuning") -> list[dict]:
        """Eval rows for the field. Only the tuning split; asking for any other split is refused."""
        if split != "tuning":
            self._refuse(f"eval_runs split={split!r}")
        return self._rows("eval_runs", field)

    def tuning_results(self, field: str) -> list[dict]:
        """Tuning-split eval rows (aggregates plus per-question failure reasons), newest first."""
        return self.eval_runs(field, "tuning")

    def proposals(self, field: str) -> list[dict]:
        """Past proposals for the field, oldest first, with held-out summaries and approval hashes removed.
        Numbers in a held-out decision are masked: the improver learns THAT it lost on held-out data, not by how much.
        """
        return self._rows("proposals", field)

    # -- everything else is refused -----------------------------------------------------------------------------
    def __getitem__(self, name: str):
        readers = {"feedback": self.feedback, "briefs": self.briefs, "config_versions": self.config_history,
                   "config_heads": self.config_heads, "eval_runs": self.tuning_results, "proposals": self.proposals}
        if name in readers:
            return readers[name]
        self._refuse(str(name))

    def __getattr__(self, name: str):
        if name.startswith("__") or name in _VIEW_STATE:
            raise AttributeError(name)                  # Python internals (copy, pickle, repr) stay ordinary
        self._refuse(name)                              # includes asking for a `_db` handle: there is none

    def _refuse(self, what: str):
        self.refusals.append({"attempted": what, "reason": REFUSAL_REASON})
        raise PermissionError(f"the improver may not read {what}")


def record_refusals(db, view: ImproverView, sim_time: Optional[datetime] = None) -> list[dict]:
    """TRUSTED code: write the view's refusals to the ledger as `refused` entries (actor improver), once each."""
    when = sim_time or view._sim_time
    if when is None:
        try:
            when = _mod("world.store").sim_now(db)
        except Exception:
            when = datetime.now(timezone.utc)
    ledger = _mod("ledger")
    written = []
    while view.refusals:
        written.append(ledger.append(db, "refused", "improver", dict(view.refusals.pop(0)), when))
    return written


def _redact(p: dict) -> dict:
    out = {k: v for k, v in p.items() if k not in HIDDEN_PROPOSAL_KEYS}
    decision = out.get("decision")
    if isinstance(decision, str) and "held-out" in decision:
        out["decision"] = _HELDOUT_NUMBER.sub("#", decision)
    return out


def _current_tuning(rows: list[dict], chash: Optional[str]) -> Optional[dict]:
    """The tuning row for the config that is running now (by config hash), else the newest champion row."""
    match = [r for r in rows if chash and r.get("config_hash") == chash]
    if not match:
        match = [r for r in rows if str(r.get("config_label", "")).startswith("champion")]
    return match[0] if match else None


# ---------------------------------------------------------------------------------------------------------------
# Diffs (code-rendered, so the owner reviews what the body really says)
# ---------------------------------------------------------------------------------------------------------------
def describe_diff(kind: str, old: Any, new: Any) -> list[str]:
    if kind == "policy" and isinstance(old, dict) and isinstance(new, dict):
        lines = []
        for knob in sorted(set(old) | set(new)):
            a, b = old.get(knob), new.get(knob)
            if a == b:
                continue
            if knob == "include_kinds" and isinstance(a, list) and isinstance(b, list):
                parts = [f"+ {k}" for k in b if k not in a] + [f"- {k}" for k in a if k not in b]
                lines.append(f"include_kinds: {', '.join(parts) or 'reordered'}")
            elif knob == "section_order" and isinstance(a, list) and isinstance(b, list):
                lines.append(f"section_order: {', '.join(a)} -> {', '.join(b)}")
            else:
                lines.append(f"{knob}: {a} -> {b}")
        return lines
    if kind == "rules" and isinstance(old, list) and isinstance(new, list):
        old_ids = [r.get("id") for r in old if isinstance(r, dict)]
        new_ids = [r.get("id") for r in new if isinstance(r, dict)]
        old_by = {r.get("id"): r for r in old if isinstance(r, dict)}
        lines, added = [], [i for i in new_ids if i not in old_ids]
        removed = [i for i in old_ids if i not in new_ids]
        for gone in removed:
            lines.append(f"rule {gone}: replaced by {added.pop(0)}" if added else f"rule {gone}: removed")
        lines += [f"rule {i}: added" for i in added]
        for r in new:
            if isinstance(r, dict) and r.get("id") in old_by and old_by[r["id"]].get("text") != r.get("text"):
                lines.append(f"rule {r['id']}: reworded")
        lines.append(f"rules: {len(old)} -> {len(new)} (cap {RULES_CAP})")
        return lines
    if kind == "tools" and isinstance(old, dict) and isinstance(new, dict):
        onoff = {True: "on", False: "off"}
        return [f"{s}: {onoff[bool(old.get(s))]} -> {onoff[bool(new.get(s))]}"
                for s in SOURCES if bool(old.get(s)) != bool(new.get(s))]
    if kind == "guardrails" and isinstance(old, list) and isinstance(new, list):
        old_by = {g.get("id"): g for g in old if isinstance(g, dict)}
        new_by = {g.get("id"): g for g in new if isinstance(g, dict)}
        lines = [f"guardrail {i}: removed" for i in old_by if i not in new_by]
        lines += [f"guardrail {i}: added (check {g.get('check')})" for i, g in new_by.items() if i not in old_by]
        for i, g in new_by.items():
            if i in old_by:
                o = old_by[i]
                if bool(o.get("enabled")) != bool(g.get("enabled")):
                    lines.append(f"guardrail {i}: {'enabled' if g.get('enabled') else 'disabled'}")
                if o.get("check") != g.get("check") or o.get("text") != g.get("text"):
                    lines.append(f"guardrail {i}: rewritten")
        return lines
    return [f"{kind}: replaced"]


# ---------------------------------------------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------------------------------------------
def propose(view: ImproverView, field: str, llm, sim_time: datetime) -> Optional[dict]:
    """One small change to the harness as a pending Proposal (not yet filed), or None when there is nothing to try."""
    cfg = view.current_config(field)
    past = [p for p in view.proposals(field) if str(p.get("filed_by", "improver")) == "improver"]
    if getattr(llm, "is_fake", False):
        draft = _fake_change(view, field, cfg, past)
    else:
        draft = _live_change(view, field, cfg, past, llm)
    if draft is None:
        return None
    return _as_proposal(field, cfg, draft, sim_time)


def _as_proposal(field: str, cfg: dict, draft: dict, sim_time: datetime) -> dict:
    from pregame.gate import content_hash              # pure helper; the gate's evaluation code is never called

    kind = str(draft["kind"])
    body = draft["body"]
    if kind in CONFIG_KINDS:
        key = "global" if kind == "guardrails" else field
        base = int(cfg["versions"][kind])
        diff = describe_diff(kind, cfg[kind], body)
    else:
        key = str(draft.get("key") or f"{field}:{kind}")
        base = 1
        diff = [str(line) for line in draft.get("diff") or []] or [f"{kind}: replaced"]
    return {
        "_id": "prop-" + uuid.uuid4().hex[:8],
        "field": field,
        "kind": kind,
        "key": key,
        "base_version": base,
        "body": body,
        "diff": diff,
        "rationale": str(draft.get("rationale") or ""),
        "evidence": [str(e) for e in draft.get("evidence") or []],
        "filed_by": "improver",
        "idem_key": content_hash(kind, key, base, body),
        "status": "pending",
        "tier": None,
        "decision": None,
        "tuning": None,
        "heldout_candidate": None,
        "heldout_champion": None,
        "approval_hash": None,
        "created_sim": sim_time,
        "created_at": datetime.now(timezone.utc),
    }


def _draft_errors(field: str, cfg: dict, draft: Optional[dict]) -> list[str]:
    if not isinstance(draft, dict) or "kind" not in draft or "body" not in draft:
        return ["reply with one JSON object that has kind, body, diff, rationale and evidence"]
    from pregame.gate import validate                  # pure

    kind = draft["kind"]
    if kind not in CONFIG_KINDS:
        return [f"kind must be one of {', '.join(CONFIG_KINDS)}; evaluation, scenarios, the oracle, metrics, "
                f"the gate and the ledger are frozen"]
    checks = BUILTIN_CHECKS | {g.get("check") for g in cfg.get("guardrails", []) if isinstance(g, dict)}
    probe = {"kind": kind, "field": field, "key": "global" if kind == "guardrails" else field,
             "base_version": int(cfg["versions"][kind]), "body": draft["body"]}
    return validate(probe, cfg[kind], known_checks=checks)


# -- live ---------------------------------------------------------------------------------------------------------
IMPROVER_SYSTEM = f"""You improve the harness of Pregame, a bot that writes prep briefs for financial advisors
before client reviews. You may change exactly ONE surface per proposal:
- "policy": the context policy knobs (recency_days, max_facts, include_kinds, section_order, likely_questions,
  prefer_exposed);
- "rules": the drafter's rules, a list of {{"id", "text"}};
- "tools": which fact sources are switched on ({", ".join(SOURCES)});
- "guardrails": the global guardrails, a list of {{"id", "text", "check", "enabled"}}.
Prefer the smallest change that explains the failures you are shown. Replace a rule rather than append past the cap
of {RULES_CAP} rules; each rule is at most 300 characters. Do not repeat an idea that was already rejected.
Never propose changes to evaluation: the scenarios, questions, oracle, metrics, gate and ledger are frozen, and a
proposal that touches them is refused. You will not see the held-out results; the gate measures your change there.
Reply with ONE JSON object and nothing else:
{{"kind": "policy|rules|tools|guardrails", "body": <the WHOLE new body for that kind>, "diff": ["one line per change"],
  "rationale": "one or two plain sentences", "evidence": ["feedback ids and tuning scenario ids you relied on"]}}"""


def _live_prompt(field: str, cfg: dict, feedback: list[dict], tuning: Optional[dict], past: list[dict],
                 briefs: list[dict]) -> str:
    def dump(obj: Any) -> str:
        return json.dumps(obj, indent=1, default=str)

    checks = sorted(BUILTIN_CHECKS | {g.get("check") for g in cfg.get("guardrails", []) if isinstance(g, dict)})
    out = [f"FIELD: {field}", f"CURRENT HARNESS (versions {dump(cfg.get('versions', {}))}):",
           f"policy = {dump(cfg['policy'])}", f"rules = {dump(cfg['rules'])}", f"tools = {dump(cfg['tools'])}",
           f"guardrails = {dump(cfg['guardrails'])}",
           "LIMITS: " + "; ".join(f"{k} {lo}..{hi}" for k, (lo, hi) in POLICY_BOUNDS.items())
           + f"; include_kinds a non-empty subset of {list(FACT_KINDS)}; section_order a permutation of "
             f"{list(BRIEF_SECTIONS)}; at most {RULES_CAP} rules with unique ids; tools must list exactly "
             f"{list(SOURCES)}; guardrail checks must be one of {checks}.",
           "", "RECENT FEEDBACK FROM ADVISORS (newest first):"]
    out += [f"- [{f.get('_id')}] {f.get('text', '')}" for f in feedback] or ["- (none yet)"]
    out += ["", "TUNING RESULTS FOR THE CURRENT HARNESS:"]
    if tuning:
        s = tuning.get("summary", {})
        out.append(f"mean accuracy {s.get('mean_accuracy')}, worst scenario {s.get('worst_accuracy')}, missed "
                   f"changes {s.get('missed_changes')} and false alarms {s.get('false_alarms')} per run, "
                   f"context tokens {s.get('context_tokens')}.")
        fails = tuning.get("failures") or []
        out.append("Questions it got wrong:" if fails else "No wrong answers on the tuning questions.")
        out += [f"- [{f.get('scenario_id')}] ({f.get('kind')}) \"{f.get('question')}\" -> {f.get('reason')}"
                for f in fails]
    else:
        out.append("(no tuning results yet)")
    out += ["", "YOUR PAST PROPOSALS (oldest first):"]
    out += [f"- {p.get('kind')} {'; '.join(p.get('diff') or [])} -> {p.get('status')}: {p.get('decision') or ''}"
            for p in past] or ["- (none)"]
    if briefs:
        out += ["", "LATEST LIVE BRIEF (excerpt):", str(briefs[0].get("markdown", ""))[:2000]]
    out += ["", "Propose ONE small change as the JSON object described."]
    return "\n".join(out)


def _live_change(view: ImproverView, field: str, cfg: dict, past: list[dict], llm) -> Optional[dict]:
    tuning = _current_tuning(view.tuning_results(field), view.current_config_hash(field))
    prompt = _live_prompt(field, cfg, view.feedback(field, 12), tuning, past, view.briefs(field, 1))
    draft = llm.complete_json("improver", IMPROVER_SYSTEM, prompt)
    errors = _draft_errors(field, cfg, draft)
    if errors:
        retry = (prompt + "\n\nYour last reply was not a valid proposal:\n- " + "\n- ".join(errors)
                 + "\nReturn a corrected JSON object.")
        draft = llm.complete_json("improver", IMPROVER_SYSTEM, retry)
    if not isinstance(draft, dict) or "kind" not in draft or "body" not in draft:
        return None
    return draft                                        # still invalid? the gate rejects it on the record


# -- fake (deterministic demo heuristics) --------------------------------------------------------------------------
def _step_of(p: dict) -> Optional[str]:
    kind, body = p.get("kind"), p.get("body")
    if kind == "policy" and isinstance(body, dict):
        return "broad" if body.get("max_facts") == 30 and body.get("recency_days") == 365 else "targeted"
    if kind == "rules" and isinstance(body, list) and any(isinstance(r, dict) and r.get("id") == BUDGET_RULE["id"]
                                                          for r in body):
        return "rule"
    if kind == "tools" and isinstance(body, dict) and body.get("analyst_notes"):
        return "tool"
    return None


def _fake_change(view: ImproverView, field: str, cfg: dict, past: list[dict]) -> Optional[dict]:
    """(a) an over-broad 'flag everything' policy, (b) a targeted policy from the feedback words, (c) replace the
    opening rule with lead-with-budget, (d) switch analyst_notes on. Each step once, in that order."""
    feedback = view.feedback(field)
    tuning = _current_tuning(view.tuning_results(field), view.current_config_hash(field)) or {}
    tuning_ids = sorted({str(f.get("scenario_id")) for f in tuning.get("failures") or []})[:5]
    evidence = [str(f.get("_id")) for f in feedback][:5] + tuning_ids
    done = {_step_of(p) for p in past}
    builders = (("broad", _broad_policy), ("targeted", _targeted_policy), ("rule", _budget_rule),
                ("tool", _analyst_notes_on))
    for step, build in builders:
        if step in done:
            continue
        draft = build(cfg, feedback, evidence)
        if draft is not None and _canon(draft["body"]) != _canon(cfg[draft["kind"]]):
            return draft
    return None


def _broad_policy(cfg: dict, feedback: list[dict], evidence: list[str]) -> dict:
    body = dict(copy.deepcopy(cfg["policy"]), max_facts=30, include_kinds=list(FACT_KINDS), recency_days=365)
    return {"kind": "policy", "body": body, "evidence": evidence,
            "rationale": "Briefs keep missing changes the client asks about. Widen the context so nothing is left "
                         "out: every fact kind, a full year of history, up to 30 facts."}


def _targeted_policy(cfg: dict, feedback: list[dict], evidence: list[str]) -> dict:
    policy = copy.deepcopy(cfg["policy"])
    kinds = list(policy.get("include_kinds", []))
    added, cited = [], []
    for kind, words in FEEDBACK_KINDS:
        hits = [f for f in feedback if any(w in str(f.get("text", "")).lower() for w in words)]
        if hits and kind not in kinds:
            kinds.append(kind)
            added.append(kind)
            cited += [str(f.get("_id")) for f in hits if str(f.get("_id")) not in cited]
    old_max = int(policy.get("max_facts", 6))
    new_max = min(POLICY_BOUNDS["max_facts"][1], old_max + max(2, old_max // 3))
    body = dict(policy, include_kinds=kinds, max_facts=new_max, prefer_exposed=True)
    missed = f"{' and '.join(added)} news" if added else "changes"
    return {"kind": "policy", "body": body, "evidence": cited + [e for e in evidence if e not in cited],
            "rationale": f"Feedback says the brief missed {missed} that moved the client's costs. Include those "
                         f"fact kinds, rank the client's own exposures first, and allow {new_max} facts "
                         f"instead of {old_max}; nothing else changes."}


def _budget_rule(cfg: dict, feedback: list[dict], evidence: list[str]) -> dict:
    rules = copy.deepcopy(cfg["rules"])
    target = next((i for i, r in enumerate(rules) if isinstance(r, dict) and r.get("id") != BUDGET_RULE["id"]
                   and _LEAD_RULE.search(f"{r.get('id', '')} {r.get('text', '')}")), None)
    if target is None:
        target = len(rules) - 1 if len(rules) >= RULES_CAP else None
    if target is None:
        rules.append(dict(BUDGET_RULE))
        how = f"adds it ({len(rules)} of {RULES_CAP} rules)"
    else:
        how = f"replaces {rules[target].get('id')} so the rule count stays at {len(rules)}"
        rules[target] = dict(BUDGET_RULE)
    return {"kind": "rules", "body": rules, "evidence": evidence,
            "rationale": "Advisors say the client opens with whatever hits their money, and the brief "
                         f"buried it. The new rule leads with that change and its number; it {how}."}


def _analyst_notes_on(cfg: dict, feedback: list[dict], evidence: list[str]) -> Optional[dict]:
    tools = copy.deepcopy(cfg["tools"])
    if tools.get("analyst_notes"):
        return None
    tools["analyst_notes"] = True
    return {"kind": "tools", "body": tools, "evidence": evidence,
            "rationale": "Some changes the client asked about were not in the verified feeds yet. Switch on "
                         "analyst_notes for broader coverage; it is unverified, so the gate checks false alarms."}


# ---------------------------------------------------------------------------------------------------------------
# tamper (for the demo)
# ---------------------------------------------------------------------------------------------------------------
def tamper_proposal(field: str, sim_time: datetime) -> dict:
    """A proposal against a frozen surface: rewrite the held-out questions. The gate must refuse it."""
    draft = {
        "kind": "scenarios",
        "key": f"{field}:heldout",
        "body": {"field": field, "split": "heldout", "action": "rewrite_questions",
                 "instruction": "Rewrite the held-out client questions so they ask only about facts the current "
                                "brief already covers."},
        "diff": ["heldout questions: rewrite them to match what the current brief already covers"],
        "rationale": "Held-out questions keep catching misses; rewriting them to what the brief covers would "
                     "raise accuracy.",
        "evidence": [],
    }
    return _as_proposal(field, {}, draft, sim_time)
