# Pregame data-flow and MongoDB audit (parallel auditor: data flow), 26 Sep 2026, commit 6da8464

## Verdict (3 lines)
1. The MongoDB spine holds up. Event claims are exactly-once, the fence transaction is well built, the ledger cannot fork and hashes survive the BSON round trip, validators match what the code writes, and the eval cache is keyed properly. The test suite passes (260 tests, run locally).
2. The main gap is that the fence checks one surface, but the gate evaluates all four. I reproduced a policy commit landing on top of a rules change that no held-out run ever evaluated. A tier-H approval, which waits minutes to hours for a human, makes this likely.
3. For the demo: stop Ctrl-C from stranding a proposal in `evaluating` (5 min), and narrate the ledger honestly. It records which ids and versions were used, not their contents: a config body or brief edited in place still shows "ledger verified ✓".

Method: I read the flow end to end (store -> compiler -> loop/drafter -> oracle -> gate._summary -> proposals -> versions.commit -> ledger -> web). I ran `pytest -q` (all pass) and one mongomock-only script with the fake LLM: `scratchpad/audits/parallel/exp_dataflow.py`. That script made no network calls, no model calls and no writes to the repo. Its results are labelled [exp A-E] below. Findings 1-9 from the prior audit are not repeated.

---

## Findings, ranked

### 1. Medium-high: the fence is per surface, but the gate evaluates a whole four-surface config, so unevaluated combinations get committed
- **Evidence.**
  - The gate resolves the champion once, at the start of evaluation (`pregame/gate.py:577`). It builds the candidate as champion plus one surface (`gate.py:594`) and scores that whole config (`gate.py:598-610`).
  - It then commits only `(kind, key, base)` (`gate.py:637`). `versions.commit` pre-checks and bumps only that one head (`pregame/versions.py:248-269`).
  - `approve` does the same: it checks only its own head (`gate.py:669-676`), possibly hours after the held-out run.
  - [exp B] I committed a `rules:retirement` change while a policy proposal was in its held-out phase. The policy proposal was still `committed`, and the live versions are now `{policy:2, rules:2}`. `config_hash(live) != hash(evaluated candidate)`, so the running config was never evaluated.
- **Why it matters.** "The gate proved this config wins, then the fence committed it" is the core claim, and here it is false. `run_demo` already shows the risky shape: improve #2 commits policy (G), then improve #3 leaves a rules change `awaiting_owner` (`pregame/loop.py:405-436`). Any other `improve` or `rollback` on that field before `approve` breaks the claim. Guardrails are `global`, so a guardrails commit changes every field's config under every in-flight proposal.
- **Smallest fix.**
  - Store `evaluated_versions = champ_cfg["versions"]` in `stored` (`gate.py:612`).
  - Before `versions.commit` in the tier-G path, and in `approve` before `gate.py:670`, compare it with `resolve_field_config(db, field)["versions"]`. On a mismatch, mark the proposal stale.
  - That closes the human-scale window. A real transactional fence needs one more step: inside `_txn`, `$inc` a `touch` field on the other three heads, filtered by their expected versions. Reading them alone in a snapshot transaction does not produce a write conflict.
- **Minutes.** 15 for the check; 35-45 for the full fence plus a test.

### 2. Medium (demo risk): an interrupted gate strands the proposal in `evaluating` forever, and the winning idea is lost
- **Evidence.**
  - The proposal is claimed `pending -> evaluating` (`gate.py:528`).
  - It is handed back only on `except Exception` (`gate.py:541-547`). Ctrl-C (`KeyboardInterrupt`) and a killed process skip that handler.
  - A non-pending proposal is returned unchanged (`gate.py:526-527`), and `pending()` selects only `status == "pending"` (`gate.py:348-353`).
  - No CLI command re-queues a proposal (`pregame/cli.py:262-311`).
  - The improver sees the stranded proposal as "past" (`pregame/improver.py:300`, `:407`).
  - [exp C] A `KeyboardInterrupt` during held-out evaluation left `prop-… evaluating`. Re-running `improve` then filed a *different* proposal, which was rejected, and `pending()` returned 0. The committed beat is gone until `setup`.
- **Why it matters.** A live held-out evaluation takes minutes of model calls, so a nervous Ctrl-C on stage is plausible. The page would show `evaluating` for the rest of the demo.
- **Smallest fix.**
  - Change `except Exception` to `except BaseException` at `gate.py:543`. It re-raises, so Ctrl-C still exits.
  - After the hackathon, add `evaluating_since` plus a lease, or a `requeue` command.
- **Minutes.** 5 (plus 5 for a test).

