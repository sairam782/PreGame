#!/usr/bin/env python3
"""Score call preps against the banker-sim answer side.

Grades any set of preps with the rules the data's generator uses to build its answer key, so a harness's preps and
the no-harness baseline are scored the same way. This script reads the HIDDEN truth: the harness must never import
it or read its output while it is drafting or improving.

Data versions (told apart by the files, never by folder names):
  version 2  data/banker_sim_small/out: 6 clients, 2 approved sentences, both required in every prep.
  version 3  any folder with hidden/splits.json or approved_language entries that carry applies_to
             (data/pregamev0_abhi_cabinet20/out, data/pregamev0_abhi_cabinet80/out, data/banker_sim_v3/out):
             adds contact_preference, life events, up to 10 approved sentences of which each prep needs only some,
             and a tune/test split by client.
  (A folder whose answer key has no "expected" list is version 1; its expected actions are inferred.)
Drift severity comes from the data's own generator, <data>/../gen.py (its LADDER), when that file exists; otherwise
from the answer key's compliance_drift faults (claimed id, claimed_text, severity), with the approved text as step 0.

Input (harness mode): a JSON list of preps, one per client per prep date.
    {"prep_id": "H-0001", "client_id": "C01", "date": "2026-07-18", "text": "...",
     "claims": [
        {"attribute": "risk_attitude", "value": "growth", "kind": "observation", "basis": ["E-0320"]},
        {"attribute": "risk_attitude", "value": "cautious", "kind": "observation", "as_of": "2026-05-13", "basis": ["N-0010"]},
        {"attribute": "investing_style", "value": null, "kind": "question", "basis": ["E-0327"]},
        {"attribute": "investing_style", "value": null, "kind": "flag", "basis": ["N-0021"]},
        {"attribute": "decision_maker", "value": ["Tom Brennan", "Lisa Brennan"], "kind": "observation"},
        {"attribute": "contact_preference", "value": "email_only", "kind": "observation", "basis": ["E-0101"]},
        {"attribute": "life_event", "value": "E-0016", "kind": "question", "basis": ["E-0016"]},
        {"attribute": "disclosure", "value": "AS-01", "kind": "disclosure",
         "text": "Past performance is not indicative of future results."}]}
  attribute: risk_attitude | investing_style | decision_maker | retirement_date | fee_rate | disclosure
             | contact_preference (v3) | life_event (v3)
  Values: see the data's visible/vocabulary.json.
  as_of (the data's rule): a claim with as_of is history, scored against the truth on as_of; without it, the claim
    is the prep's current view, scored against the truth on the prep date. Applies to labels, retirement and fee.
  contact_preference (v3): a label like risk_attitude (phone_ok | email_only), scored against the truth; a wrong one
    is a sticky_label fault. Version 2 data has no contact preference, so there it is not scored.
  decision_maker as a list: correct when it includes the true decision maker (version 3, as its generator: and names
    at least two people).
  life_event (v3): {"attribute": "life_event", "value": "<event_id>"} of any kind, a question included, means the
    prep mentions that feed event. A mentioned id that is not one of the client's life events dated on or before the
    claim's date (as_of, else the prep date) is a missed_life_event fault. Separately, every life event of the client
    in the 90 days up to and including the prep date (prep date - 90 < event date <= prep date) that the prep does
    not mention is a missed_life_event fault. A life_event claim with value null mentions nothing and is never a fault.
  Disclosures: any disclosure given, required or not, must be the approved sentence word for word, or it is
    compliance_drift (severity 1 is a warning, not a fault). A required sentence left out is missing_disclosure,
    a type this scorer adds (the answer key does not have it). Which are required:
      version 2  every approved sentence, in every prep.
      version 3  per prep, from approved_language applies_to / required_when, on holdings worked out from VISIBLE
                 data only: start holdings in clients.json plus trade_buy / trade_sell events dated on or before the
                 prep date (cash moves the opposite way; only positive positions count).
                   "always" / applies_to all        required in every prep
                   an instrument type (fund, index_fund, bond_fund, single_stock, managed, cash)
                                                    required when the client holds one on the prep date
                   advisory                         required when the account's advisory flag is set
                   multi_asset                      required when two or more non-cash instrument types are held
                   required_when "never ..."        never required (AS-10 is optional); so is an unknown tag
  Actions the answer key expects (answer_key.json "expected"):
    ask        -> a claim of kind "question" on that attribute (v3: attribute life_event for a due life event)
    flag       -> a claim of kind "flag" on that attribute (a "No changes" note conflicts with what the client did;
                  v3: the first attribute that changed, life_event included)
    brief_both -> decision_maker given as a list that includes the true decision maker (both holders)
  Questions and flags assert nothing, so they are never value faults (a life_event question naming an event that is
  not the client's is the one exception).
  Splits (v3): each prep result carries its client's split (hidden/splits.json), and the summary adds
    by_split = {"tune": <the same summary on tune clients' preps>, "test": <the same on test clients' preps>}.

Usage:
    python score_preps.py --baseline                       # the preps written with no harness (data v2)
    python score_preps.py harness_preps.json               # a harness's preps
    python score_preps.py harness_preps.json --json results/harness.json
    python score_preps.py --baseline --data data/pregamev0_abhi_cabinet20/out
"""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "data" / "banker_sim_small" / "out"

