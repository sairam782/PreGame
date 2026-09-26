"""Command line: `python -m pregame.cli <command>`.

Output is for a terminal shown to judges: short, aligned, plain words. Colour only via ANSI
codes, and only when stdout is a tty. Never prints secrets (API keys, connection strings).
"""
from __future__ import annotations

import argparse
import getpass
import sys
from typing import Optional

from pregame import loop


# ---------------------------------------------------------------------------------------------------------------
# colour (tty-only)
# ---------------------------------------------------------------------------------------------------------------
_TTY = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _TTY else ""


RESET = _c("\033[0m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
GREEN = _c("\033[32m")
RED = _c("\033[31m")
YELLOW = _c("\033[33m")
CYAN = _c("\033[36m")


def _status_color(status: Optional[str]) -> str:
    return {
        "committed": GREEN,
        "awaiting_owner": YELLOW,
        "rejected": RED,
        "stale": RED,
        "pending": DIM,
        "evaluating": DIM,
    }.get(status or "", "")


# ---------------------------------------------------------------------------------------------------------------
# db / llm plumbing
# ---------------------------------------------------------------------------------------------------------------
def _db():
    from pregame.db import get_db

    return get_db()


def _llm():
    from pregame.llm import get_llm

    return get_llm()


def _owner() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "owner"


def _print_banner(db, llm=None) -> None:
    """One line before any command that calls a model or writes: mode, provider, model ids, and
    the target database + host (never the connection string, username or password).

    Prefers the settings actually behind `llm` when given (accurate even if a caller injected a
    non-default Settings), falling back to the process's global settings otherwise.
    """
    from pregame.config import mode_banner
    from pregame.config import settings as get_settings

    settings = getattr(llm, "settings", None) or get_settings()
    print(mode_banner(getattr(db, "name", "?"), settings))


# ---------------------------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------------------------
def cmd_setup(args) -> None:
    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    try:
        counts = loop.setup(db, llm, yes=args.yes)
    except loop.SetupRefused as exc:
        print(f"{RED}refused{RESET}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"{BOLD}setup complete{RESET}")
    for k, v in counts.items():
        print(f"  {k:<16} {v}")


def cmd_events(args) -> None:
    from pregame.world import store

    db = _db()
    events = store.list_events(db, field=args.field)
    print(f"{BOLD}{'id':<28}{'field':<12}{'month':<7}{'fired':<7}title{RESET}")
    for e in events:
        fired = f"{GREEN}yes{RESET}" if e.get("fired") else f"{DIM}no{RESET}"
        print(f"{e['id']:<28}{e['field']:<12}{e['month']:<7}{fired:<16}{e.get('title', '')}")


def cmd_fire(args) -> None:
    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    result = loop.market_event(db, args.event_id, llm)
    print(f"{BOLD}fired {result['event']}{RESET}")
    print(f"  brief         {result['brief_id']}")
    acc = result.get("call_accuracy")
    print(f"  call_accuracy {acc:.2f}" if acc is not None else "  call_accuracy n/a")
    print(f"  missed        {len(result['missed'])}")
    for m in result["missed"]:
        print(f"    - {m.get('text', m.get('question_id'))}")
    if result.get("feedback"):
        print(f"  {YELLOW}feedback filed:{RESET} {result['feedback']}")
    else:
        print(f"  {GREEN}no feedback needed{RESET}")


def cmd_brief(args) -> None:
    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    try:
        brief = loop.make_brief(db, args.field, args.account, llm)
    except loop.BriefBlocked as exc:
        print(f"{BOLD}brief blocked by guardrails{RESET} (recorded in the ledger; nothing stored):")
        for v in exc.violations[:10]:
            print(f"  - {v}")
        raise SystemExit(1)
    print(f"{BOLD}brief {brief['_id']}{RESET}  ({args.field}/{brief.get('account_id')})")
    print(f"  versions: {brief['receipt']['versions']}")
    print(f"  facts used: {len(brief['receipt']['fact_ids'])}  (excluded_superseded={brief['receipt']['excluded_superseded']})")
    print()
    print(brief["markdown"])


def cmd_improve(args) -> None:
    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    proposal = loop.improve(db, args.field, llm)
    if proposal is None:
        print(f"{DIM}no proposal filed{RESET}")
        return
    color = _status_color(proposal.get("status"))
    print(f"{BOLD}proposal {proposal['_id']}{RESET}  tier={proposal.get('tier')}  status={color}{proposal['status']}{RESET}")
    for line in proposal.get("diff") or []:
        print(f"  {line}")
    if proposal.get("decision"):
        print(f"  {proposal['decision']}")
    if proposal.get("status") == "awaiting_owner":
        print(f"  approve with: python -m pregame.cli approve {proposal['_id']} {proposal.get('approval_hash')}")


def cmd_proposals(args) -> None:
    db = _db()
    docs = list(db.proposals.find().sort([("created_sim", -1), ("created_at", -1)]))
    print(f"{BOLD}{'id':<12}{'field':<12}{'kind':<12}{'tier':<6}{'status':<16}decision{RESET}")
    for p in docs:
        color = _status_color(p.get("status"))
        print(
            f"{p['_id']:<12}{p.get('field', ''):<12}{p.get('kind', ''):<12}"
            f"{p.get('tier') or '-':<6}{color}{p.get('status', ''):<16}{RESET}{p.get('decision') or ''}"
        )


def cmd_approve(args) -> None:
    from pregame import gate
    from pregame.world import store

    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    sim_time = store.sim_now(db)
    version = gate.approve(db, args.proposal_id, args.hash, _owner(), sim_time)
    print(f"{GREEN}approved{RESET}: {version['_id']}")


def cmd_reject(args) -> None:
    from pregame import gate
    from pregame.world import store

    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    sim_time = store.sim_now(db)
    proposal = gate.reject(db, args.proposal_id, _owner(), args.reason or "", sim_time)
    print(f"{RED}rejected{RESET}: {proposal['_id']}")


def cmd_rollback(args) -> None:
    from pregame import versions
    from pregame.world import store

    if ":" not in args.kind_key:
        print(f"{RED}error{RESET}: expected <kind>:<key>, got {args.kind_key}", file=sys.stderr)
        sys.exit(2)
    kind, key = args.kind_key.split(":", 1)

    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    sim_time = store.sim_now(db)
    version = versions.rollback(db, kind, key, int(args.version), f"owner:{_owner()}", sim_time)
    print(f"{GREEN}rolled back{RESET}: {version['_id']} (restores v{args.version})")


def cmd_trace(args) -> None:
    db = _db()
    brief = db.briefs.find_one({"_id": args.brief_id})
    if not brief:
        print(f"{RED}no such brief{RESET}: {args.brief_id}", file=sys.stderr)
        sys.exit(1)

    receipt = brief["receipt"]
    print(f"{BOLD}brief {brief['_id']}{RESET}  field={brief.get('field')}  account={brief.get('account_id')}  as_of={receipt.get('as_of')}")
    print(f"  config versions: {receipt.get('versions')}")
    print(f"  config_hash: {receipt.get('config_hash')}")
    print(f"  excluded_superseded: {receipt.get('excluded_superseded')}   context_tokens: {receipt.get('context_tokens')}")
    print(f"\n{BOLD}facts:{RESET}")
    for fact_id in receipt.get("fact_ids", []):
        fact = db.facts.find_one({"_id": fact_id})
        text = fact.get("text", "") if fact else f"{DIM}(not found){RESET}"
        print(f"  {fact_id:<48} {text}")


def cmd_ledger(args) -> None:
    from pregame import ledger

    db = _db()
    if args.verify:
        ok, checked, problem = ledger.verify(db)
        status = f"{GREEN}OK{RESET}" if ok else f"{RED}FAILED{RESET}"
        print(f"ledger verify: {status}  ({checked} entries checked)")
        if problem:
            print(f"  {problem}")
        if not ok:
            sys.exit(1)
        return

    entries = ledger.tail(db, 30)
    print(f"{BOLD}{'seq':<6}{'kind':<12}{'actor':<14}{'hash':<12}sim_time{RESET}")
    for e in entries:
        print(f"{e['seq']:<6}{e['kind']:<12}{e['actor']:<14}{e['hash'][:10]:<12}{e['sim_time']}")


def cmd_tamper(args) -> None:
    from pregame import gate, improver
    from pregame.world import store

    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    sim_time = store.sim_now(db)
    proposal = improver.tamper_proposal(args.field, sim_time)
    filed = gate.file_proposal(db, proposal)
    evaluated = gate.evaluate_proposal(db, filed["_id"], llm)
    color = _status_color(evaluated.get("status"))
    print(f"{BOLD}proposal {evaluated['_id']}{RESET}  tier={evaluated.get('tier')}  status={color}{evaluated['status']}{RESET}")
    print(f"  {evaluated.get('decision')}")


def cmd_demo(args) -> None:
    db = _db()
    llm = _llm()
    loop.run_demo(db, llm, yes=getattr(args, "yes", False))


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("pregame.web.app:app", host="127.0.0.1", port=8000)


def _cabinet_table(cols: dict) -> list[str]:
    """Rows for the before/after table: one column per scored set (aggregates only)."""
    names = list(cols)

    def pct(s, key):
        n, v = s.get("preps") or 0, s.get(key)
        return f"{v}/{n} ({round(100 * v / n)}%)" if v is not None and n else "-"

    def recall(s):
        exp = s.get("expected_actions")
        if exp is None:
            exp, hit = s.get("questions_expected"), s.get("questions_hit")
        else:
            hit = s.get("actions_hit")
        return f"{hit}/{exp}" if exp is not None else "-"

    rows = [("preps with a fault", lambda s: pct(s, "preps_with_a_fault")),
            ("  ignoring warnings", lambda s: pct(s, "preps_with_a_fault_ignoring_warnings")),
            ("faults (total)", lambda s: str(s.get("faults_total", "-")))]
    types = sorted({t for s in cols.values() for t in (s.get("faults_by_type") or {})})
    rows += [(f"  {t}", (lambda t: lambda s: str((s.get("faults_by_type") or {}).get(t, 0)))(t)) for t in types]
    rows += [("forbidden promises", lambda s: str(s.get("forbidden_promises", "-"))),
             ("expected actions done", recall)]
    kinds = sorted({k for s in cols.values() for k in (s.get("expected_actions_by_type") or {})})
    rows += [(f"  {k}", (lambda k: lambda s: f"{(s.get('actions_hit_by_type') or {}).get(k, 0)}/"
                                              f"{(s.get('expected_actions_by_type') or {}).get(k, 0)}")(k))
             for k in kinds]
    rows += [("unneeded questions", lambda s: str(s.get("questions_unneeded", "-")))]
    out = [f"{BOLD}{'measure':<26}" + "".join(f"{n:>14}" for n in names) + RESET]
    for label, fn in rows:
        out.append(f"{label:<26}" + "".join(f"{fn(cols[n]):>14}" for n in names))
    return out


def cmd_cabinet(args) -> None:
    """Before/after on the data teammate's banker cabinet: no-harness baseline, naive policy, harness policy."""
    from pregame import cabinet

    db = _db()
    _print_banner(db)
    data = cabinet.load_data(db.client)
    if not cabinet.check_disclosures(data):
        print(f"{RED}refused{RESET}: the cabinet's locked disclosures differ from contracts.APPROVED_LANGUAGE",
              file=sys.stderr)
        sys.exit(1)
    print(f"cabinet data: {data['source']}  ({len(data['preps_baseline'])} prep slots, no model calls)")
    if getattr(args, "ablate", False):
        _cabinet_ablate(db, data)
        return
    result = cabinet.run_before_after(data)
    cols = {"no harness": result["baseline"], "naive policy": result["naive"], "harness": result["harness"]}
    print()
    for line in _cabinet_table(cols):
        print(line)
    try:
        receipt = cabinet.store_run(db, result["harness_preps"], cabinet.HARNESS_POLICY,
                                    {"baseline": result["baseline"], "naive": result["naive"],
                                     "harness": result["harness"]}, data["source"])
        print()
        print(f"stored {receipt['prep_count']} harness preps in {cabinet.PREPS_COLLECTION} and receipt "
              f"{receipt['_id']} in {cabinet.RUNS_COLLECTION} (db={db.name})")
    except Exception as exc:
        print(f"{YELLOW}not stored{RESET}: {type(exc).__name__}", file=sys.stderr)


def _cabinet_ablate(db, data) -> None:
    """`cabinet --ablate`: the harness with one rule switched off at a time, plus full harness and all-off rows."""
    from pregame import cabinet

    rows = cabinet.run_ablation(data)
    lines = cabinet.ablation_table(rows)
    print()
    print(f"{BOLD}{lines[0]}{RESET}")
    for line in lines[1:]:
        print(line)
    print(f"{DIM}mistakes by type: drift = compliance_drift, said/did = said_vs_did, overwrite = stale_overwrite, "
          f"stale ref = stale_reference, sticky = sticky_label, wrong DM = wrong_decision_maker; expected actions = "
          f"ask / flag / brief_both done of expected{RESET}")
    same = [r["rule"] for r in rows if r["knob"] and r["preps_changed_vs_harness"] == 0]
    if same:
        print(f"{DIM}no prep changes when switched off (on this data): {'; '.join(same)}{RESET}")
    try:
        doc = cabinet.store_ablation(db, rows, data["source"])
        print()
        print(f"stored the table as {doc['_id']} in {cabinet.ABLATIONS_COLLECTION} (db={db.name})")
    except Exception as exc:
        print(f"{YELLOW}not stored{RESET}: {type(exc).__name__}", file=sys.stderr)


def cmd_cabinet_live(args) -> None:
    """The live-model experiment: the drafter with and without the harness, repeated (see pregame/cabinet_live.py)."""
    from pregame import cabinet, cabinet_live

    db = _db()
    llm = _llm()
    _print_banner(db, llm)
    if llm.is_fake:
        print(f"{RED}refused{RESET}: cabinet-live calls a model; set PREGAME_LLM_MODE to live, record or replay",
              file=sys.stderr)
        sys.exit(1)
    data = cabinet.load_data(db.client)
    if not cabinet.check_disclosures(data):
        print(f"{RED}refused{RESET}: the cabinet's locked disclosures differ from contracts.APPROVED_LANGUAGE",
              file=sys.stderr)
        sys.exit(1)
    clients = [c.strip() for c in args.clients.split(",") if c.strip()] if args.clients else None
    conditions = cabinet_live.CONDITIONS if args.condition == "both" else (args.condition,)
    code_owned = cabinet_live.parse_code_owned(args.code_owned)
    slots = cabinet_live.select_slots(data, clients, args.limit)
    calls = len(slots) * len(conditions) * args.runs
    print(f"cabinet data: {data['source']}  ({len(slots)} prep slots x {len(conditions)} conditions x "
          f"{args.runs} runs = {calls} model calls, concurrency {args.concurrency}, "
          f"model {llm.model_id(cabinet_live.ROLE)})")
    exp = cabinet_live.run_experiment(data, llm, runs=args.runs, clients=clients, limit=args.limit,
                                      concurrency=args.concurrency, conditions=conditions, code_owned=code_owned)
    print(f"\nprompts: {cabinet_live.prompt_digest(exp)} (same digest = the same questions asked)")
    for (cond, run), res in sorted(exp["results"].items()):
        for e in res["errors"]:
            print(f"{YELLOW}call failed{RESET}: {cond} run {run} {e['prep_id']}: {e['error']}", file=sys.stderr)
    scores = cabinet_live.score_experiment(exp)
    refs = cabinet_live.reference_scores(data, exp["slots"])
    lines, _ = cabinet_live.table(scores, refs, args.runs)
    print()
    print(f"{BOLD}{lines[0]}{RESET}")
    for line in lines[1:]:
        print(line)
    print(f"{DIM}no-h = {cabinet_live.CONDITION_LABELS['no_harness']}; harn = {cabinet_live.CONDITION_LABELS['harness']}"
          f"; baseline = the assistant's preps with no harness (all 24 slots); code harn = the code-written harness "
          f"preps on these slots{RESET}")
    print(f"{DIM}{cabinet_live.code_owned_note(exp)}{RESET}")
    try:
        receipts = cabinet_live.store_experiment(db, exp, scores, data["source"])
        print()
        print(f"stored {len(receipts)} runs in {cabinet.RUNS_COLLECTION} and their preps in "
              f"{cabinet.PREPS_COLLECTION} (db={db.name}): {', '.join(r['_id'] for r in receipts)}")
    except Exception as exc:
        print(f"{YELLOW}not stored{RESET}: {type(exc).__name__}", file=sys.stderr)


# ---------------------------------------------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pregame.cli", description="Pregame: a prep bot that improves its own harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="reset + seed the world and configs")
    p_setup.add_argument("--yes", action="store_true",
                          help="skip the safety check that refuses a target db not named pregame*")
    p_setup.set_defaults(func=cmd_setup)

    p_events = sub.add_parser("events", help="list scripted world and client events")
    p_events.add_argument("field", nargs="?", default=None)
    p_events.set_defaults(func=cmd_events)

    p_fire = sub.add_parser("fire", help="fire an event and simulate the client review")
    p_fire.add_argument("event_id")
    p_fire.set_defaults(func=cmd_fire)

    p_brief = sub.add_parser("brief", help="write a brief for a field/account")
    p_brief.add_argument("field")
    p_brief.add_argument("account", nargs="?", default=None)
    p_brief.set_defaults(func=cmd_brief)

    p_improve = sub.add_parser("improve", help="ask the improver for a proposal and run the gate")
    p_improve.add_argument("field")
    p_improve.set_defaults(func=cmd_improve)

    sub.add_parser("proposals", help="list proposals, newest first").set_defaults(func=cmd_proposals)

    p_approve = sub.add_parser("approve", help="approve an awaiting_owner proposal by hash")
    p_approve.add_argument("proposal_id")
    p_approve.add_argument("hash")
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="reject a proposal")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--reason", default="")
    p_reject.set_defaults(func=cmd_reject)

    p_rollback = sub.add_parser("rollback", help="roll a config back to an old version")
    p_rollback.add_argument("kind_key", metavar="kind:key")
    p_rollback.add_argument("version", type=int)
    p_rollback.set_defaults(func=cmd_rollback)

    p_trace = sub.add_parser("trace", help="print a brief's receipt: facts and config versions used")
    p_trace.add_argument("brief_id")
    p_trace.set_defaults(func=cmd_trace)

    p_ledger = sub.add_parser("ledger", help="show the ledger tail, or verify the hash chain")
    p_ledger.add_argument("--verify", action="store_true")
    p_ledger.set_defaults(func=cmd_ledger)

    p_tamper = sub.add_parser("tamper", help="attempt to change a frozen surface (expect refusal)")
    p_tamper.add_argument("field")
    p_tamper.set_defaults(func=cmd_tamper)

    p_demo = sub.add_parser("demo", help="run the scripted retirement-segment demo (resets its database first)")
    p_demo.add_argument("--yes", action="store_true", help="allow a database whose name does not start with pregame")
    p_demo.set_defaults(func=cmd_demo)
    sub.add_parser("serve", help="serve the live page on 127.0.0.1:8000").set_defaults(func=cmd_serve)
    p_cab = sub.add_parser("cabinet", help="before/after on the banker cabinet data (baseline, naive, harness; no model)")
    p_cab.add_argument("--ablate", action="store_true",
                       help="switch the harness rules off one at a time and score each (no model)")
    p_cab.set_defaults(func=cmd_cabinet)
    p_live = sub.add_parser("cabinet-live", help="the drafter model on the cabinet, with and without the harness, "
                                                 "repeated (calls a model)")
    p_live.add_argument("--runs", type=int, default=2, help="repeat runs per condition (identical prompts)")
    p_live.add_argument("--clients", default=None, help="comma-separated client ids (default: all)")
    p_live.add_argument("--limit", type=int, default=None, help="at most this many prep slots per client")
    p_live.add_argument("--concurrency", type=int, default=4,
                        help="thread-pool size (claude-cli calls are also capped by PREGAME_CLI_CONCURRENCY)")
    p_live.add_argument("--condition", choices=("both", "no_harness", "harness"), default="both",
                        help="run both arms (default) or only one")
    p_live.add_argument("--code-owned", default="disclosures,decision_maker",
                        help="claims code writes in the harness arm after the model's reply: 'disclosures' (the "
                             "earlier behaviour) or 'disclosures,decision_maker' (default)")
    p_live.set_defaults(func=cmd_cabinet_live)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
