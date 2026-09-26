# Live runs on real models

Three independent runs of `python -m pregame.cli demo` in record mode, started together at 13:49:46 EDT on 26 Sep 2026
(commit 582d37f), each with its own Atlas database (`pregame_run_a/b/c`) and its own recording
(`cassettes/run-a.jsonl`, `run-b.jsonl`, `run-c.jsonl`). Models: Claude Sonnet 5 drafts, Claude Haiku 4.5 reads,
Claude Opus 5.5 improves, all through a Claude subscription via headless `claude -p`. Retirement segment; each
proposal is judged on 6 held-out meetings (a small simulated test, not proof).

| Step | Run A | Run B | Run C |
|---|---|---|---|
| Champion's held-out accuracy | 0.72 | 0.72 | 0.74 |
| Improve #1 (broad change) | rejected: context +52% | rejected: guardrail violations rose, context +52% | rejected: false alarms rose, context +52% |
| Improve #2 | **committed: 0.72 -> 0.94**, worst meeting 0.60 -> 0.80 | rejected: 0.72 -> 0.84 but guardrail violations rose | **committed: 0.74 -> 0.94**, worst meeting 0.60 -> 0.80 |
| Tamper with the yardstick | refused (tier X) | refused (tier X) | refused (tier X) |
| Improve #3 | rejected: inside the 0.05 margin; guardrail violations rose | rejected: 0.72 -> 0.94 but guardrail violations rose | rejected: inside the 0.05 margin; guardrail violations rose |
| Ledger | 21 entries, chain verified | 21 entries, chain verified | 21 entries, chain verified |

- The same unchanged champion scored 0.72–0.74 across runs: run-to-run noise of about 0.02, against gains of +0.20.
- **Correction (14:25):** every guardrail violation counted in these runs was a **false positive** of the text checks
  (34 flags, all compliant prose: "Do not present it as a guaranteed outcome", "If she asks about guaranteed income...",
  "Put inflation of 3.00% next to those rates"). So in run B the gate wrongly rejected real improvements (0.72 -> 0.84
  and 0.72 -> 0.94), and the "guardrail violations rose" part of the other rejections was wrong. The checks were fixed
  (2aa6ce2, then scoped after a Sol review in fb67de6); the 26 flagged sentences are now regression tests.
- An earlier single run (13:10–13:32, commit before the disclosure and grader changes) told the same story:
  rejected 0.72 -> 0.91, committed 0.72 -> 0.94, tamper refused, third proposal rejected for guardrail violations.

**Re-graded with the fixed checks** (replaying each recording, no new model calls, 14:28): run C replays in full and
its decisions stand without any guardrail reason: broad change rejected (false alarms up, brief +52%), narrow change
**adopted 0.74 -> 0.94** (worst meeting 0.60 -> 0.80), tamper refused, third change rejected as a gain under the 0.05
margin. Runs A and B cannot be fully re-graded: under the fixed checks a live brief that was redrafted during recording
now passes first time, so the later prompts differ from the recording.

**Stage replay.** `cassettes/demo.jsonl` is run C. Replaying it from a clean database reproduces it exactly (21 ledger
entries verified; checked again after the guardrail fix at 14:35) in under a minute with no model calls:

    PREGAME_LLM_MODE=replay PREGAME_DB=pregame_demo python -m pregame.cli demo
