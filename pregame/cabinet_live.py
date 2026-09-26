"""The live-model experiment on the banker cabinet: the same model, with and without the harness, repeated.

Two conditions, one model (the "drafter" role), the same prep slots and the same output schema:

    no_harness  the prompt carries the client's RAW visible material as of the prep date (notes with author and date,
                feed events, market items for their holdings, the reference fee table, the approved disclosure
                texts); the model writes every claim, disclosures included.
    harness     the prompt carries the harness's compiled client file for that date (cabinet.py's adapter and
                HARNESS_POLICY: current-state facts with source, author, date and expiry, and the questions, flags and
                conflicts it derives; the fee from the reference table); the model writes no disclosure, code appends
                the locked AS-01/AS-02 claims (as cabinet.py does).

The instructions are one shared block; only the disclosure rule and the material differ. Prompts carry nothing that
varies between runs (no run index, ids or timestamps), so run 2 asks the same question again and a recording replays by
the same keys (llm.py serves repeated identical prompts in recorded order; runs go one after the other for that).

Scoring is cabinet.score (the cabinet-eval scorer as a subprocess, aggregates only). This module never reads the
answer side.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from pregame import cabinet

ROLE = "drafter"
CONDITIONS = ("no_harness", "harness")
CONDITION_LABELS = {"no_harness": "model, no harness", "harness": "model + harness"}
MAX_TOKENS = 4000
# Claims code writes in the harness arm after the model's reply (post-processing only; the prompts do not change).
# "disclosure": the locked AS-01/AS-02 claims, every prep. "decision_maker": the harness's brief-both-holders claim
# (a list), only where brief_both_holders_on_conflict resolves a conflict; it replaces the model's observation.
CODE_OWNABLE = ("disclosure", "decision_maker")
DEFAULT_CODE_OWNED = ("disclosure", "decision_maker")

SYSTEM = (
    "You write call preps for a private banker at a bank. A call prep is a short briefing the banker reads before a "
    "client call: who the client is now, what to confirm or ask on the call, and anything in the file that needs the "
    "banker's attention. You are careful: you state only what the material supports, as of the prep date. "
    "Reply with ONLY one JSON object, no code fences, no commentary."
)

_INSTRUCTIONS = """TASK
Write the call prep for {name} (client {client_id}) for a call on {day}. The prep date is {day}. Use only the MATERIAL
below; everything in it is dated before the prep date.

OUTPUT
One JSON object: {{"text": "<the prep as plain text for the banker, at most 250 words>", "claims": [<claim>, ...]}}
Each claim is one object: {{"attribute": ..., "value": ..., "kind": ..., "basis": [<source ids>]}}

