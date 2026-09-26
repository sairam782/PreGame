VERDICT: PASS WITH FIXES
329 passed, 5 skipped in 13.77s

## Blocking findings

None.

## Other findings

- `pregame/contracts.py:287` — `Proposal` does not declare the `evaluated_versions` field that `pregame/gate.py` now persists and requires during approval. The binding interface says shared data shapes live in `contracts.py`, so the runtime document and its advertised `Proposal` shape have diverged; typed consumers can neither rely on nor type-check the four-version fence record. Smallest fix: add `evaluated_versions: Optional[dict[str, int]]` to `Proposal` (and initialize it to `None` when proposals are constructed/filed). This is not a live Atlas blocker because the proposal validator permits additional properties.
- `pregame/versions.py:261` — the stated concurrency limit is real: the three non-target heads are read but not conditionally written/locked, so two independent gate processes changing different surfaces could both validate the same four-head snapshot and commit. With the stipulated single gate process, commits are serialized and this does not affect the demo. Smallest fix if multi-process operation becomes a requirement: introduce a shared field/global fence document updated conditionally in every config transaction, or otherwise make all four heads participate in write-conflict detection.

## What I checked and found correct

- Gate evaluation records all four resolved versions, and both tier-G commit and owner approval pass all four as `expected_heads`; stale proposals are marked `stale`.
- `versions.commit` checks expected heads before writes and keeps the target-head compare-and-set, version insertion, ledger append, and proposal transition in one transaction; its signature matches `INTERFACES.md`.
- `evaluate_proposal` catches `BaseException`, restores an interrupted evaluation to `pending`, clears partial decision data, and re-raises.
- Guardrail additions, removals, disabling, text changes, check changes, and reordering are tier H; only enabling an otherwise unchanged existing guardrail is tier G.
- Rule and guardrail ids use the required short safe full-match pattern, and both texts are capped at 300 characters.
- Code-generated rules/guardrails diffs include the exact escaped old and new text that an owner reviews before signing the body hash.
- Tests exercise cross-surface movement both before approval and inside the commit fence, missing evaluated-version data, `KeyboardInterrupt`, tier classification, validation limits, exact diff text, and improver isolation from oracle/metrics/held-out data. I found no vacuous assertions in these additions.
- The improver snapshot exposes only tuning evaluation rows and strips held-out results/hashes from proposal history; static and dynamic tripwire tests prevent it from importing or calling its yardstick.
- No credentials or connection secrets are present in the scoped implementation or tests. The fake-demo paths and expected decisions remain covered and unchanged.
- The required command completed successfully; the second run used pytest's addopts override only to expose the otherwise suppressed exact summary line reported above.