LABEL_ATTRS = ["risk_attitude", "investing_style", "decision_maker"]
TRUTH_ATTRS = LABEL_ATTRS + ["retirement_date"]          # data v2; v3 adds OPTIONAL_TRUTH_ATTRS (AnswerSide.truth_attrs)
OPTIONAL_TRUTH_ATTRS = ["contact_preference"]            # scored when the data's truth has it (v3)
FAULT_BY_ATTR = {"risk_attitude": "sticky_label", "investing_style": "said_vs_did",
                 "decision_maker": "wrong_decision_maker", "retirement_date": "stale_overwrite",
                 "contact_preference": "sticky_label"}
HUMAN_AUTHORS = {"banker", "junior"}
STATED_KINDS = {"observation", "label", "plan"}
ACTION_KIND = {"ask": "question", "flag": "flag"}
LIFE_WINDOW_DAYS = 90                                    # v3 default; the data's generator or vocabulary can say otherwise
SPLITS = ("tune", "test")
INSTRUMENT_TAGS = {"fund", "index_fund", "bond_fund", "single_stock", "managed", "cash"}

D = date.fromisoformat


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------------------------------ the data's generator
_GENERATORS = {}


def _is_main_guard(node):
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__")


def load_generator(data):
    """The data folder's own generator, <data>/../gen.py, as a module with a unique name; None when absent or broken.

    Importing is safe when its top level only imports, assigns and defines (main() behind if __name__ == "__main__",
    as in every gen.py so far). Otherwise only those top-level statements run, so a generator that would build or
    write at import time does not. Nothing it prints reaches stdout, and no bytecode is written into the data folder."""
    path = Path(data).resolve().parent / "gen.py"
    key = str(path)
    if key in _GENERATORS:
        return _GENERATORS[key]
    mod = None
    if path.is_file():
        name = "_score_preps_gen_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        write_bytecode = sys.dont_write_bytecode
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=key)
            keep = [n for n in tree.body
                    if isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                      ast.Assign, ast.AnnAssign))
                    or _is_main_guard(n) or (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            sys.dont_write_bytecode = True
            with contextlib.redirect_stdout(io.StringIO()):
                if len(keep) == len(tree.body):
                    spec.loader.exec_module(mod)
                else:                                     # not import-safe: run only definitions and assignments
                    tree.body = keep
                    exec(compile(tree, key, "exec"), mod.__dict__)
        except Exception:
            sys.modules.pop(name, None)
            mod = None
        finally:
            sys.dont_write_bytecode = write_bytecode
    _GENERATORS[key] = mod
    return mod


def ladder_of(gen):
    ladder = getattr(gen, "LADDER", None)
    return ladder if isinstance(ladder, dict) else {}


LADDER = ladder_of(load_generator(DEFAULT_DATA))   # the default data's ladder, for older readers; AnswerSide has its own


def norm(text):
    return " ".join((text or "").split())


def severity_of(claim_id, text, ladder=None):
    """Step of `text` on the drift ladder of `claim_id` (0 = exact), or None when the ladder does not list it."""
    for i, s in enumerate((LADDER if ladder is None else ladder).get(claim_id, [])):
        if norm(s) == norm(text):
            return i
    return None  # reworded in a way the ladder does not list


# ------------------------------------------------------------------------------------------ the answer side
class AnswerSide:
    def __init__(self, root):
        root = Path(root)
        self.root = root
        self.truth = load(root / "hidden" / "truth.json")
        self.claims = load(root / "hidden" / "claims.json")
        self.notes = {n["note_id"]: n for n in load(root / "visible" / "notes.json")}
        self.reference = load(root / "visible" / "reference_data.json")
        approved = load(root / "visible" / "approved_language.json")
        self.approved = {a["claim_id"]: a["text"] for a in approved}
        self.approved_rules = {a["claim_id"]: (a.get("applies_to"), a.get("required_when")) for a in approved}
        self.preps_baseline = load(root / "visible" / "preps_baseline.json")
        self.answer_key = load(root / "hidden" / "answer_key.json")
        self.expected = defaultdict(list)          # prep_id -> [(attribute, action)]
        self.expected_source = "answer key" if self.answer_key.get("expected") else "inferred (data v1 has no expected list)"
        for row in self.answer_key.get("expected", []):
            self.expected[row["prep_id"]].append((row["attribute"], row["action"]))

        split_file = next((p for p in (root / "hidden" / "splits.json", root / "visible" / "splits.json") if p.exists()), None)
        self.splits = {s["client_id"]: s["split"] for s in load(split_file)} if split_file else {}
        if split_file or any("applies_to" in a for a in approved):
            self.version = 3
        else:
            self.version = 2 if self.answer_key.get("expected") else 1
        self.truth_attrs = TRUTH_ATTRS + [a for a in OPTIONAL_TRUTH_ATTRS if any(a in t for t in self.truth.values())]
        self.life_events = {cid: t.get("life_events", []) for cid, t in self.truth.items()}

        gen = load_generator(root)
        self.severity = {}                         # (claim_id, normalised text) -> drift step
        if ladder_of(gen):
            self.ladder_source = "gen.py"
            for cid, steps in ladder_of(gen).items():
                for i, s in enumerate(steps):
                    self.severity.setdefault((cid, norm(s)), i)
        elif any(f.get("fault_type") == "compliance_drift" for f in self.answer_key.get("faults", [])):
            self.ladder_source = "answer key"
            for cid, text in self.approved.items():
                self.severity[(cid, norm(text))] = 0
            for f in self.answer_key["faults"]:
                if f.get("fault_type") == "compliance_drift" and f.get("severity") is not None:
                    self.severity.setdefault((f["claimed"], norm(f.get("claimed_text"))), f["severity"])
        else:
            self.ladder_source = None
        self.life_window_days = LIFE_WINDOW_DAYS
        if isinstance(getattr(gen, "LIFE_WINDOW_DAYS", None), int):
            self.life_window_days = gen.LIFE_WINDOW_DAYS
        elif (root / "visible" / "vocabulary.json").exists():
            rule = str(load(root / "visible" / "vocabulary.json").get("_rules", {}).get("life_events", ""))
            m = re.search(r"(\d+) days", rule)
            if m:
                self.life_window_days = int(m.group(1))

        if self.version >= 3:                      # visible data for the required disclosures
            clients = load(root / "visible" / "clients.json")
            self.clients = {c["client_id"]: c for c in clients}
            self.instrument_types = {"Cash": "cash"}
            for c in clients:
                for h in c.get("holdings", []):
                    self.instrument_types.setdefault(h["instrument"], h.get("instrument_type"))
            self.trades = defaultdict(list)
            for e in load(root / "visible" / "events.json"):
                if e.get("type") in ("trade_buy", "trade_sell"):
                    self.trades[e["client_id"]].append(e)
                    if e.get("instrument_type"):
                        self.instrument_types.setdefault(e["instrument"], e["instrument_type"])

    def truth_at(self, cid, attr, day):
        val = None
        for step in self.truth[cid].get(attr, []):
            if D(step["since"]) <= day:
                val = step["value"]
        return val

    def fee_at(self, day):
        for r in self.reference:
            if r["item"] == "advisory_fee" and D(r["valid_from"]) <= day and (
                    r["valid_to"] is None or day <= D(r["valid_to"])):
                return r["value_pct"], r["ref_id"]
        return None, None

    def severity_of(self, claim_id, text):
        return self.severity.get((claim_id, norm(text)))

    def holdings_at(self, cid, day):
        """Positions on `day` from visible data: start holdings plus trades dated on or before it, cash moving the
        other way (the generator's holdings_at). Only positive positions count."""
        h = {}
        for x in self.clients[cid].get("holdings", []):
            h[x["instrument"]] = h.get(x["instrument"], 0) + x["amount"]
        for e in self.trades.get(cid, []):
            if D(e["date"]) <= day:
                sign = 1 if e["type"] == "trade_buy" else -1
                h[e["instrument"]] = h.get(e["instrument"], 0) + sign * e["amount"]
                h["Cash"] = h.get("Cash", 0) - sign * e["amount"]
        return {k: v for k, v in h.items() if v > 0}

    def required_disclosures(self, cid, day):
        """Approved claim ids the prep for `cid` on `day` must carry (v2: all of them; v3: see the module doc)."""
        if self.version < 3:
            return list(self.approved)
        kinds = {self.instrument_types.get(k, "single_stock") for k in self.holdings_at(cid, day)}
        advisory = bool(self.clients[cid]["accounts"][0].get("advisory"))
        out = []
        for claim_id, (tags, when) in self.approved_rules.items():
            tags, when = set(tags or []), str(when or "").strip().lower()
            if when.startswith("never"):
                continue
            if when == "always" or "all" in tags or (tags & INSTRUMENT_TAGS & kinds) \
                    or ("advisory" in tags and advisory) or ("multi_asset" in tags and len(kinds - {"cash"}) > 1):
                out.append(claim_id)
        return out

    def life_events_due(self, cid, day):
        """The client's life events a prep on `day` must raise: day - window < event date <= day."""
        lo = day - timedelta(days=self.life_window_days)
        return [e for e in self.life_events.get(cid, []) if lo < D(e["date"]) <= day]

    def life_event_ok(self, cid, event_id, day):
        return any(e["event_id"] == event_id and D(e["date"]) <= day for e in self.life_events.get(cid, []))

    def last_human_statement(self, cid, attr, before):
        best = None
        for c in self.claims:
            if c["doc_type"] != "note" or c["client_id"] != cid or c["attribute"] != attr:
                continue
            if c["kind"] not in STATED_KINDS or D(c["date"]) >= before:
                continue
            note = self.notes.get(c["doc_id"])
            if not note or note["author"] not in HUMAN_AUTHORS:
                continue
            if best is None or c["date"] >= best["date"]:
                best = c
        return best

    def expected_for(self, prep):
        """(attribute, action) pairs the prep should carry: from the answer key when it has them, else inferred."""
        if self.answer_key.get("expected"):
            return sorted(set(self.expected.get(prep["prep_id"], [])))
        out = []
        day = D(prep["date"])
        for attr in TRUTH_ATTRS:
            stated = self.last_human_statement(prep["client_id"], attr, day)
            if stated is not None and stated["value"] != self.truth_at(prep["client_id"], attr, day):
                out.append((attr, "brief_both" if attr == "decision_maker" else "ask"))
        return out


# ------------------------------------------------------------------------------------------ scoring
def score_prep(side, prep):
    cid, prep_day = prep["client_id"], D(prep["date"])
    v3 = side.version >= 3
    faults, actions_done, history = [], set(), 0
    seen_disclosures, mentioned_events = set(), set()
    for c in prep.get("claims", []):
        attr, kind, val = c.get("attribute"), c.get("kind"), c.get("value")
        if v3 and attr == "life_event" and val is not None:
            # any kind, a question too: the prep mentions this event, which must be the client's, dated by the claim
            if isinstance(val, str):
                mentioned_events.add(val)
            if not side.life_event_ok(cid, val, D(c["as_of"]) if c.get("as_of") else prep_day):
                faults.append({"fault_type": "missed_life_event", "level": "fault", "attribute": attr, "claimed": val,
                               "truth": [e["event_id"] for e in side.life_events.get(cid, [])],
                               "as_of": c.get("as_of"), "basis": c.get("basis", [])})
        if kind == "question":
            actions_done.add((attr, "ask"))
            continue
        if kind == "flag":
            actions_done.add((attr, "flag"))
            continue
        day = D(c["as_of"]) if c.get("as_of") else prep_day
        if c.get("as_of"):
            history += 1
        if attr in side.truth_attrs:
            t = side.truth_at(cid, attr, day)
            if attr == "decision_maker" and isinstance(val, list):
                if t in val and (not v3 or len(val) > 1):
                    if not c.get("as_of"):
                        actions_done.add((attr, "brief_both"))
                    continue
                val = list(val)
            if val != t:
                faults.append({"fault_type": FAULT_BY_ATTR[attr], "level": "fault", "attribute": attr, "claimed": val,
                               "truth": t, "as_of": c.get("as_of"), "basis": c.get("basis", [])})
        elif attr == "fee_rate":
            t, ref = side.fee_at(day)
            if t is not None and (val is None or abs(float(val) - t) > 1e-9):
                faults.append({"fault_type": "stale_reference", "level": "fault", "attribute": attr, "claimed": val,
                               "truth": t, "reference": ref, "as_of": c.get("as_of"), "basis": c.get("basis", [])})
        elif attr == "disclosure":
            approved = side.approved.get(val)
            seen_disclosures.add(val)
            if approved is None or norm(c.get("text")) != norm(approved):
                sev = side.severity_of(val, c.get("text"))
                faults.append({"fault_type": "compliance_drift", "level": "warning" if sev == 1 else "fault",
                               "attribute": attr, "claimed": val, "claimed_text": c.get("text"), "truth": approved,
                               "severity": sev})
    for claim_id in side.required_disclosures(cid, prep_day):
        if claim_id not in seen_disclosures:
            faults.append({"fault_type": "missing_disclosure", "level": "fault", "attribute": "disclosure",
                           "claimed": None, "truth": side.approved.get(claim_id), "required": claim_id,
                           "note": "not in the answer key's types; added by this scorer"})
    if v3:
        for e in side.life_events_due(cid, prep_day):
            if e["event_id"] not in mentioned_events:
                faults.append({"fault_type": "missed_life_event", "level": "fault", "attribute": "life_event",
                               "claimed": None, "truth": e["event_id"], "event_date": e["date"],
                               "event_type": e.get("type")})
    expected = set(side.expected_for(prep))
    asked_only = {a for a in actions_done if a[1] == "ask"}
    result = {"prep_id": prep["prep_id"], "client_id": cid, "date": prep["date"], "faults": faults,
              "history_claims": history,
              "expected_actions": [list(x) for x in sorted(expected)],
              "actions_done": [list(x) for x in sorted(actions_done)],
              "actions_hit": [list(x) for x in sorted(expected & actions_done)],
              "questions_unneeded": [list(x) for x in sorted(asked_only - expected)]}
    if side.splits:
        result["split"] = side.splits.get(cid)
    return result


def summarize(results, expected_source=None, by_split=True):
    n = len(results)
    faulty = [r for r in results if r["faults"]]
    faulty_strict = [r for r in results if any(f["level"] == "fault" for f in r["faults"])]
    by_type = Counter(f["fault_type"] for r in results for f in r["faults"])
    by_client = defaultdict(lambda: [0, 0])
    for r in results:
        by_client[r["client_id"]][1] += 1
        if r["faults"]:
            by_client[r["client_id"]][0] += 1
    sev = Counter(f.get("severity") for r in results for f in r["faults"] if f["fault_type"] == "compliance_drift")
    exp = Counter(a[1] for r in results for a in r["expected_actions"])
    hit = Counter(a[1] for r in results for a in r["actions_hit"])
    total_exp, total_hit = sum(exp.values()), sum(hit.values())
    s = {
        "preps": n,
        "preps_with_a_fault": len(faulty),
        "trusted_prep_rate": round((n - len(faulty)) / n, 3) if n else None,
        "preps_with_a_fault_ignoring_warnings": len(faulty_strict),
        "trusted_prep_rate_ignoring_warnings": round((n - len(faulty_strict)) / n, 3) if n else None,
        "faults_total": sum(by_type.values()),
        "warnings_total": sum(1 for r in results for f in r["faults"] if f["level"] == "warning"),
        "faults_by_type": dict(sorted(by_type.items())),
        "faulty_preps_by_client": {c: f"{v[0]}/{v[1]}" for c, v in sorted(by_client.items())},
        "compliance_drift_by_severity": {str(k): v for k, v in sorted(sev.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))},
        "forbidden_promises": sev.get(4, 0),
        "history_claims": sum(r["history_claims"] for r in results),
        "expected_actions_source": expected_source,
        "expected_actions": total_exp,
        "expected_actions_by_type": dict(sorted(exp.items())),
        "actions_hit": total_hit,
        "actions_hit_by_type": dict(sorted(hit.items())),
        "action_recall": round(total_hit / total_exp, 3) if total_exp else None,
        "questions_unneeded": sum(len(r["questions_unneeded"]) for r in results),
        # kept for readers of data-v1 results: asks only
        "questions_expected": exp.get("ask", 0),
        "questions_hit": hit.get("ask", 0),
        "question_recall": round(hit.get("ask", 0) / exp["ask"], 3) if exp.get("ask") else None,
    }
    if by_split and any("split" in r for r in results):       # v3: the same summary per split (tune, test)
        names = list(SPLITS) + sorted({r["split"] for r in results if r.get("split") and r["split"] not in SPLITS})
        s["by_split"] = {name: summarize([r for r in results if r.get("split") == name], expected_source, by_split=False)
                         for name in names}
    return s