attribute: risk_attitude | investing_style | decision_maker | retirement_date | fee_rate | disclosure
kind:
  observation  the client's current state as the prep states it (a value is required)
  question     something the banker should ask or reconfirm on the call because the material disagrees, or a
               judgement is out of date; value null
  flag         a note in the file that conflicts with what the client did (for example a check-in logged
               "No changes" although the client's state moved since the previous contact); value null
  disclosure   see DISCLOSURES
values:
{values}
  decision_maker: a holder's full name from the account record; it may be a list of holders' names when the banker
                  should brief more than one holder
  retirement_date: "YYYY-MM", or null when the client has no retirement plan
  fee_rate: the advisory fee in percent as a number (for example 0.5); advisory accounts only
rules:
  - Current-state claims only: what is true on the prep date. Do not add "as_of" and do not write history claims.
  - At most one observation per attribute. Leave an attribute out when the material does not support a value.
  - basis: the ids from the material the claim rests on (N-..., E-..., M-..., FEE-..., account ids).

DISCLOSURES
{disclosure_rule}
"""

DISCLOSURE_RULES = {
    "no_harness": (
        "Every prep carries the approved disclosures listed in the material, one claim each: "
        '{"attribute": "disclosure", "value": "<claim id>", "kind": "disclosure", "text": "<the disclosure sentence>"}.'
    ),
    "harness": (
        "Do not write disclosure claims or disclosure sentences. The system appends the approved disclosures "
        "(the locked compliance text) to the prep word for word after your reply."
    ),
}


# ---------------------------------------------------------------------------------------------------------------
# slots
# ---------------------------------------------------------------------------------------------------------------
def select_slots(data: dict, clients: Optional[list[str]] = None, limit: Optional[int] = None) -> list[tuple]:
    """(prep_id, client_id, date) from preps_baseline, filtered by client, first `limit` per client."""
    known = {c["client_id"] for c in data["clients"]}
    if clients:
        unknown = sorted(set(clients) - known)
        if unknown:
            raise ValueError(f"unknown client ids: {unknown} (known: {sorted(known)})")
    out, per_client = [], {}
    for prep_id, cid, day in cabinet.prep_slots(data):
        if clients and cid not in clients:
            continue
        if limit is not None and per_client.get(cid, 0) >= limit:
            continue
        per_client[cid] = per_client.get(cid, 0) + 1
        out.append((prep_id, cid, day))
    return out


# ---------------------------------------------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------------------------------------------
def _values_block(data: dict) -> str:
    vocab = data.get("vocabulary") or {}
    lines = []
    for attr, values in cabinet.vocabulary(data).items():
        desc = vocab.get(attr) if isinstance(vocab.get(attr), dict) else {}
        lines.append(f"  {attr}: one of " + ", ".join(values))
        for v in values:
            if desc.get(v):
                lines.append(f"      {v}: {desc[v]}")
    return "\n".join(lines)


def instructions(data: dict, cid: str, day: date, condition: str) -> str:
    acct = cabinet.accounts(data)[cid]
    return _INSTRUCTIONS.format(name=acct["name"], client_id=cid, day=day.isoformat(), values=_values_block(data),
                                disclosure_rule=DISCLOSURE_RULES[condition])


def _fields(doc: dict, skip: tuple) -> str:
    return ", ".join(f"{k}={doc[k]}" for k in sorted(doc) if k not in skip and doc[k] is not None)


def _instruments_held(data: dict, cid: str, day: date) -> set[str]:
    acct = cabinet.accounts(data)[cid]
    names = {h["instrument"] for h in acct["holdings"]}
    names |= {e.get("instrument") for e in data["events"] if e.get("client_id") == cid
              and e["type"] == "trade_buy" and e["date"] < day and e.get("instrument")}
    return names


def _market_for(data: dict, cid: str, day: date) -> list[dict]:
    held = _instruments_held(data, cid, day)
    out = []
    for m in sorted(data["market"], key=lambda m: (m["date"], m["event_id"])):
        if m["date"] >= day:
            continue
        if m["type"] == "news_on_holding" and m.get("instrument") in held:
            out.append(m)
        elif m["type"] == "index_move" and any(m.get("index", "\0") in h for h in held):
            out.append(m)
        elif m["type"] not in ("news_on_holding", "index_move"):
            out.append(m)          # market-wide items (a rate change)
    return out


def raw_material(data: dict, cid: str, day: date) -> str:
    """The client's raw visible material as of `day` (strictly before it), deterministic."""
    c = next(c for c in data["clients"] if c["client_id"] == cid)
    acct = cabinet.accounts(data)[cid]
    lines = ["MATERIAL (raw client file, as of the prep date)", "",
             f"Client {cid}: {c['name']}, age {c.get('age')}, tier {c.get('tier')}",
             f"Household: " + "; ".join(f"{h['name']} ({h.get('role')})" for h in c.get("household", [])),
             f"Account {acct['account_id']}: {acct['type']}; holders: {', '.join(acct['holders'])}; "
             f"advisory: {'yes' if acct['advisory'] else 'no'}",
             "Opening holdings: " + "; ".join(f"{h['instrument']} ${h['amount']:,}" for h in acct["holdings"]),
             "", "Notes (date, id, author: text):"]
    notes = [n for n in data["notes"] if n["client_id"] == cid and n["date"] < day]
    lines += [f"- {n['date'].isoformat()} {n['note_id']} {n['author']}: {n['text']}" for n in notes] or ["- (none)"]
    lines += ["", "Feed events (date, id, type, details):"]
    events = [e for e in data["events"] if e.get("client_id") == cid and e["date"] < day]
    lines += [f"- {e['date'].isoformat()} {e['event_id']} {e['type']}"
              + (f": {_fields(e, ('event_id', 'client_id', 'date', 'type'))}" if _fields(e, ('event_id', 'client_id', 'date', 'type')) else "")
              for e in events] or ["- (none)"]
    lines += ["", "Market items for the client's holdings (date, id, type, details):"]
    lines += [f"- {m['date'].isoformat()} {m['event_id']} {m['type']}: {_fields(m, ('event_id', 'date', 'type'))}"
              for m in _market_for(data, cid, day)] or ["- (none)"]
    lines += ["", "Reference data (fee table):"]
    for r in sorted(data["reference_data"], key=lambda r: r["ref_id"]):
        if r.get("valid_from") is not None and r["valid_from"] > day:
            continue
        to = r["valid_to"].isoformat() if r.get("valid_to") else "open"
        lines.append(f"- {r['ref_id']}: {r.get('item')} {r.get('value_pct')}% valid {r['valid_from'].isoformat()} to {to}")
    lines += ["", "Approved disclosures (compliance; claim id: text):"]
    lines += [f"- {a['claim_id']}: {a['text']}" for a in sorted(data["approved_language"], key=lambda a: a["claim_id"])]
    return "\n".join(lines)


def compiled_facts(data: dict, facts: list[dict], cid: str, day: date, policy: dict) -> list[str]:
    """The harness's current-state facts for (client, day): value, source, author, date, expiry, status."""
    acct = cabinet.accounts(data)[cid]
    days = policy.get("label_expiry_days")
    out = []
    for attr in cabinet.STATE_ATTRS:
        st = cabinet.current_state(facts, cid, attr, day, policy)
        f = st["fact"]
        if f is None:
            out.append(f"- {attr}: nothing on file")
            continue
        since = "opening" if f["valid_from"] == cabinet._SINCE_OPENING else f["valid_from"].isoformat()
        expiry = (f["valid_from"] + timedelta(days=days)).isoformat() if f["label"] and days is not None else "none"
        if f.get("expired"):
            status = "EXPIRED: do not state it; reconfirm on the call"
        elif st["contradicted"]:
            status = f"CONTRADICTED by {', '.join(st['evidence'])}: current value {cabinet._fmt_value(st['value']) if st['value'] is not None else 'unknown'}"
        else:
            status = "current"
        shown = "no plan" if f["value"] is None else cabinet._fmt_value(f["value"])
        out.append(f"- {attr}: {shown} | source {f['source_id']} ({f['kind']}) | author {f['author']} | "
                   f"since {since} | expires {expiry} | {status}")
    if acct["advisory"]:
        refs = [f for f in facts if f["attribute"] == "fee_rate" and f["author"] == "reference"
                and f["valid_from"] <= day and (f["valid_to"] is None or day <= f["valid_to"])]
        fee = cabinet._latest(refs)
        if fee:
            to = fee["valid_to"].isoformat() if fee["valid_to"] else "open"
            out.append(f"- fee_rate: {fee['value']} | source {fee['source_id']} (reference table) | author reference | "
                       f"valid {fee['valid_from'].isoformat()} to {to} | current")
    return out


def harness_material(data: dict, facts: list[dict], cid: str, day: date, prep_id: str) -> str:
    """The harness's compiled client file for (client, day): facts, then the file with its questions and flags."""
    policy = cabinet.HARNESS_POLICY
    prep = cabinet.write_prep(data, facts, cid, day, policy, prep_id)
    file_text = prep["text"].split("\n\nDisclosures:")[0]
    lines = ["MATERIAL (the harness's compiled client file, as of the prep date)", "",
             "Current-state facts (ranked sources: banker notes, the client's own words and actions, the account "
             "record and the dated reference table outrank a junior's assumption; the assistant's summaries rank "
             "last; behaviour labels expire):"]
    lines += compiled_facts(data, facts, cid, day, policy)
    lines += ["", "Compiled client file (with the questions and watch-outs the harness derived):", file_text]
    return "\n".join(lines)


def build_prompt(data: dict, facts: list[dict], cid: str, day: date, prep_id: str, condition: str) -> str:
    if condition == "no_harness":
        material = raw_material(data, cid, day)
    elif condition == "harness":
        material = harness_material(data, facts, cid, day, prep_id)
    else:
        raise ValueError(f"unknown condition {condition!r}")
    return instructions(data, cid, day, condition) + "\n" + material + "\n"


# ---------------------------------------------------------------------------------------------------------------
# replies -> preps
# ---------------------------------------------------------------------------------------------------------------
def clean_claims(raw: Any, data: dict, cid: str, condition: str) -> tuple[list[dict], int, int]:
    """(valid claims, malformed dropped, model-written disclosures dropped). Harness: model disclosures are dropped."""
    if not isinstance(raw, list):
        return [], (0 if raw is None else 1), 0
    kept, malformed, dropped_disclosures = [], 0, 0
    for c in raw:
        if not isinstance(c, dict) or "as_of" in c:
            malformed += 1
            continue
        attr, kind, val = c.get("attribute"), c.get("kind"), c.get("value")
        if (attr == "disclosure") != (kind == "disclosure"):
            malformed += 1
            continue
        if attr == "disclosure" and not isinstance(val, str):
            malformed += 1
            continue
        if attr == "fee_rate" and isinstance(val, bool):
            malformed += 1
            continue
        claim = {"attribute": attr, "value": val, "kind": kind}
        basis = c.get("basis")
        claim["basis"] = [b for b in basis if isinstance(b, str)] if isinstance(basis, list) else []
        if kind == "disclosure":
            claim["text"] = c.get("text")
        probe = {"prep_id": "-", "client_id": cid, "date": "-", "text": "", "claims": [claim]}
        if cabinet.validate_claims(probe, data):
            malformed += 1
            continue
        if kind == "disclosure" and condition == "harness":
            dropped_disclosures += 1
            continue
        kept.append(claim)
    return kept, malformed, dropped_disclosures


def parse_code_owned(spec) -> tuple[str, ...]:
    """'disclosures' | 'disclosures,decision_maker' (or a list) -> the claims code writes in the harness arm, in
    CODE_OWNABLE order. Disclosures are always code-owned there (the harness prompt tells the model not to write any)."""
    parts = [p.strip() for p in (spec.split(",") if isinstance(spec, str) else spec) if p and p.strip()]
    names = {"disclosure" if p == "disclosures" else p for p in parts}
    unknown = sorted(names - set(CODE_OWNABLE))
    if unknown:
        raise ValueError(f"unknown code-owned claims: {unknown} (known: disclosures, decision_maker)")
    if "disclosure" not in names:
        raise ValueError("disclosures are always code-owned in the harness arm")
    return tuple(a for a in CODE_OWNABLE if a in names)


def harness_owned_claims(data: dict, facts: list[dict], slot: tuple) -> dict[str, dict]:
    """attribute -> the code-written harness claim that code guarantees for this slot, beyond the disclosures: the
    decision maker when brief_both_holders_on_conflict resolves a conflict on a joint account (a list of holders)."""
    prep_id, cid, day = slot
    prep = cabinet.write_prep(data, facts, cid, day, cabinet.HARNESS_POLICY, prep_id)
    out = {}
    for c in prep["claims"]:
        if c["attribute"] == "decision_maker" and c["kind"] == "observation" and isinstance(c["value"], list):
            out["decision_maker"] = dict(c, basis=list(c["basis"]), value=list(c["value"]))
    return out


def finalize_prep(data: dict, slot: tuple, condition: str, reply: dict, code_owned=DEFAULT_CODE_OWNED,
                  facts: Optional[list[dict]] = None, owned: Optional[dict] = None) -> dict:
    """A scorer-format prep from one reply: validated claims; harness adds the locked disclosures (claims + text)
    and, when `code_owned` has decision_maker and the harness briefs both holders, replaces the model's
    decision_maker observation with the harness's (claim + one text line). `code_owned` on the prep lists the claims
    code wrote; the no-harness arm is untouched (code_owned [])."""
    prep_id, cid, day = slot
    text = reply.get("text") if isinstance(reply, dict) else None
    text = text if isinstance(text, str) else ""
    claims, malformed, dropped = clean_claims(reply.get("claims") if isinstance(reply, dict) else None,
                                              data, cid, condition)
    wrote: list[str] = []
    replaced: list = []
    if condition == "harness":
        code_owned = parse_code_owned(code_owned)
        if "decision_maker" in code_owned:
            if owned is None:
                owned = harness_owned_claims(data, facts if facts is not None else cabinet.build_facts(data), slot)
            dm = owned.get("decision_maker")
            if dm is not None:
                replaced = [c["value"] for c in claims if c["attribute"] == "decision_maker" and c["kind"] == "observation"]
                claims = [c for c in claims if not (c["attribute"] == "decision_maker" and c["kind"] == "observation")]
                claims.append(dict(dm, basis=list(dm["basis"]), value=list(dm["value"])))
                text = text.rstrip() + f"\n\nDecision maker: brief both holders ({', '.join(dm['value'])})."
                wrote.append("decision_maker")
        locked = cabinet.locked_disclosures(data)
        ids = sorted(locked)
        claims += [{"attribute": "disclosure", "value": i, "kind": "disclosure", "text": locked[i]} for i in ids]
        text = text.rstrip() + "\n\nDisclosures:\n" + "\n".join(f"- {locked[i]} ({i})" for i in ids)
        wrote.insert(0, "disclosure")
    return {"prep_id": prep_id, "client_id": cid, "date": day.isoformat(), "text": text, "claims": claims,
            "malformed_claims": malformed, "model_disclosures_dropped": dropped, "code_owned": wrote,
            "model_decision_maker_replaced": replaced}


# ---------------------------------------------------------------------------------------------------------------
# the experiment
# ---------------------------------------------------------------------------------------------------------------
def _ask(llm, system: str, prompt: str, attempts: int) -> dict:
    last = None
    for _ in range(max(1, attempts)):
        try:
            return llm.complete_json(ROLE, system, prompt, max_tokens=MAX_TOKENS)
        except Exception as exc:   # an LLMError (CLI failure, bad JSON after retry, replay miss) or a transport error
            last = exc
    raise last


def run_experiment(data: dict, llm, runs: int = 2, clients: Optional[list[str]] = None, limit: Optional[int] = None,
                   concurrency: int = 4, conditions: tuple = CONDITIONS, attempts: int = 2,
                   code_owned=DEFAULT_CODE_OWNED) -> dict:
    """Ask the model for every (condition, slot), `runs` times. Runs go one after another (so a replay serves each
    run's answers in recorded order); within a run, calls go through a thread pool of `concurrency` workers.
    `code_owned`: the claims code writes in the harness arm (post-processing; see finalize_prep)."""
    if getattr(llm, "is_fake", False):
        raise RuntimeError("cabinet-live needs a model: set PREGAME_LLM_MODE to live, record or replay")
    conditions = tuple(conditions)
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown or not conditions:
        raise ValueError(f"unknown conditions {unknown} (known: {list(CONDITIONS)})")
    code_owned = parse_code_owned(code_owned)
    slots = select_slots(data, clients, limit)
    if not slots:
        raise ValueError("no prep slots selected")
    facts = cabinet.build_facts(data)
    owned = {s[0]: harness_owned_claims(data, facts, s) for s in slots} if "harness" in conditions else {}
    prompts = {(cond, s[0]): build_prompt(data, facts, s[1], s[2], s[0], cond) for cond in conditions for s in slots}
    mode = getattr(getattr(llm, "settings", None), "llm_mode", None)
    if mode == "replay":
        attempts = 1            # a replay miss would only miss again (and a hit must not be consumed twice)
    results: dict[tuple[str, int], dict] = {}
    for run in range(1, runs + 1):
        jobs = [(cond, s) for cond in conditions for s in slots]
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [(cond, s, pool.submit(_ask, llm, SYSTEM, prompts[(cond, s[0])], attempts)) for cond, s in jobs]
            for cond in conditions:
                results[(cond, run)] = {"condition": cond, "run_index": run, "preps": [], "errors": []}
            for cond, s, fut in futures:
                try:
                    reply = fut.result()
                except Exception as exc:
                    results[(cond, run)]["errors"].append({"prep_id": s[0], "error": f"{type(exc).__name__}: "
                                                                                    f"{str(exc)[:200]}"})
                    continue
                results[(cond, run)]["preps"].append(finalize_prep(data, s, cond, reply, code_owned, facts,
                                                                   owned.get(s[0])))
    return {"slots": slots, "runs": runs, "conditions": list(conditions), "model": llm.model_id(ROLE),
            "mode": mode, "results": results, "prompts": prompts, "code_owned": list(code_owned)}


def paired_preps(experiment: dict, run: int) -> dict[str, list[dict]]:
    """Per condition, the run's preps restricted to slots every condition answered (so both columns grade the same
    slots even when a call failed)."""
    res = experiment["results"]
    ok = None
    for cond in experiment["conditions"]:
        ids = {p["prep_id"] for p in res[(cond, run)]["preps"]}
        ok = ids if ok is None else ok & ids
    return {cond: [p for p in res[(cond, run)]["preps"] if p["prep_id"] in ok] for cond in experiment["conditions"]}


def score_experiment(experiment: dict, scorer=None) -> dict[tuple[str, int], dict]:
    """(condition, run) -> the scorer's aggregate summary plus this module's own counts. `scorer` defaults to
    cabinet.score (the subprocess)."""
    scorer = scorer or cabinet.score
    out = {}
    for run in range(1, experiment["runs"] + 1):
        paired = paired_preps(experiment, run)
        for cond, preps in paired.items():
            s = dict(scorer(preps)) if preps else {"preps": 0}
            s["claims_kept"] = sum(len(p["claims"]) for p in preps)
            s["malformed_claims"] = sum(p["malformed_claims"] for p in preps)
            s["model_disclosures_dropped"] = sum(p["model_disclosures_dropped"] for p in preps)
            s["call_errors"] = len(experiment["results"][(cond, run)]["errors"])
            s["code_owned_by_claim"] = {a: sum(1 for p in preps if a in p.get("code_owned", ()))
                                        for a in CODE_OWNABLE}
            out[(cond, run)] = s
    return out


def code_owned_note(experiment: dict) -> str:
    """One line for the table footer: which claims code wrote in the harness arm, and on which preps."""
    if "harness" not in experiment["conditions"]:
        return "code-owned claims: none (the harness arm was not run)"
    setting = experiment.get("code_owned") or ["disclosure"]
    per = {}
    for run in range(1, experiment["runs"] + 1):
        for p in experiment["results"][("harness", run)]["preps"]:
            for a in p.get("code_owned", ()):
                per.setdefault(a, set()).add(p["prep_id"])
    parts = []
    for a in setting:
        ids = sorted(per.get(a, ()))
        where = "every prep" if a == "disclosure" else (f"preps {', '.join(ids)}" if ids else "no prep (no conflict)")
        parts.append(f"{a} ({where})")
    return (f"code-owned claims (harness arm, written by code after the model's reply; --code-owned "
            f"{','.join('disclosures' if a == 'disclosure' else a for a in setting)}): " + "; ".join(parts)
            + "; no-harness arm: none")


def reference_scores(data: dict, slots: list[tuple], scorer=None, baseline_scorer=None) -> dict[str, dict]:
    """Deterministic context: the no-harness assistant baseline (all 24 slots) and the code-written harness preps on
    the selected slots."""
    scorer = scorer or cabinet.score
    baseline_scorer = baseline_scorer or cabinet.score_baseline
    facts = cabinet.build_facts(data)
    code = [cabinet.write_prep(data, facts, cid, day, cabinet.HARNESS_POLICY, pid) for pid, cid, day in slots]
    return {"baseline": baseline_scorer(), "code_harness": scorer(code)}


# ---------------------------------------------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------------------------------------------
def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def _rows(sets: list[dict]) -> list[tuple[str, Any]]:
    """(label, fn(summary) -> (value, denominator or None, style) or None) -- the `cabinet` command's measures."""
    def get(key):
        return lambda s: (s[key], s.get("preps"), "pct") if s.get(key) is not None else None

    def by(field, key):
        return lambda s: ((s.get(field) or {}).get(key, 0), None, "n") if "preps" in s and s.get("preps") else None

    def recall(s):
        if s.get("expected_actions") is None:
            return None
        return s.get("actions_hit", 0), s["expected_actions"], "frac"

    def act(k):
        return lambda s: (((s.get("actions_hit_by_type") or {}).get(k, 0), (s.get("expected_actions_by_type") or {}).get(k, 0), "frac")
                          if s.get("expected_actions") is not None else None)

    def plain(key):
        return lambda s: (s[key], None, "n") if s.get(key) is not None else None

    rows = [("preps with a fault", get("preps_with_a_fault")),
            ("  ignoring warnings", get("preps_with_a_fault_ignoring_warnings")),
            ("faults (total)", plain("faults_total"))]
    types = sorted({t for s in sets for t in (s.get("faults_by_type") or {})})
    rows += [(f"  {t}", by("faults_by_type", t)) for t in types]
    rows += [("forbidden promises", plain("forbidden_promises")), ("expected actions done", recall)]
    kinds = sorted({k for s in sets for k in (s.get("expected_actions_by_type") or {})})
    rows += [(f"  {k}", act(k)) for k in kinds]
    rows += [("unneeded questions", plain("questions_unneeded")),
             ("claims kept (with discl.)", plain("claims_kept")),
             ("malformed claims dropped", plain("malformed_claims")),
             ("model calls failed", plain("call_errors"))]
    owned = [a for a in CODE_OWNABLE if any(a in (s.get("code_owned_by_claim") or {}) for s in sets)]
    if owned:
        rows += [(f"code-owned {a}", (lambda a: lambda s: ((s.get("code_owned_by_claim") or {}).get(a, 0), None, "n")
                             if s.get("code_owned_by_claim") is not None else None)(a)) for a in owned]
    return rows


def _fmt(cell) -> str:
    if cell is None:
        return "-"
    v, d, style = cell
    if style == "pct":
        return f"{_num(v)}/{_num(d)} ({round(100 * v / d)}%)" if d else _num(v)
    if style == "frac":
        return f"{_num(v)}/{_num(d)}"
    return _num(v)


def _mean(cells: list) -> Optional[tuple]:
    cells = [c for c in cells if c is not None]
    if not cells:
        return None
    v = sum(c[0] for c in cells) / len(cells)
    ds = [c[1] for c in cells if c[1] is not None]
    d = sum(ds) / len(ds) if ds else None
    return v, d, cells[0][2]


def table(scores: dict[tuple[str, int], dict], refs: Optional[dict] = None, runs: Optional[int] = None,
          width: int = 13) -> tuple[list[str], list[list]]:
    """(printable lines, rows of raw cells). Columns: per condition, run 1..n and the mean; then the references."""
    runs = runs or max(r for _, r in scores)
    conds = [c for c in CONDITIONS if any(k[0] == c for k in scores)]
    short = {"no_harness": "no-h", "harness": "harn"}
    heads, cols = [], []
    for cond in conds:
        for r in range(1, runs + 1):
            heads.append(f"{short[cond]} #{r}")
            cols.append(("run", scores.get((cond, r))))
        heads.append(f"{short[cond]} mean")
        cols.append(("mean", [scores.get((cond, r)) for r in range(1, runs + 1)]))
    for name, label in (("baseline", "baseline"), ("code_harness", "code harn")):
        if refs and name in refs:
            heads.append(label)
            cols.append(("run", refs[name]))
    all_sets = [s for s in scores.values() if s] + [s for s in (refs or {}).values() if s]
    lines = [f"{'measure':<26}" + "".join(f"{h:>{width}}" for h in heads)]
    raw_rows = []
    for label, fn in _rows(all_sets):
        cells = []
        for kind, col in cols:
            if kind == "run":
                cells.append(fn(col) if col else None)
            else:
                cells.append(_mean([fn(s) if s else None for s in col]))
        raw_rows.append([label] + cells)
        lines.append(f"{label:<26}" + "".join(f"{_fmt(c):>{width}}" for c in cells))
    return lines, raw_rows


# ---------------------------------------------------------------------------------------------------------------
# storage (through cabinet.store_run, which refuses any database not named pregame*)
# ---------------------------------------------------------------------------------------------------------------
def store_experiment(db, experiment: dict, scores: dict, data_source: str, now: Optional[datetime] = None) -> list[dict]:
    """One cabinet_runs receipt per (condition, run), its scored preps in cabinet_preps; returns the receipts."""
    now = now or datetime.now(timezone.utc)
    receipts = []
    n = 0
    for run in range(1, experiment["runs"] + 1):
        paired = paired_preps(experiment, run)
        for cond in experiment["conditions"]:
            preps = paired[cond]
            policy = dict(cabinet.HARNESS_POLICY, name="live_harness") if cond == "harness" \
                else {"name": "live_no_harness"}
            stamp = now + timedelta(microseconds=n)     # distinct run ids even inside one clock tick
            n += 1
            receipt = cabinet.store_run(db, [{k: p[k] for k in ("prep_id", "client_id", "date", "text", "claims")}
                                             for p in preps], policy, scores[(cond, run)], data_source, now=stamp)
            extra = {"condition": cond, "run_index": run, "model": experiment["model"], "role": ROLE,
                     "llm_mode": experiment.get("mode"),
                     "call_errors": experiment["results"][(cond, run)]["errors"],
                     "malformed_claims": sum(p["malformed_claims"] for p in preps),
                     "code_owned": list(experiment.get("code_owned") or ["disclosure"]) if cond == "harness" else []}
            db[cabinet.RUNS_COLLECTION].update_one({"_id": receipt["_id"]}, {"$set": extra})
            db[cabinet.PREPS_COLLECTION].update_many({"run_id": receipt["_id"]},
                                                     {"$set": {"condition": cond, "run_index": run,
                                                               "model": experiment["model"]}})
            for p in preps:
                db[cabinet.PREPS_COLLECTION].update_one(
                    {"run_id": receipt["_id"], "prep_id": p["prep_id"]},
                    {"$set": {"code_owned": list(p.get("code_owned", [])),
                              "model_decision_maker_replaced": p.get("model_decision_maker_replaced", [])}})
            receipt.update(extra)
            receipts.append(receipt)
    return receipts


def prompt_digest(experiment: dict) -> str:
    """A short fingerprint of every prompt (for the operator: two invocations asked the same questions)."""
    import hashlib
    blob = json.dumps(sorted([f"{c}|{p}" for (c, p) in experiment["prompts"]]) +
                      [experiment["prompts"][k] for k in sorted(experiment["prompts"])])
    return hashlib.sha256((SYSTEM + blob).encode("utf-8")).hexdigest()[:12]
