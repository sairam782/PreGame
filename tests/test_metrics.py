"""Tests for pregame.metrics: pass^k summaries and the candidate-vs-champion comparison."""
from pregame.contracts import PASS_THRESHOLD, WIN_MARGIN
from pregame.metrics import compare, summarize


def g(sid, accuracy, *, stale=0, false_alarms=0, missed=0, tokens=1000):
    return {
        "scenario_id": sid, "split": "heldout", "accuracy": accuracy, "missed_changes": missed,
        "false_alarms": false_alarms, "honest_unknowns": 0, "stale_claims": stale, "uncited_claims": 0,
        "guardrail_violations": [], "context_tokens": tokens, "results": [],
    }


def summary(label, per_scenario_runs, **kw):
    grades = {sid: [g(sid, a, **kw) for a in accs] for sid, accs in per_scenario_runs.items()}
    return summarize(label, "heldout", "retirement", 2, grades)


def test_summarize_means_worst_and_pass_k():
    grades = {
        "s1": [g("s1", 1.0, false_alarms=0, tokens=800), g("s1", 0.8, false_alarms=1, tokens=1000)],
        "s2": [g("s2", 0.9, missed=1, tokens=1200), g("s2", 0.5, missed=2, stale=1, tokens=1000)],
    }
    s = summarize("champion", "heldout", "retirement", 2, grades)
    assert s["config_label"] == "champion" and s["split"] == "heldout" and s["field"] == "retirement"
    assert s["k"] == 2 and s["n_scenarios"] == 2
    assert s["per_scenario"] == {"s1": 0.9, "s2": 0.7}
    assert s["mean_accuracy"] == 0.8
    assert s["worst_accuracy"] == 0.7              # the worst scenario's mean over its runs, not the worst run
    assert s["pass_k"] == 0.5                      # s1: both runs >= 0.8 (0.8 counts); s2: one run below
    assert s["missed_changes"] == 0.75             # mean per run: (0+0+1+2)/4
    assert s["false_alarms"] == 0.25
    assert s["stale_claims"] == 0.25
    assert s["context_tokens"] == 1000.0


def test_pass_k_needs_every_run_and_zero_stale_claims():
    perfect_but_stale = {"s1": [g("s1", 1.0), g("s1", 1.0, stale=1)]}
    assert summarize("c", "heldout", "business_owners", 2, perfect_but_stale)["pass_k"] == 0.0
    one_bad_run = {"s1": [g("s1", 1.0), g("s1", PASS_THRESHOLD - 0.01)]}
    assert summarize("c", "heldout", "business_owners", 2, one_bad_run)["pass_k"] == 0.0
    too_few_runs = {"s1": [g("s1", 1.0)]}
    assert summarize("c", "heldout", "business_owners", 2, too_few_runs)["pass_k"] == 0.0
    all_good = {"s1": [g("s1", 1.0), g("s1", 0.8)]}
    assert summarize("c", "heldout", "business_owners", 2, all_good)["pass_k"] == 1.0


def test_summarize_empty():
    s = summarize("c", "heldout", "business_owners", 2, {})
    assert s["n_scenarios"] == 0 and s["mean_accuracy"] == 0.0 and s["worst_accuracy"] == 0.0 and s["pass_k"] == 0.0


def test_clear_win_lists_margins():
    champ = summary("champion", {"s1": [0.6, 0.6], "s2": [0.7, 0.7]})
    cand = summary("candidate", {"s1": [0.8, 0.8], "s2": [0.8, 0.8]}, tokens=1100)
    result = compare(cand, champ)
    assert result["win"] is True
    assert abs(result["delta_accuracy"] - 0.15) < 1e-9
    text = " | ".join(result["reasons"])
    assert "mean accuracy 0.65 -> 0.80" in text
    assert "worst scenario 0.60 -> 0.80" in text
    assert "context +10%" in text


def test_exactly_the_margin_wins_and_just_below_loses():
    champ = summary("champion", {"s1": [0.70, 0.70]})
    at_margin = summary("candidate", {"s1": [0.70 + WIN_MARGIN] * 2})
    assert compare(at_margin, champ)["win"] is True
    below = summary("candidate", {"s1": [0.73, 0.73]})
    result = compare(below, champ)
    assert result["win"] is False
    assert any("needs at least +0.05" in r for r in result["reasons"])


def test_flag_everything_candidate_wins_accuracy_but_loses_on_context():
    # Stuffs every fact into the context: accuracy goes up, but the context triples.
    champ = summary("champion", {"s1": [0.6, 0.6], "s2": [0.6, 0.6]}, tokens=1000)
    cand = summary("candidate", {"s1": [0.9, 0.9], "s2": [0.9, 0.9]}, tokens=3120)
    result = compare(cand, champ)
    assert result["delta_accuracy"] >= WIN_MARGIN
    assert result["win"] is False
    assert result["reasons"] == ["context grew 212% (1000 -> 3120 tokens, limit +50%)"]


def test_lucky_candidate_mean_wins_but_worst_scenario_is_lower():
    champ = summary("champion", {"s1": [0.6, 0.6], "s2": [0.6, 0.6], "s3": [0.6, 0.6]})
    cand = summary("candidate", {"s1": [1.0, 1.0], "s2": [1.0, 1.0], "s3": [0.4, 0.4]})
    assert cand["mean_accuracy"] - champ["mean_accuracy"] >= WIN_MARGIN
    result = compare(cand, champ)
    assert result["win"] is False
    assert result["reasons"] == ["worst scenario fell 0.60 -> 0.40 (must not be lower than the champion's)"]


def test_every_failed_condition_is_listed():
    champ = summary("champion", {"s1": [0.8, 0.8]}, false_alarms=0, stale=0, tokens=1000)
    cand = summary("candidate", {"s1": [0.7, 0.7]}, false_alarms=2, stale=1, tokens=2000)
    reasons = compare(cand, champ)["reasons"]
    joined = " | ".join(reasons)
    assert len(reasons) == 5
    assert "mean accuracy 0.80 -> 0.70" in joined
    assert "worst scenario fell 0.80 -> 0.70" in joined
    assert "false alarms rose 0.00 -> 2.00 per run" in joined
    assert "stale claims rose 0.00 -> 1.00 per run" in joined
    assert "context grew 100%" in joined


def test_zero_token_champion_and_incomparable_summaries():
    champ = summary("champion", {"s1": [0.5, 0.5]}, tokens=0)
    cand = summary("candidate", {"s1": [0.9, 0.9]}, tokens=10)
    assert compare(cand, champ)["reasons"] == ["context grew from 0 to 10 tokens (limit +50%)"]

    champ = summary("champion", {"s1": [0.5, 0.5], "s2": [0.5, 0.5]})
    easier = summary("candidate", {"s1": [0.9, 0.9], "s9": [0.9, 0.9]})
    result = compare(easier, champ)
    assert result["win"] is False
    assert any("different scenarios" in r for r in result["reasons"])

    tuning = dict(summary("candidate", {"s1": [0.9, 0.9], "s2": [0.9, 0.9]}), split="tuning")
    result = compare(tuning, champ)
    assert result["win"] is False
    assert any(r.startswith("not comparable: split") for r in result["reasons"])
