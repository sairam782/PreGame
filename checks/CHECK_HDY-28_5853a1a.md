VERDICT: SEND BACK

.................                                                        [100%]

## Blocking findings

1. `pregame/versions.py:221-241` — The mongomock failure compensation restores only `config_heads`; it does not remove the version or ledger entry already inserted before a late proposal-status race is detected. I reproduced this by making the final proposal update return `matched_count == 0`: `commit()` raised `StaleVersion`, the head and proposal were unchanged, but `config_versions` grew from 10 to 11 and `ledger` from 1 to 2. This directly violates the done line that a stale commit changes nothing and leaves an invalid committed version and audit receipt in test/demo databases. Smallest fix: on the mock path, snapshot the affected head/proposal and ledger tail before writes and fully undo this invocation's version, ledger, proposal, and head changes on any exception (or provide transaction emulation), then add a regression test that forces the final proposal update to lose its race and compares all four collections before and after.

## Other findings

1. `tests/test_db_versions.py:181-217` — The new missing/ineligible proposal tests exercise only pre-check failures, before any write. They therefore cannot fail when the late, write-time `matched_count != 1` branch leaves partial state under mongomock. Smallest fix: patch or wrap the proposal collection so the pre-check sees an eligible proposal but the final conditional update reports no match, then assert the complete snapshot is unchanged after `StaleVersion`.

## What I checked and found correct

The requested pytest command exited 0 with the exact result line above (17 passing test indicators). The three findings from `checks/CHECK_HDY-28_f7558d3.md` are fixed for their existing regression cases: target-version collisions are rejected before writes, missing or initially ineligible proposals are rejected before writes, and ledger verification rejects `_id != seq`. Public signatures match `INTERFACES.md`; defaults retain the deliberately improvable v1 settings and analyst notes off; hashes exclude version numbers; rollback creates a new version; validators and required indexes are configured; `get_db()` uses `ServerApi("1")` without strict API mode and does not print credentials; and the scoped code does not expose held-out/oracle data to the improver.
