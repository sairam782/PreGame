#!/usr/bin/env python3
"""Check that the showcase data can show learning: sweep the harness's two tunable settings with the harness's
OWN prep writer (prep-harness pregame/cabinet.py, imported read-only; no model calls), pick the best setting on
the TUNE clients only, and report it on the TEST clients next to the harness's current default.

This is a data check, not the harness improving itself: it shows what a loop that tunes these settings on the
tune clients could reach at best, and whether that holds on clients it never saw. Scores come from
score_preps.py run as a separate process on each split.

    python showcase_sweep.py                         # data/banker_sim_showcase/out
    python showcase_sweep.py --data data/banker_sim_showcase/out80 --json results/showcase_sweep.json
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = Path(r"C:\Projects\prep-harness")
EXPIRY_GRID = [None, 30, 45, 50, 55, 60, 75, 90, 120, 150, 180]
THRESHOLD_GRID = [None, 1, 2, 3, 4, 5]


SCORER = HERE / "score_preps.py"


def score(preps, data_dir):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(preps, f)
        path = f.name
    out = subprocess.run([sys.executable, str(SCORER), path, "--data", str(data_dir)],
                         capture_output=True, text=True, encoding="utf-8")
    Path(path).unlink(missing_ok=True)
    if out.returncode:
        raise SystemExit(out.stderr)
    return json.loads(out.stdout)["summary"]


def key(s):
    """Lower is better: preps with a mistake, then mistakes, then expected actions missed, then unneeded questions."""
    return (s["preps_with_a_fault"], s["faults_total"], s["expected_actions"] - s["actions_hit"], s["questions_unneeded"])


def brief(s):
    return {k: s[k] for k in ("preps", "preps_with_a_fault", "faults_total", "faults_by_type", "expected_actions",
                              "actions_hit", "action_recall", "questions_unneeded")}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(HERE / "data" / "banker_sim_showcase" / "out"))
    ap.add_argument("--json")
    ap.add_argument("--scorer", help="another copy of score_preps.py (default: the one beside this file)")
    a = ap.parse_args()
    global SCORER
    if a.scorer:
        SCORER = Path(a.scorer)
    data_dir = Path(a.data)
    sys.path.insert(0, str(HARNESS))
    from pregame import cabinet  # noqa: E402  (read-only use of the harness's own prep writer)

    data = cabinet.load_files(data_dir / "visible")
    facts = cabinet.build_facts(data)
    split_of = {r["client_id"]: r["split"] for r in json.loads((data_dir / "hidden" / "splits.json").read_text("utf-8"))}
    default = dict(cabinet.HARNESS_POLICY)
    rows = []
    for e in EXPIRY_GRID:
        for t in THRESHOLD_GRID:
            policy = dict(default, name=f"expiry={e},threshold={t}", label_expiry_days=e, contradiction_threshold=t)
            preps = cabinet.write_preps(data, policy, facts)
            res = {sp: score([p for p in preps if split_of[p["client_id"]] == sp], data_dir) for sp in ("tune", "test")}
            rows.append({"label_expiry_days": e, "contradiction_threshold": t, **res})
            print(f"expiry={str(e):>5} threshold={str(t):>4}  tune {key(res['tune'])}  test {key(res['test'])}",
                  flush=True)
    best = min(rows, key=lambda r: (key(r["tune"]), EXPIRY_GRID.index(r["label_expiry_days"]),
                                    THRESHOLD_GRID.index(r["contradiction_threshold"])))
    cur = next(r for r in rows if r["label_expiry_days"] == default["label_expiry_days"]
               and r["contradiction_threshold"] == default["contradiction_threshold"])
    best_test = min(rows, key=lambda r: key(r["test"]))
    report = {
        "data": str(data_dir),
        "method": "harness prep writer (code only, no model) at every setting; choose on tune; report test",
        "objective": "fewest preps with a mistake, then mistakes, then expected actions missed, then unneeded questions",
        "current_default": {"label_expiry_days": default["label_expiry_days"],
                            "contradiction_threshold": default["contradiction_threshold"],
                            "tune": brief(cur["tune"]), "test": brief(cur["test"])},
        "chosen_on_tune": {"label_expiry_days": best["label_expiry_days"],
                           "contradiction_threshold": best["contradiction_threshold"],
                           "tune": brief(best["tune"]), "test": brief(best["test"])},
        "best_possible_on_test": {"label_expiry_days": best_test["label_expiry_days"],
                                  "contradiction_threshold": best_test["contradiction_threshold"],
                                  "test": brief(best_test["test"])},
        "grid": [{"label_expiry_days": r["label_expiry_days"], "contradiction_threshold": r["contradiction_threshold"],
                  "tune": key(r["tune"]), "test": key(r["test"])} for r in rows],
    }
    print(json.dumps({k: v for k, v in report.items() if k != "grid"}, indent=1))
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