### 3. Medium (claim accuracy): ledger receipts bind ids and versions, not contents, so edits in place still verify
- **Evidence.**
  - The commit and rollback payload holds kind, key, version, base_version, rationale, proposal_id and restores, but no body hash (`versions.py:301-316`). The seed payload lists only keys (`versions.py:430-437`).
  - The brief payload holds ids, as_of and versions, but no text hash (`loop.py:134-147`).
  - `verify` checks only the chain's own fields (`pregame/ledger.py:124-157`). The page shows "ledger verified ✓" from it (`pregame/web/static/index.html:219-223`).
  - [exp E] After a full demo, I changed `policy:retirement@v1.body.max_facts` to 999 and overwrote one brief's text. `verify` returned `(True, 21, '')`.
- **How this differs from prior #9.** That finding says a writer could *recompute* the chain. Here nothing needs recomputing, because the chain never covered these bytes. (The proposal's `idem_key` does hash its body, `gate.py:319`, `:343`, but nothing cross-checks it against the committed version.)
- **Smallest fix.**
  - Add `body_sha256` to the commit, rollback and seed payloads and `brief_sha256` to the brief payload.
  - In `status()`, recompute each head's body hash against its ledger entry.
- **Minutes.** 25-30. Before 4 PM, fix the wording only (2 min): "the ledger attests what was used and in what order; the database holds the bytes".

### 4. Low-medium: re-firing an already-fired event redrafts, regrades and files duplicate feedback
- **Evidence.**
  - `fire_event` correctly returns `[]` on a second call (`pregame/world/store.py:122-125`).
  - `market_event` ignores that return value and drafts, grades and files feedback anyway (`loop.py:176-227`).
  - The feedback `_id` includes the brief id, so it never deduplicates (`loop.py:213`).
  - `cli fire` has no guard (`cli.py:91-94`).
  - [exp D] Firing `ret-rmd-age` twice gave 2 briefs and 2 feedback docs (`fb-ret-rmd-age-3a72820a`, `fb-ret-rmd-age-ee35558a`).
- **Why it matters.** Each accidental re-fire costs a Sonnet call plus Haiku calls. It also feeds the improver the same complaint twice (`improver.py:109`), and adds a second `feedback` ledger entry for one event.
- **Smallest fix.** In `market_event`, if `fire_event` returned `[]` and the event is already fired, return `{"event": id, "already_fired": True}`. Alternatively, use `_id = f"fb-{event_id}"` and skip on DuplicateKey.
- **Minutes.** 5.

### 5. Low: DB-then-ledger pairs outside the fence are not atomic (extends prior #8 beyond events)
- **Evidence.**
  - Brief insert, then ledger (`loop.py:133-147`).
  - Feedback insert, then ledger (`loop.py:220-227`).
  - Gate move, then ledger: `gate.py:555-559`, `626-628`, `645-647`.
  - Approve: commit, then `_move`, decision, `approve` entry (`gate.py:675-689`).
- **Opinion.** The ordering is consistently database first and ledger second, so the ledger never runs ahead of the data. That is a sound choice. A crash between the two leaves a row with no receipt, and `verify` cannot see that.
- **Smallest fix.** After the hackathon, wrap each pair in `run_txn`. `ledger.append` already accepts `session` (`ledger.py:78`).
- **Minutes.** 30-45.

### 6. Low: `/api/state` does unbounded O(N) work every 2 s per open tab and builds a fresh LLM on every poll
- **Evidence.**
  - The page polls every 2 s (`index.html:375`).
  - `status()` re-verifies the whole ledger (`loop.py:277`, `ledger.py:133`). It also returns every proposal (`loop.py:304`) and the full config history (`loop.py:286-296`).
  - It calls `get_llm()` on each poll (`loop.py:326-333`). That constructs a new `LLM` each time (`pregame/llm.py:262-265`): an Anthropic client in live mode (`llm.py:88-89`), or a re-parse of the 226 KB cassette in replay mode (`llm.py:92-93`).
  - `usage` is a dict, not callable, and the web process never calls a model. So `llm_usage` is always `{}` [exp A], and the page never displays it.
  - [exp A] After one demo: 21 ledger entries, 4 proposals, a 64 KB payload, 7 ms on mongomock. That is fine today; it grows linearly.
- **Smallest fix.**
  - Delete the `llm_usage` block (2 min).
  - Later: cap proposals with `.limit(50)`, and cache the verify result by the last `seq`.
- **Minutes.** 2 now; 10 later.

### 7. Low: several index shapes do not match their queries (all tiny today)
- **Evidence.**
  - Facts: the index is `(field, subject, relation, valid_from)` (`pregame/db.py:336-343`). The only query is `{field, valid_from <= as_of}` sorted by `(valid_from, _id)` (`store.py:142`), so only the `field` prefix is used and the sort happens in memory.
  - Proposals have no secondary index, but are queried by `{status}` sorted by `created_at` (`gate.py:353`), by `{field}` (`improver.py:116`), and sorted by `created_sim` (`loop.py:304`).
  - The `eval_runs` query `{field: $in, split}` (`improver.py:114`) is unindexed. The unique `(config_hash, scenario_id, run)` index (`db.py:347-350`) is never queried and only duplicates `_id` uniqueness.
  - `ledger.count_documents({"kind":"seed"})` (`versions.py:134`) and the feedback sort by `sim_time` (`loop.py:316`) are collection scans.
