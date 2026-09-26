"""Frozen metrics over oracle grades: pass^k summaries and the candidate-vs-champion comparison.

FROZEN (tier X): no proposal may change this file. Pure: no I/O, no database, no model calls.

Definitions (these are the yardstick; the gate only reads them):
- accuracy of a run          = correct answers / questions (computed by the oracle's code checks)
- per_scenario[id]           = mean accuracy of that scenario's runs
- mean_accuracy              = mean of per_scenario (every scenario weighs the same)
- worst_accuracy             = min of per_scenario (the worst scenario's mean over its k runs)
- pass_k                     = share of scenarios with at least k runs where EVERY run has
                               accuracy >= PASS_THRESHOLD and 0 stale claims
- missed_changes, false_alarms, stale_claims, context_tokens = mean per run over all runs
"""
from __future__ import annotations

from typing import Iterable, Mapping

from pregame.contracts import PASS_THRESHOLD, WIN_MARGIN, EvalSummary, Grade

CONTEXT_GROWTH_LIMIT = 0.5      # candidate context may be at most +50% over the champion's
_EPS = 1e-9                     # float slack so 0.75 + 0.05 counts as reaching 0.80


def _mean(values: Iterable[float]) -> float:
    vals = [float(v) for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def _r(x: float) -> float:
    return round(float(x), 6)


def _run_passes(g: Grade) -> bool:
    return float(g.get("accuracy", 0.0)) >= PASS_THRESHOLD - _EPS and int(g.get("stale_claims", 0)) == 0


def _scenario_passes(runs: list[Grade], k: int) -> bool:
    return len(runs) >= max(int(k), 1) and all(_run_passes(g) for g in runs)


def summarize(config_label: str, split: str, field: str, k: int,
              grades: Mapping[str, list[Grade]]) -> EvalSummary:
    """Aggregate per-run grades (scenario_id -> runs) into an EvalSummary."""
    per_scenario: dict[str, float] = {}
    all_runs: list[Grade] = []
    passed = 0
    for scenario_id, runs in grades.items():
        runs = list(runs)
        all_runs.extend(runs)
        per_scenario[scenario_id] = _r(_mean(g.get("accuracy", 0.0) for g in runs))
        if _scenario_passes(runs, k):
            passed += 1
    n = len(per_scenario)
    return {
        "config_label": config_label,
        "split": split,
        "field": field,
        "k": int(k),
        "n_scenarios": n,
        "mean_accuracy": _r(_mean(per_scenario.values())),
        "worst_accuracy": _r(min(per_scenario.values())) if per_scenario else 0.0,
        "pass_k": _r(passed / n) if n else 0.0,
        "missed_changes": _r(_mean(g.get("missed_changes", 0) for g in all_runs)),
        "false_alarms": _r(_mean(g.get("false_alarms", 0) for g in all_runs)),
        "stale_claims": _r(_mean(g.get("stale_claims", 0) for g in all_runs)),
        "context_tokens": _r(_mean(g.get("context_tokens", 0) for g in all_runs)),
        "per_scenario": per_scenario,
    }


def compare(candidate: EvalSummary, champion: EvalSummary) -> dict:
    """Decide whether the candidate beats the champion on the same scenarios.

    A win needs ALL of: mean accuracy >= champion + WIN_MARGIN; worst scenario not lower; false alarms per run not
    higher; stale claims per run not higher; context tokens at most +50% over the champion. The two summaries must
    also be comparable (same field, split, k and scenario set). `reasons` lists every failed condition in plain
    words, or on a win the margins that carried it.
    """
    failures: list[str] = []
    margins: list[str] = []

    # Comparability: a candidate scored on different (e.g. easier or fewer) scenarios cannot win.
    for key in ("field", "split", "k", "n_scenarios"):
        if candidate.get(key) != champion.get(key):
            failures.append(f"not comparable: {key} is {candidate.get(key)!r} for the candidate "
                            f"but {champion.get(key)!r} for the champion")
    cand_ids = set((candidate.get("per_scenario") or {}).keys())
    champ_ids = set((champion.get("per_scenario") or {}).keys())
    if cand_ids != champ_ids and candidate.get("n_scenarios") == champion.get("n_scenarios"):
        failures.append("not comparable: the candidate and champion were scored on different scenarios")
    if not candidate.get("n_scenarios"):
        failures.append("no scenarios were evaluated")

    c_acc, h_acc = float(candidate.get("mean_accuracy", 0.0)), float(champion.get("mean_accuracy", 0.0))
    delta = c_acc - h_acc
    if delta + _EPS >= WIN_MARGIN:
        margins.append(f"mean accuracy {h_acc:.2f} -> {c_acc:.2f} ({delta:+.2f}, needs +{WIN_MARGIN:.2f})")
    else:
        failures.append(f"mean accuracy {h_acc:.2f} -> {c_acc:.2f} ({delta:+.2f}); needs at least +{WIN_MARGIN:.2f}")

    c_worst, h_worst = float(candidate.get("worst_accuracy", 0.0)), float(champion.get("worst_accuracy", 0.0))
    if c_worst + _EPS >= h_worst:
        margins.append(f"worst scenario {h_worst:.2f} -> {c_worst:.2f}")
    else:
        failures.append(f"worst scenario fell {h_worst:.2f} -> {c_worst:.2f} (must not be lower than the champion's)")

    for key, label in (("false_alarms", "false alarms"), ("stale_claims", "stale claims")):
        c_val, h_val = float(candidate.get(key, 0.0)), float(champion.get(key, 0.0))
        if c_val <= h_val + _EPS:
            margins.append(f"{label} {h_val:.2f} -> {c_val:.2f} per run")
        else:
            failures.append(f"{label} rose {h_val:.2f} -> {c_val:.2f} per run (must not be higher)")

    c_tok, h_tok = float(candidate.get("context_tokens", 0.0)), float(champion.get("context_tokens", 0.0))
    limit = f"limit +{CONTEXT_GROWTH_LIMIT:.0%}"
    if h_tok <= 0:
        if c_tok <= 0:
            margins.append(f"context 0 -> 0 tokens ({limit})")
        else:
            failures.append(f"context grew from 0 to {c_tok:.0f} tokens ({limit})")
    else:
        growth = c_tok / h_tok - 1.0
        if growth <= CONTEXT_GROWTH_LIMIT + _EPS:
            margins.append(f"context {growth:+.0%} ({h_tok:.0f} -> {c_tok:.0f} tokens, {limit})")
        else:
            failures.append(f"context grew {growth:.0%} ({h_tok:.0f} -> {c_tok:.0f} tokens, {limit})")

    return {"win": not failures, "delta_accuracy": _r(delta), "reasons": failures if failures else margins}
