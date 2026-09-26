VERDICT: SEND BACK

.............                                                            [100%] (13 passed; exit code 0)

## Blocking findings

1. `pregame/versions.py:147-173` — A stale commit can change state under mongomock. `commit()` advances the head before inserting the new version; if that insert raises `DuplicateKeyError`, the code converts it to `StaleVersion`, but `run_txn()` has no rollback under mongomock. Reproduction with an orphan `policy:insurance@v2` and head v1 leaves the head at v2 after `StaleVersion`, while no valid commit/ledger entry was written. This contradicts the done line (“a stale commit changes nothing”), leaves the head pointing at an unrelated/orphan document, and the existing stale test covers only failure of the first conditional update. Smallest fix: make the mongomock path atomic by restoring the prior head/removing any writes made by this invocation on failure (or provide a transaction-emulating test path), and add a regression test that pre-creates the target version id, asserts `StaleVersion`, and asserts head, versions, proposal, and ledger are byte-for-byte/count unchanged.

## Other findings

1. `pregame/versions.py:193-198` — Supplying `proposal_id` does not guarantee that proposal becomes `committed`: the update result is ignored, so a missing, rejected, stale, or already committed proposal still permits the version/head/ledger commit. The interface explicitly requires “if `proposal_id`, proposal status `committed`”; this also weakens the fence by allowing a config version to claim provenance from a proposal that was never eligible. Smallest fix: validate the proposal id and eligible status before any writes (needed for mongomock safety), require `matched_count == 1` inside the transaction to close races on Atlas, and abort otherwise; add missing/ineligible-proposal tests.

2. `pregame/ledger.py:128-149` — `verify()` never checks the contract invariant `_id == seq`. A one-entry ledger whose unchanged, valid seq-1 entry is reinserted with `_id: 99` returns `(True, 1, "")`. That lets structural tampering pass verification and can make append ordering diverge from the hash-chain sequence because reads sort by `_id`. Smallest fix: reject an entry when `entry.get("_id") != seq`, before checking the chain/hash, and add a tamper regression test.

3. `tests/test_db_versions.py:106-123` — The stale-commit test cannot detect the post-head-update failure above because it uses a mismatched base version, which fails before the first write. Smallest fix: add the target-version collision case described in blocking finding 1.

## What I checked and found correct

The requested test command completed successfully. Public function signatures match `INTERFACES.md`; defaults have the specified deliberately limited v1 policy, analyst notes off, and the three enabled guardrails. `get_db()` uses `ServerApi("1")` without strict mode and does not print the URI. Atlas writes in `commit()` are grouped through `with_transaction`, the normal stale-base check occurs before later writes, config hashes exclude version numbers, rollback creates a new version carrying the selected body, ledger hashes bind the documented fields, required indexes and strict validation mode are configured, and the scoped files contain no committed credentials or code that exposes held-out/oracle data to the improver.
