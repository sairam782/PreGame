VERDICT: PASS
TEST RESULT: ............................................                             [100%] (exit code 0)

## Blocking findings

None.

## Other findings

None.

## What I checked and found correct

- `pregame/metrics.py:74-141` enforces the accuracy margin, non-decreasing worst-scenario accuracy and pass^k, non-increasing missed changes, false alarms, stale claims, uncited claims, and guardrail violations, plus the 50% context-growth ceiling. Every failed condition is named in `reasons`.
- `pregame/gate.py:479-504,611-620` carries failed metric names into the proposal's rejection sentence, including pass^k, missed changes, uncited claims, and guardrail violations.
- `pregame/gate.py:373-456` hashes the required evaluator modules (`contracts`, `compiler`, `drafter`, `oracle`, and `metrics`) and includes that tag in each evaluation cache key, alongside config, scenario set, split, k, and model identity.
- `tests/test_metrics.py` exercises each new comparison rule, and `tests/test_gate.py` exercises gate rejection wording, evaluator-tag cache invalidation, and champion-score reuse only under an unchanged key.
- The scoped signatures and data shapes agree with `INTERFACES.md` and `pregame/contracts.py`; no secret material, MongoDB transaction/validator/ServerApi regression, or path allowing the improver to grade or edit the frozen yardstick was found in the scoped files.
- The full mandated test suite completed successfully, including the fake demo coverage for rejecting the broad change and committing the narrow one.
