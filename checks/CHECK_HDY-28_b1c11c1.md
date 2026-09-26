VERDICT: PASS WITH FIXES

..................                                                       [100%]

## Blocking findings

None.

## Other findings

1. `pregame/versions.py:296-330` — `seeded_keys` is allocated outside the callback passed to `run_txn()` and `_seed_one()` mutates it. On Atlas, `with_transaction` may invoke that callback more than once, so a retry retains the keys appended by the aborted attempt and the eventual `seed` ledger payload can contain every key twice (or more). This violates `run_txn`'s requirement that callbacks have no side effects outside the database and makes the audit receipt inaccurate under a normal transaction retry. Smallest fix: create the seeded-key list inside `_txn`, pass it into `_seed_one` (or derive the fixed list without mutation), and add a unit test with a retrying transaction stub that invokes the callback twice while discarding the first attempt's database writes.

2. `pregame/versions.py:290-293` — `seed_configs()` treats the presence of any one head as proof that all ten heads, versions, and the seed ledger entry exist. A failed non-transactional mock seed or pre-existing partial database therefore makes every later call silently no-op in a permanently incomplete state; that is not idempotent initialization. Smallest fix: check for the complete expected seed state (and either repair missing seed records safely or raise a clear consistency error) rather than returning when the head count is merely nonzero, with a regression test starting from one seeded head.

## What I checked and found correct

The mandated command exited 0 with the exact result line shown above. The previous report's blocking late proposal-status race is now covered by a regression test and mock cleanup restores the head, version, ledger, and proposal snapshots. Target-version collisions and initially missing/ineligible proposals change nothing; ledger verification rejects `_id != seq`; public signatures, defaults, version-independent config hashing, rollback-as-new-version behavior, validators, and required indexes match the specification. `get_db()` uses `ServerApi("1")` without strict API mode, and the scoped files contain no committed credentials or secret-printing code. Nothing in the scoped database/version code exposes held-out data or lets the improver edit its oracle, scenarios, ledger, or gate yardstick.