def baseline_preps(side):
    by_doc = defaultdict(list)
    for c in side.claims:
        if c["doc_type"] == "prep":
            by_doc[c["doc_id"]].append(c)
    return [dict(p, claims=by_doc[p["prep_id"]]) for p in side.preps_baseline]


def check_against_answer_key(side, results):
    """The baseline must reproduce the answer key's prep faults exactly, or this scorer has drifted from gen.py."""
    key = Counter((f["doc_id"], f["fault_type"], f.get("level", "fault")) for f in side.answer_key["faults"]
                  if f["doc_type"] == "prep")
    ours = Counter((r["prep_id"], f["fault_type"], f["level"]) for r in results for f in r["faults"]
                   if f["fault_type"] != "missing_disclosure")
    return key == ours, key - ours, ours - key


def check_required_disclosures(side):
    """v3: the disclosures this scorer requires must be exactly the ones the generator put in each baseline prep
    (its own disclosure claims, not the ones copied from a note). Returns (ok, [mismatches])."""
    by_doc = defaultdict(list)
    for c in side.claims:
        if c["doc_type"] == "prep" and c["attribute"] == "disclosure" and not c.get("copied_from"):
            by_doc[c["doc_id"]].append(c["value"])
    bad = []
    for p in side.preps_baseline:
        ours = side.required_disclosures(p["client_id"], D(p["date"]))
        if sorted(ours) != sorted(by_doc[p["prep_id"]]):
            bad.append({"prep_id": p["prep_id"], "client_id": p["client_id"], "date": p["date"],
                        "generator": sorted(by_doc[p["prep_id"]]), "scorer": sorted(ours)})
    return not bad, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preps", nargs="?", help="JSON list of preps with claims (harness mode)")
    ap.add_argument("--baseline", action="store_true", help="score the preps written with no harness")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="the generator's output folder (has visible/ and hidden/)")
    ap.add_argument("--json", help="write per-prep results and the summary to this file")
    a = ap.parse_args()
    if bool(a.preps) == a.baseline:
        ap.error("give either a preps file or --baseline")

    side = AnswerSide(a.data)
    if side.ladder_source is None:
        print("warning: no drift ladder (no gen.py beside the data and no drift in its answer key); "
              "every reworded disclosure counts as a fault of unknown severity", file=sys.stderr)
    preps = baseline_preps(side) if a.baseline else load(a.preps)
    results = [score_prep(side, p) for p in preps]
    summary = summarize(results, side.expected_source)
    out = {"source": "baseline (no harness)" if a.baseline else str(a.preps), "data": str(a.data),
           "data_version": side.version, "drift_ladder_source": side.ladder_source,
           "summary": summary, "preps": results}

    if a.baseline:
        ok, missing, extra = check_against_answer_key(side, results)
        out["matches_answer_key"] = ok
        if not ok:
            out["answer_key_missing"] = [list(k) for k in missing.elements()]
            out["scorer_extra"] = [list(k) for k in extra.elements()]
        if side.version >= 3:
            ok, bad = check_required_disclosures(side)
            out["required_disclosures_match_baseline"] = ok
            if not ok:
                out["required_disclosures_mismatch"] = bad

    print(json.dumps({k: v for k, v in out.items() if k != "preps"}, indent=2))
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)


if __name__ == "__main__":
    main()
