VERDICT: PASS WITH FIXES
.........................................                                [100%]

## Blocking findings

None.

## Other findings

1. `pregame/compiler.py:96` — `_kind_ok` always admits an `account` fact whenever the `account_notes` tool is enabled, even when `policy["include_kinds"]` excludes `account`. This contradicts the binding `INTERFACES.md` sequence (“keep sources whose tool is on; keep `include_kinds`”) and makes the policy knob unable to exclude account-kind facts. `tests/test_compiler_drafter.py:135` explicitly expects the contradictory behavior, so the passing suite cannot detect this contract violation. It matters because a configured context policy is silently ignored and client notes can enter briefs that requested only other fact kinds (ownership isolation still prevents cross-client leakage). Smallest fix: make `_kind_ok` require `f["kind"] in include_kinds` for every kind, retain the independent source/tool check, and change the account-exception test to assert exclusion when `account` is omitted plus inclusion when it is present.

2. `pregame/drafter.py:147` — live-response validation performs set membership on every element of a model-provided `fact_ids` list before checking that each element is a string. A valid JSON response containing an object or array in that list raises `TypeError: unhashable type` immediately, bypassing the documented corrective retry and salvage behavior. The scalar regression at `tests/test_account_isolation.py:49` exercises `_salvage_sections` directly and therefore does not cover this validation crash. Smallest fix: classify every non-string id as invalid before set membership (for example, `not isinstance(fid, str) or fid not in valid_ids`) and add a `draft_brief` live-path regression with an object/list id in both responses.

## What I checked and found correct

The required signatures match `INTERFACES.md`; cutoff, supersession, source filtering, recency, ranking, cap, receipt construction, deterministic fake drafting, configured section order, citations, prompt inputs, retry/salvage flow, and Markdown rendering were inspected. The prior scalar-`fact_ids` finding is fixed at `pregame/drafter.py:197-200`: non-list values are dropped without iteration. Account-note ownership is checked before supersession at `pregame/compiler.py:29-30`; missing and mismatched owners fail closed, and the shared-subject tests genuinely detect cross-client replacement/leakage. The scoped implementation is pure and contains no database, transaction, Atlas ServerApi, secrets, oracle, grading, held-out-data, or yardstick-editing access.
