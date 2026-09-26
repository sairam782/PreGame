VERDICT: SEND BACK
............................................................             [100%] (exit 0; pytest printed no conventional passed-count/result-summary line)

## Blocking findings

- `tests/test_oracle.py:420`: The test named `test_oracle_and_metrics_do_not_import_improver_gate_or_database` checks only that the oracle and metrics do not import the improver. That is the reverse of the done-line security boundary: it can still pass if an improver imports/calls `pregame.oracle`, reads its keys, or otherwise reaches its yardstick. The snapshot contains no `pregame/improver.py` or gate implementation against which the required boundary can be verified, and comments in `pregame/oracle.py:3` are not enforcement. This matters because an improving loop able to inspect its held-out grader can optimize against or disclose its own yardstick. Smallest fix: when the improver exists, add an architectural test that scans the improver and every module it can invoke for imports/dynamic imports/references to `pregame.oracle`, `pregame.metrics`, and held-out scenarios, and exercise the restricted improver view to prove those resources raise `PermissionError`; keep proposal validation rejecting oracle, metrics, scenarios, and gate surfaces.

## Other findings

- `pregame/oracle.py:491`: `evaluate` silently accepts scenarios from multiple splits and labels the result with a synthetic string such as `"heldout+tuning"`. The contract describes evaluation of a scenario split, and mixing tuning with held-out inputs weakens the temporal isolation boundary and produces a non-contract `Split` value. Smallest fix: validate that all scenarios have one identical split and raise `ValueError` otherwise; add a regression test beside the mixed-field/duplicate-ID test.

## What I checked and found correct

- Read `DESIGN.md`, `INTERFACES.md`, and `pregame/contracts.py` first, then reviewed both implementations and both scoped test files. `check_answer` is code-based, the reader receives only brief markdown plus question text, stale-claim grading uses full scenario history, the required guardrails exist, live evaluation uses a thread pool, and metrics enforce the specified accuracy margin, worst-scenario floor, false-alarm/stale-claim floors, and +50% context ceiling. Neither frozen module performs database I/O or imports the database, gate, ledger, versions, or improver modules. No secrets or Atlas/transaction/ServerApi code occur in these scoped modules.
