VERDICT: SEND BACK

....................                                                     [100%]

## Blocking findings

1. `pregame/versions.py:317-326` — the required partial-seed fix is incomplete. `seed_configs()` declares the database fully seeded based only on the presence of the ten expected head IDs and ten v1 version IDs. It does not verify that each head is actually at version 1, that each v1 document corresponds to the expected kind/key/version, or that the required `seed` ledger receipt exists. Consequently, a failed or damaged initialization with all IDs present but a missing ledger entry (or a head pointing at another version) silently no-ops, despite the interface requiring “heads at 1; ledger `seed`.” This leaves the audit chain incomplete and can make resolved configuration fail later or use an unintended version in the live Atlas demo. Smallest fix: before returning, validate every expected head’s `version == 1`, validate the identifying fields of every expected v1 document, and require the matching seed ledger entry; otherwise raise `SeedInconsistent`. Add regression tests for a complete set of heads/versions with the seed ledger removed and for an expected head whose version is not 1.

## Other findings

None.

## What I checked and found correct

The mandated command exited 0 with the exact pytest result line shown above. The transaction-retry ledger-key fix uses a fixed, non-mutated ten-key list, so callback retries do not duplicate the payload. Stale-base, target-version-collision, missing/ineligible-proposal, and late proposal-race paths are covered and preserve the tested collections under mongomock; the conditional head update remains the Atlas transaction fence. Public signatures, pure body-only hashing, deep-copy replacement, oldest-first history, rollback-as-new-version behavior, and seed defaults otherwise match `INTERFACES.md`. The scoped code contains no secrets or secret printing and does not expose or modify the oracle, held-out scenarios, ledger rules, gate code, or other grading yardsticks.
