VERDICT: SEND BACK
TEST RESULT: PASS — `C:/Projects/prep-harness/.venv/Scripts/python -c "import pregame.contracts"` exited 0 with no output.

## Blocking findings

- `pregame/contracts.py:207` — The grading/contracts section defines `QuestionResult`, `Grade`, and `EvalSummary`, but never defines the shape of an `eval_runs` row. `INTERFACES.md:27` requires a unique cache key over `(config_hash, scenario_id, run)`, `INTERFACES.md:103-105` requires the improver-facing rows to carry `split` plus aggregates and per-question reasons, and `INTERFACES.md:114-117` requires the gate/oracle to persist and reuse those rows. Without one shared `EvalRun` contract, separately implemented modules can disagree about required keys or accidentally omit `split`, undermining both cache correctness and the rule that the improver may see tuning rows only. This also directly misses the done line that data shapes cover every module. Smallest fix: add an `EvalRun(TypedDict)` containing the binding persisted fields (at minimum cache identity fields `config_hash`, `scenario_id`, `run`; `split` and field/config identity; and the stored grade/aggregate/per-question result data), then make the relevant interface wording name that type.

## Other findings

- None.

## What I checked and found correct

- `DESIGN.md`, `INTERFACES.md`, and `pregame/contracts.py` are present, internally consistent on the core architecture, frozen surfaces, tier policy, transaction fence, Atlas `ServerApi("1")` without strict mode, and held-out isolation intent.
- The remaining declared shared records and constants cover facts, accounts, market events, scenarios/questions, versioned configuration, context/receipts, briefs/claims, grades/summaries, proposals, feedback, and ledger entries; the policy bounds, section names, rule cap, thresholds, tiers, statuses, ledger kinds, and model roles match the specification.
- `requirements.txt` includes the specified runtime and test dependencies; `.gitignore` excludes `.env`, local cassettes, virtual environments, bytecode, and pytest cache; `.env.example` contains placeholders rather than credentials; and `tests/conftest.py` provides a fresh timezone-aware mongomock database without network access. `pytest.ini` correctly targets `tests` in quiet mode.