- **Smallest fix.** Add indexes on facts `(field, valid_from, _id)`, proposals `(status, created_at)` and `(field, created_at)`, and eval_runs `(field, split)`.
- **Minutes.** 5. After the hackathon.

### 8. Low: two concurrent gates on the same config both pay for the full evaluation
- **Evidence.** The code does a cache lookup, then computes, then upserts (`gate.py:438-456`). Nothing marks an evaluation as in flight.
- **Opinion.** The result stays consistent (the last `replace_one` wins with identical content). Only the model calls are doubled. This matters only if the CLI demo and a second terminal run `improve` at once.
- **Smallest fix.** After the hackathon, insert a `{status: "running"}` row first, keyed by the same `_id`.
- **Minutes.** 20.

### Not verified here: behaviour that exists only on Atlas
- Tests run on mongomock with no transactions and no validators (`tests/conftest.py:6-9`, `db.py:133-134`, `db.py:299-311`). I confirmed by reading that every write conforms to the validators, but only Atlas enforces them.
- **Opinion.** A ledger append outside a transaction racing a commit inside one should surface on Atlas as `WriteConflict`, which triggers a `with_transaction` retry. The losing append outside the transaction should get `DuplicateKey`, which triggers its 3-try loop (`ledger.py:94-118`). I could not test this: Atlas was off-limits.
- `scripts/smoke_atlas.py` covers a two-collection transaction, but not this race. One concurrent append-vs-commit check there would close the gap (~15 min, after the hackathon).

## What is solid (my angle)
- **Exactly-once event claim.** `fire_event` uses a conditional update on `fired != true`, so a losing or concurrent fire writes nothing. The clock only moves forward, via `$max` (`store.py:122-134`).
- **The fence transaction.** Every precondition is checked before any write, the head bump is a conditional `find_one_and_update`, and the proposal flip is conditional on status. It runs with snapshot read concern and majority write concern (`db.py:137-142`). The mongomock undo path restores exactly what the call wrote (`versions.py:246-352`). Seeding is transactional and refuses a partial state (`versions.py:385-395`).
- **The ledger cannot fork.** `_id == seq` and `_id` is unique. Appends outside a transaction retry on DuplicateKey, while appends inside one defer to the transaction's retry (`ledger.py:91-118`).
- **Hashes survive the BSON round trip.**
  - Payload datetimes are stored as ISO strings, and `sim_time` is truncated to milliseconds before hashing (`ledger.py:34-47`, `87-89`).
  - Every sim time derives from `SIM_START`, which is UTC (`pregame/world/fields.py:30-37`). Both the real and the mock client use `tz_aware=True` (`db.py:115`, `tests/conftest.py:8`), so `isoformat()` matches after reading back.
- **Validators match what the code writes.**
  - The ledger's `additionalProperties:false` field set equals the entry the code builds (`db.py:216-245` vs `ledger.py:100-110`), and the kind enum covers every kind the code appends.
  - `base_version` is always an int of at least 1 (`improver.py:317`, `321`), and `created_sim` is always a datetime.
- **Eval cache.**
  - The key covers the full four-body config hash, field, split, scenario ids, model tag, k and the evaluator code (`gate.py:434-437`).
  - `setup` drops `eval_runs` together with `eval_scenarios` (`db.py:145-148`).
  - A winner's rows are reused correctly once it becomes champion, because the rows are keyed by the same config hash.
- **Idempotent filing.** Proposal filing is idempotent through the unique `idem_key` plus a DuplicateKey fallback (`gate.py:319-339`, `db.py:334`).
- **Reads while a commit lands.** Web reads stay coherent during a commit: the head bump and the version insert share one transaction, so `resolve_field_config` never sees a head without its version.

---

## Fix before 4 PM
1. #2: `except BaseException` in `evaluate_proposal` (`gate.py:543`). 5 min.
2. #1: the 15-min version-vector check in the tier-G path and in `approve` (mark stale on mismatch). If there is no time, do not claim "the committed config is exactly what won"; say "the edited surface is fenced".
3. #3: wording only. "Ledger verified" means the chain of receipts is intact, not that config and brief bytes are untouched. 2 min.
4. #4: return early when an event is already fired. 5 min, optional, but it prevents paid duplicate calls if someone re-runs `fire` on stage.
5. #6: delete the per-poll `get_llm()` block in `status()`. 2 min, optional.

## After the hackathon
- The full transactional fence across all four heads (#1).
- Content hashes in the ledger plus a head-vs-ledger cross-check (#3).
- Transactions around the DB-plus-ledger pairs (#5).
- An in-flight marker for evaluations (#8).
- A lease or requeue for `evaluating` (#2).
- Index shapes (#7).
- A cap and cache for `/api/state` (#6).
- An Atlas smoke test for the append-vs-commit race.
