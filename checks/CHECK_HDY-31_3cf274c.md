VERDICT: SEND BACK
........................................................................ [ 96%]
...                                                                      [100%]

## Blocking findings

- `pregame/improver.py:66`: `ImproverView` stores the unrestricted MongoDB database as the directly readable `view._db`. Consequently improver-side code can bypass every filtering/refusal method with `view._db.eval_scenarios.find(...)` or `view._db.eval_runs.find({"split": "heldout"})`, and can also mutate those collections. This violates the binding requirement that the view expose only the listed collections/tuning rows and that any other collection or held-out row raise `PermissionError`; it lets the improving side inspect or alter its own yardstick. The boundary test at `tests/test_improver.py:106` checks only the nonexistent public alias `view.db`, so it passes while the real backing handle remains exposed. Smallest fix: do not retain an unrestricted database handle on the object handed to improver code; construct a capability-limited data source whose stored capabilities are only the allowed, filtered reads (with refusal logging kept outside that object), and add regression tests that attempt the actual `_db`/instance-state bypass and prove held-out reads and writes are unreachable. If adversarial Python code is in scope, enforce this boundary out of process because underscore naming and `__getattr__` are not access controls.

## Other findings

- None.

## What I checked and found correct

- The exact requested pytest invocation exited 0 and printed the two progress/result lines above; pytest emitted no conventional passed-count summary under this configuration.
- The previous mixed-split finding is fixed: `pregame/oracle.py:448-454` rejects scenarios spanning more than one split, and evaluation validates field and duplicate scenario IDs.
- `check_answer` makes correctness decisions in code, normalizes numeric forms, and requires an honest non-numeric unknown for impossible questions. The reader prompt receives only brief markdown and question text.
- The required `cite-facts`, `no-stale-facts`, and `no-advice` checks exist; stale-claim grading uses full scenario history rather than only compiled context.
- Metrics implement equal scenario weighting, pass^k, the accuracy margin, worst-scenario floor, false-alarm and stale-claim floors, scenario-set comparability, and the +50% context ceiling.
- The new source/transitive/tripwire/marker tests substantially improve oracle/metrics and held-out isolation coverage, and the ordinary `propose` paths do not import or call oracle/metrics evaluation functions. No secrets are printed or committed in the scoped files, and these pure modules contain no Atlas transaction, validator, or ServerApi usage.
