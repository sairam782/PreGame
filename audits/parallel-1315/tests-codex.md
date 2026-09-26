# Pregame test/deployment-gap audit — 26 Sep 2026

**VERDICT 1/3:** The requested suite is green: `260 passed` (260 progress dots, exit 0; pytest's double-quiet configuration suppresses the textual summary at `pytest.ini:3`).
**VERDICT 2/3:** That green run does not exercise Atlas 8.0 transactions or validators at all (`tests/conftest.py:7-9`), and the gate suite replaces the exact modules that make deployment real (`tests/test_gate.py:3-4`, `tests/test_gate.py:154-158`).
**VERDICT 3/3:** **Opinion:** before 4 PM, add one real-Atlas commit test, one real-module gate integration test, and one validator/index/time round-trip test; treat mongomock as unit coverage, not deployment evidence.

## Findings, ranked by severity

### 1. Critical — No test proves the production commit is one atomic Atlas transaction

- **File:line:** `tests/conftest.py:7-9`; `pregame/db.py:127-142`; `pregame/versions.py:263-348`; `tests/test_db_versions.py:317-350`.
- **Why it matters:** all repository tests take `run_txn`'s `fn(None)` branch. Failure/race tests therefore prove the mock-only manual compensation path, not snapshot/majority transaction rollback, callback retry behavior, session propagation, or concurrent fencing on Atlas. The retry test explicitly simulates abort by deleting the database (`tests/test_db_versions.py:153-174`), so it cannot expose a PyMongo/Atlas transaction error.
- **Smallest fix:** run one isolated Atlas test that injects a failure after the head update and asserts head/version/ledger/proposal are unchanged, then races two commits and asserts exactly one wins.
- **Minutes:** 20–30 if credentials and a scratch database are ready.

### 2. High — `test_gate.py` verifies a parallel fake system, not the deployable gate composition

- **File:line:** `tests/test_gate.py:3-4`, `tests/test_gate.py:74-158`; production composition is `pregame/gate.py:378-430`, `pregame/gate.py:574-653`, and `pregame/versions.py:218-352`.
- **Why it matters:** the fixture replaces `versions`, `oracle`, `metrics`, `ledger`, `config`, and `world.store` in `sys.modules`; its fake `commit`, evaluator, comparison, and ledger are independently reimplemented (`tests/test_gate.py:89-147`). Thus decision-path tests cannot catch signature/shape drift, real cache-key/query mistakes, real metric changes, actual oracle output, real ledger hashing, or gate-to-transaction failures. For example, `test_g_win_commits_automatically` asserts calls recorded by the fake (`tests/test_gate.py:344-354`), not a real version document/receipt created by `versions.commit`.
- **Smallest fix:** add one test using all real modules with mongomock + fake LLM: seed, file a deterministic winning proposal, evaluate, then assert the real head, version, proposal, cache rows, and verified ledger agree.
- **Minutes:** 15–25.

### 3. High — Atlas validators and `collMod` privilege/error handling have zero executable coverage

- **File:line:** validators are deliberately skipped for mocks at `pregame/db.py:290-311`; production applies/compares them and special-cases error code 13 at `pregame/db.py:255-287`; the only init test checks collection names/idempotence at `tests/test_db_versions.py:30-40`.
- **Why it matters:** no test proves Atlas accepts the schemas, rejects invalid BSON, reports options in the compared shape, permits initial creation, or maps Unauthorized (`OperationFailure.code == 13`) to the promised actionable error. A green suite would survive a broken schema, `collMod` command, permission assumption, or error-code branch.
- **Smallest fix:** against a scratch Atlas DB, initialize, insert one invalid document into each validated collection and require rejection; change one validator (or use a lower-privilege user) and assert the precise setup failure.
- **Minutes:** 15–25.

### 4. High — Mongo-specific index guarantees are only partially tested

- **File:line:** production unique indexes are `proposals.idem_key`, `eval_runs(config_hash, scenario_id, run)`, and `ledger.seq` at `pregame/db.py:329-354`; proposal code relies on duplicate-key handling at `pregame/gate.py:319-337`; version and ledger collision handling is at `pregame/versions.py:294-298` and `pregame/ledger.py:94-121`.
- **Why it matters:** mongomock does exercise some duplicate insertion, but no test inspects Atlas index definitions or verifies server error details/labels. Atlas already differs materially here: it rejects an explicit unique `_id` option that mongomock accepted (`pregame/db.py:330-332`). A missing/misdeclared cache or idempotency index can allow duplicate work despite all unit tests passing. **Inventory note:** production defines no partial indexes and uses no `$expr`; therefore there is no current partial-index or `$expr` semantic dependency to test (`pregame/db.py:329-354`).
- **Smallest fix:** after real `init_db`, assert `index_information()` keys/options, then provoke duplicate `idem_key`, eval-run tuple, and ledger sequence inserts and assert `DuplicateKeyError` (avoid pinning message text).
- **Minutes:** 10–15.

### 5. Medium-high — BSON datetime behavior is asserted weakly, not round-tripped at the boundary

- **File:line:** the real client requires `tz_aware=True` at `pregame/db.py:111-116`; ledger correctness depends on BSON millisecond truncation at `pregame/ledger.py:14-18`, `pregame/ledger.py:34-42`, `pregame/ledger.py:87-109`; the only store assertion merely checks non-null `tzinfo` at `tests/test_world.py:389`.
- **Why it matters:** no Atlas test proves UTC normalization, millisecond loss, nested payload datetime canonicalization, or hash verification after a genuine BSON round trip. Mongomock is assumed to mimic precision (`pregame/ledger.py:14-16`); if it diverges, the audit-chain test at `tests/test_db_versions.py:384-402` gives false confidence.
- **Smallest fix:** insert a non-UTC, microsecond-bearing time through `ledger.append` on Atlas, read it back, assert UTC-aware millisecond truncation, and require `ledger.verify()` success.
- **Minutes:** 8–12.

### 6. Medium — Several “end-to-end” assertions permit substantial regressions

- **File:line:** setup only asserts positive counts (`tests/test_loop.py:41-43`); the initial receipt allows an empty fact list because it checks only `is not None` (`tests/test_loop.py:47-49`); market feedback checks only key presence (`tests/test_loop.py:56-62`); status/API tests check top-level keys rather than values/shapes (`tests/test_loop.py:94-105`, `tests/test_loop.py:108-123`).
- **Why it matters:** these tests can pass with wrong counts, an empty-context brief, `call_accuracy=None`, an unverified ledger flag, empty/malformed field state, or incomplete API serialization. They are not literally unable to fail, but they assert too little to support their “end-to-end” label (`tests/test_loop.py:1-6`).
- **Smallest fix:** assert exact setup cardinalities, non-empty cited facts and stored brief identity, numeric bounded accuracy, `ledger_verified is True`, and a minimal nested response schema.
- **Minutes:** 10–15.

### 7. Medium — Sort determinism is uneven at Mongo query boundaries

- **File:line:** facts deliberately tie-break by `_id` (`pregame/world/store.py:142`), but recent briefs, feedback, versions, proposals, and UI lists sort only by potentially equal timestamps/versions (`pregame/improver.py:103-116`, `pregame/cli.py:144`, `pregame/loop.py:304-316`).
- **Why it matters:** MongoDB does not promise stable ordering for equal sort keys; mongomock's insertion order can make selection/display deterministic in tests. This can change which limited briefs/feedback reach the improver and reorder proposals between local tests and Atlas. No test constructs equal-key rows and compares deterministic selection; existing loop/API checks only presence (`tests/test_loop.py:101-105`, `tests/test_loop.py:120-123`).
- **Smallest fix:** add `_id` as the final sort key on limited queries and test tied timestamps inserted in reverse order.
- **Minutes:** 8–12.

## MongoDB 8.0 difference checklist

| Difference | Production dependency | Would an existing test catch an Atlas regression? |
|---|---|---|
| Transactions/retries | `pregame/db.py:127-142`; atomic fence `pregame/versions.py:263-348` | **No**; mock branch only (`tests/conftest.py:7-9`). |
| Validators/`collMod` | `pregame/db.py:255-311` | **No**; skipped under mock, collection names only (`tests/test_db_versions.py:30-40`). |
| Unique indexes/error type | `pregame/db.py:329-354`; handlers `pregame/gate.py:319-337`, `pregame/ledger.py:94-121` | **Partial** under mongomock; no Atlas index/options/error-label check. |
| TZ-aware dates/ms precision | `pregame/db.py:111-116`; `pregame/ledger.py:14-18`, `pregame/ledger.py:34-42` | **Partial**; awareness only (`tests/test_world.py:389`) and mock round trip (`tests/test_db_versions.py:384-402`). |
| `$expr` | No production use found; queries use ordinary predicates, e.g. `pregame/world/store.py:142`. | Not applicable. |
| Sort stability | Untied limited sorts at `pregame/improver.py:103-116` and `pregame/loop.py:304-316` | **No** tied-key test. |
| Partial indexes | None created; complete index inventory is `pregame/db.py:329-354`. | Not applicable. |
| Server error codes | Authorization branch assumes code 13 at `pregame/db.py:273-280`; duplicate-key classes at `pregame/versions.py:294-298`. | **No** real-server test. |

## Three highest-value tests to add before 4 PM (smallest first)

1. **`test_atlas_datetime_and_unique_invariants` (8–12 min):** on a scratch Atlas DB, run `init_db`, assert index definitions, provoke all three duplicate constraints, append a non-UTC microsecond timestamp, and verify the round-tripped ledger. It cheaply covers indexes, server errors, TZ awareness, and BSON precision (`pregame/db.py:111-116`, `pregame/db.py:329-354`, `pregame/ledger.py:87-121`).
2. **`test_gate_real_modules_commit_a_winner` (15–25 min):** use mongomock but no `sys.modules` replacements; seed real scenarios/config, run a deterministic real gate evaluation, and assert cache rows, head/version/proposal consistency, and `ledger.verify()`. It closes the composition hole created at `tests/test_gate.py:74-158`.
3. **`test_atlas_commit_is_atomic_and_fenced` (20–30 min):** on Atlas, fail after the conditional head update and assert complete rollback, then race two commits from one base and assert one commit/receipt. It validates the central deployment claim at `pregame/versions.py:224-236` through the real transaction implementation at `pregame/db.py:127-142`.

