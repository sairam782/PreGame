VERDICT: SEND BACK
.......................                                                  [100%]

## Blocking findings

1. `pregame/versions.py:123-130` — `_diagnose_seed_state()` verifies that the document selected by a current head has the expected `kind` and `key`, but it never verifies `current_doc["version"] == head_version`. For example, `policy:insurance` may point to version 2 while `policy:insurance@v2` claims `version: 99`; `seed_configs()` then treats the database as completely seeded, and `resolve_field_config()` reports version 99 even though the head is 2. This violates the insert-only version identity invariant and the done-line requirement that incomplete/inconsistent seeding refuse loudly, and it can make receipts and subsequent Atlas commits disagree about the configuration version in use. Smallest fix: include `current_doc.get("version") != head_version` in the current-document consistency check and add a regression test that corrupts the embedded version of the head target and expects `SeedInconsistent`.

## Other findings

None.

## What I checked and found correct

The mandated command exited 0 with the exact pytest result line shown above (`23 passed`). The prior finding is otherwise substantially fixed: a legitimately advanced head is accepted, a head pointing to a missing version and a missing seed-ledger entry are rejected, and callback retries use a stable ten-key seed payload. Stale commits, target-version collisions, missing or ineligible proposals, and a late proposal-status race preserve the tested state under mongomock; the conditional head update remains the Atlas transaction fence. Public signatures, body-only hashing, pure deep-copy replacement, oldest-first history, rollback-as-new-version behavior, and default seed coverage match `INTERFACES.md`. The scoped files contain no secrets or secret printing and do not let the improver read or edit the oracle, held-out scenarios, ledger rules, gate code, or metric definitions.
