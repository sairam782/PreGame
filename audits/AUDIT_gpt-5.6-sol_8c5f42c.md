# Pregame independent audit — 26 Sep 2026

## Five-line verdict

1. **Pregame is a strong hackathon prototype with a real governed-change loop, not a fake “agent rewrites its prompt” demo.**
2. **Do not claim that the gate proves improvement:** per proposal and field it has only six held-out scenarios, no uncertainty/significance test, and an adaptive improver learns pass/fail from repeated use.
3. **The most important correctness gap is that guardrail violations, uncited claims, missed-change counts, and `pass^k` are computed but do not gate deployment.**
4. **MongoDB is genuinely central for state, version fencing, evaluation caching, and audit receipts, but “frozen/insert-only” and improver isolation are application conventions rather than Atlas-enforced security.**
5. **For the 4 PM demo: use a pre-recorded replay, tighten the gate, smoke-test the real Atlas transaction, and narrate the statistical and in-process-isolation limits plainly.**

## Top 10 findings, ranked by severity

### 1. Critical — The gate can deploy a brief that regresses enabled guardrails

**Evidence.** A grade contains `uncited_claims` and `guardrail_violations` (`pregame/oracle.py:445-467`), but the summary drops both fields (`pregame/metrics.py:54-68`). The actual win decision checks mean accuracy, worst accuracy, false alarms, stale claims, and context size only (`pregame/metrics.py:71-128`). Although `pass_k` and `missed_changes` are summarized (`pregame/metrics.py:62-64`), neither participates in `compare`. Production briefs do fail closed on enabled guardrails (`pregame/loop.py:102-117`), but the deployment gate does not prove that the candidate can produce a shippable brief.

**Why it matters.** A candidate can gain answer-key accuracy while adding advice or invalid citations, win the gate, then cause live briefs to be blocked. A newly added/tightened guardrail can also auto-commit without the gate requiring that it actually passes. This directly weakens the claim that companion metrics prevent Goodhart behavior.

**Smallest fix.** Add mean/max `uncited_claims` and a `guardrail_violations` count to `EvalSummary`; require both to be zero or no worse than champion. Also require `pass_k` not to fall and `missed_changes` not to rise. Add one test where accuracy rises but advice/uncited claims cause rejection.

**Effort:** 30–40 minutes.

### 2. High — Six held-out meetings per field do not support a “proved improvement” claim

**Evidence.** The generator creates two seeds over months 1–6, with months 4–6 held out (`pregame/world/scenarios.py:46-49`, `pregame/world/scenarios.py:589-600`). A proposal is evaluated for one field (`pregame/gate.py:566-584`), so its held-out comparison contains only 2 × 3 = 6 scenarios, not 36. `compare` applies a fixed 0.05 mean margin and a worst-scenario floor, with no confidence interval, paired test, or multiplicity correction (`pregame/metrics.py:71-128`). README calls these “36 held-out meetings,” but 36 is the total across tuning and held-out for all three fields (`README.md:51-53`; `pregame/world/scenarios.py:589-600`).

**Opinion.** This is useful smoke-test evidence for a demo, not reliable evidence of general improvement. With roughly 5–7 questions per scenario (`pregame/world/scenarios.py:563-565`), one or two answer changes can clear a 0.05 mean margin.

**Smallest fix.** Change stage language to “won this small simulated holdout.” Correct README to “18 held-out meetings overall; 6 per field.” If time allows, show paired per-scenario deltas and require at least four of six scenarios to improve or tie with no regressions.

**Effort:** 15 minutes for honest wording; 35–50 minutes for a paired sign rule and tests.

### 3. High — Repeated proposals adapt to the supposedly held-out set

**Evidence.** Every proposal stores a held-out decision (`pregame/gate.py:591-603`). The snapshot removes numeric held-out summaries but retains status/tier and masks numbers in the decision rather than removing the outcome (`pregame/improver.py:21`, `pregame/improver.py:226-231`). The live improver is explicitly shown all past proposal outcomes (`pregame/improver.py:406-408`) and told not to repeat rejected ideas (`pregame/improver.py:363-376`). The held-out set is fixed and reused (`pregame/world/store.py:79-93`).

**Why it matters.** “Lost on held-out” is one bit of held-out feedback. Across enough proposals, that is adaptive test-set selection—the improver can hill-climb against the same six cases without seeing their questions. This contradicts the stronger impression that the held-out set is never leaked or tuned against.

**Smallest fix.** Hide held-out outcomes from subsequent improver prompts, cap a demo cycle to one candidate, and count held-out peeks. Longer term, rotate/finalize a second untouched acceptance set.

**Effort:** 20–30 minutes.

### 4. High — Fake-mode wins are artifacts, and `k=2` adds no reliability in fake mode

**Evidence.** The fake drafter is deterministic (`pregame/drafter.py:265-307`) and the fake reader deterministically selects the sentence with the most keyword overlap (`pregame/oracle.py:385-402`). Fake evaluations execute the same job serially `k` times (`pregame/oracle.py:507-516`), so repeated runs are identical. Questions are generated from the same fact structures and templates that supply the keyed terms (`pregame/world/scenarios.py:504-566`).

**Opinion.** Fake mode is excellent for exercising control flow, but its score is not evidence about Sonnet/Haiku behavior. `pass^2` sounds stochastic while being a duplicate computation in the stage-safe path.

**Smallest fix.** Label fake scores “deterministic harness test.” Show pre-recorded live results for the quantitative claim, including model IDs and run date; otherwise omit `pass^k` from the fake demo narration.

**Effort:** 10 minutes for labeling; recording time depends on API latency.

### 5. High — The first cold live proposal is about 73 model calls, not “roughly 50”

**Evidence.** Each evaluation run drafts once and reads once (`pregame/oracle.py:501-505`). Per field there are six tuning and six held-out scenarios (`pregame/world/scenarios.py:46-49`, `pregame/world/scenarios.py:589-600`). A cold proposal evaluates champion and candidate at tuning `k=1`, then both at held-out `k=2` (`pregame/gate.py:571-584`): 12 × 2 + 24 × 2 = 72 calls, plus one improver call. Corrective JSON retries and live brief redrafts can add calls (`pregame/llm.py:117-126`; `pregame/loop.py:110-116`). Concurrency is capped at eight evaluation workers (`pregame/oracle.py:21`, `pregame/oracle.py:511-512`) and three Claude CLI calls (`pregame/llm.py:86-87`). The architecture page says roughly 50 (`context/architecture.html:371`).

**Why it matters.** This is the most likely stage failure: rate limits, a five-minute CLI timeout, one malformed response, or cassette misses can stall the centerpiece. Champion caching reduces later cycles, but not the first cold run.

**Smallest fix.** Never run a cold live gate on stage. Prewarm champion caches and record the exact candidate flow; rehearse replay from a clean database. Correct the call-count claim.

**Effort:** 30–45 minutes plus one live recording run.

### 6. Medium-high — Improver isolation is a good data-transfer boundary, not a security boundary

**Evidence.** `build_snapshot` performs permitted database reads and copies them to plain data (`pregame/improver.py:90-117`); `ImproverView` retains that snapshot rather than the database (`pregame/improver.py:120-139`). That is solid. However, the trusted application process still owns the unrestricted database and constructs the view (`pregame/loop.py:241-255`), while MongoDB has one ordinary client path (`pregame/db.py:96-117`) and no role/view setup. README accurately admits isolation is in-process and suggests a separate database user (`README.md:51-54`).

**Opinion.** “The model has no tools and never receives the yardstick” is supportable. “Atlas prevents the improver from reaching the yardstick” is not implemented. The demo tamper refusal proves proposal validation, not database-level denial.

**Smallest fix.** Use the honest README wording on stage. Do not attempt Atlas custom roles before 4 PM unless already provisioned; relabel the demo as an application capability boundary.

**Effort:** 5–10 minutes.

### 7. Medium-high — Evaluation cache reuse is not tied to scenario content, prompt code, or model revision

**Evidence.** The cache key hashes only sorted scenario IDs, a configuration hash, `k`, and the configured drafter/reader model strings (`pregame/gate.py:407-418`). It does not hash scenario contents, oracle/compiler/drafter code, prompts, or a provider/model revision. Stored scenarios refuse changed reloads under an existing ID (`pregame/world/store.py:79-93`), which protects one database, but a code/prompt deployment with the same config and IDs can reuse old scores.

**Why it matters.** A stale champion cache can compare today’s candidate calls against yesterday’s champion behavior. That is especially risky when rehearsing, switching fake/live/replay, or changing code shortly before the demo.

**Smallest fix.** Add an explicit evaluator version string to the cache key and bump it now; clear/prewarm `eval_runs` after any evaluator/prompt/model change.

**Effort:** 15–25 minutes.

### 8. Medium — Event firing is claimed once but is not an atomic event commit

**Evidence.** The event is first marked fired with a conditional update, then facts are inserted one by one, then the clock and ledger are updated separately (`pregame/world/store.py:122-136`). Only configuration commits use the transaction helper (`pregame/versions.py:350-352`).

**Why it matters.** A crash or network error after the claim can permanently leave an event marked fired with only some facts, no clock movement, or no ledger receipt; retry returns no work. This is unlikely in a three-minute demo but is a real correctness hole in the “events land as facts” path.

**Smallest fix.** Do not refactor today. Preload and verify stage data, and avoid firing an event over a flaky connection. After the hackathon, wrap claim/facts/clock/ledger in a transaction or add a recoverable state machine (`pending → applying → fired`).

**Effort:** 5 minutes mitigation; 45–75 minutes robust fix.

### 9. Medium — “Strict schema” and “insert-only/frozen” overstate what Atlas enforces

**Evidence.** Only `proposals`, `config_versions`, and `ledger` receive validators (`pregame/db.py:28-44`). Proposal and version schemas allow arbitrary additional properties (`pregame/db.py:155-185`, `pregame/db.py:189-213`); proposal `kind` is merely a string (`pregame/db.py:175-181`), and configuration `body` has no BSON shape constraint (`pregame/db.py:194-210`). Facts and evaluation scenarios have no validators or write-protection. The ledger hash is an integrity detector (`pregame/ledger.py:124-157`), not protection against a writer recomputing a chain.

**Opinion.** MongoDB is still core, but these invariants are mostly maintained by Python and `_id` uniqueness, not database authorization. Say “validated and verified by the app” rather than implying tamper-proof storage.

**Smallest fix.** Fix the narration today. Later, add narrow Atlas roles, validators for facts/scenarios/heads/eval rows, and deny update/delete on append-only collections.

**Effort:** 10 minutes for wording; several hours for hardening.

### 10. Medium — The repository cannot be independently tested in this audit environment

**Evidence.** `README.md:24-31` says to install dependencies and reports 252 tests. The latest check reports a passing suite (`checks/CHECK_HDY-37_6f68300.md:1-18`). In this clean copy, both `pytest -q` and `python -m pytest -q` failed because pytest is not installed; the work order prohibited installing packages. Tests themselves use mongomock with no transactions (`tests/conftest.py:7-9`), while the key production claim depends on a real Atlas transaction (`pregame/db.py:127-142`).

**Why it matters.** Prior green checks are useful evidence, but they do not replace a final run from the exact stage environment or an Atlas smoke test. Mock compensation code cannot prove real transaction permissions/connectivity.

**Smallest fix.** Run the documented suite in the project virtual environment, then run `scripts/smoke_atlas.py` from the exact laptop/network used on stage and save its output.

**Effort:** 10–20 minutes.

## Bloat to cut

**Opinion.** Do not delete working code before the deadline. Cut demo surface and claims, not files.

- Show one field, one event, one rejected broad policy, one accepted narrow policy, and one frozen-surface refusal. The three-domain world and four-step fake proposal ladder exist (`pregame/improver.py:442-512`), but demonstrating all of them dilutes the central loop.
- Skip live rule, tool, and guardrail mutation. The tier matrix is valuable design depth (`DESIGN.md:38-48`); it does not all need stage execution.
- Do not demo the web page, CLI trace, rollback, approval, and tamper flow separately. Pick at most two audit artifacts: the version/receipt comparison and ledger verification. The CLI exposes all these commands already (`INTERFACES.md:128-137`).
- Treat the large architecture page as leave-behind material. Its improvement-hook backlog (`context/architecture.html:410-427`) is thoughtful but irrelevant to a no-slides, three-minute judging window.
- Avoid adding vector search, change streams, OpenRouter, per-household inheritance, a classifier guardrail, or an owner UI today; they are explicitly future hooks (`context/architecture.html:418-427`).
- If simplification after the hackathon is desired, consolidate repeated lazy imports and fake/live forks. Before 4 PM, churn here creates more risk than value.

## Shaky assumptions in the plan

- **Evidence:** The research digest recommends 3–5 trials per seed, paired differences, a best-of-N equal-budget baseline, and no regression on earlier months (`context/self_improvement_digest.md:14-16`, `context/self_improvement_digest.md:73-76`). The implementation uses `k=2`, no best-of-N control, and only checks that tuning mean does not decrease before testing six held-out scenarios (`pregame/gate.py:571-585`). **Opinion:** Say “inspired by the research,” not “does this the way the research says works.”
- **Evidence:** The plan recommends a tuning-only Atlas view/user and invisible oracle/ledger (`context/self_improvement_digest.md:80-81`), while implementation isolation is an in-memory snapshot (`pregame/improver.py:90-139`). **Opinion:** The current boundary is adequate against prompt-level model behavior, not malicious code in the trusted process.
- **Evidence:** The reader model is part of the yardstick (`pregame/oracle.py:313-414`), yet there is no human-labeled calibration set. The architecture itself lists that as future work (`context/architecture.html:424-426`). **Opinion:** Code grading removes an LLM judge, but it does not remove model measurement error—the reader can fail to extract content that a human advisor would understand, or succeed via lexical artifacts.
- **Evidence:** Answer correctness is term matching (`pregame/oracle.py:102-138`). Balance questions with no keys pass whenever forbidden terms are absent (`pregame/oracle.py:114-130`). **Opinion:** This rewards evasive or irrelevant answers on some balance cases; accuracy is not synonymous with brief usefulness.
- **Evidence:** Scenario questions and keyed terms are generated from the same synthetic world (`pregame/world/scenarios.py:418-440`, `pregame/world/scenarios.py:504-566`). **Opinion:** The holdout measures generalization across later synthetic templates and values, not across real advisors, households, or question styles.
- **Evidence:** Feedback is scripted text attached to each event and is filed only when the simulated oracle misses a change (`pregame/loop.py:190-227`). **Opinion:** The “client review discovers a miss” loop is partially circular: the same synthetic generator defines what matters, checks the brief, and selects prewritten feedback.
- **Evidence:** Policy changes are tier G and auto-commit (`pregame/gate.py:104-124`, `pregame/gate.py:605-621`). **Opinion:** Context policy can materially change advice preparation; automatic deployment on six held-out cases is aggressive even if rule/tool widening is human-gated.
- **Evidence:** Setup drops all application collections before rebuilding them (`pregame/loop.py:40-60`; `pregame/db.py:145-148`). **Opinion:** This is fine for a scripted demo, but risky if someone runs setup against the wrong Atlas database minutes before judging.

## What is solid

- The configuration fence is substantive: head movement is conditional, the version/ledger/proposal transition runs through one transaction, and stale versions are rejected (`pregame/versions.py:257-352`). Rollback creates a new version instead of rewriting history (`pregame/versions.py:355-369`).
- The improver really receives a copied plain-data snapshot, not a database handle, and held-out rows/scenarios are never copied into it (`pregame/improver.py:66-117`, `pregame/improver.py:120-139`). That is a clean, explainable prompt-level capability boundary.
- The oracle reader sees only brief text and questions, while code evaluates keyed terms and forbidden stale values (`pregame/oracle.py:102-138`, `pregame/oracle.py:313-414`). The improver is not grading its own proposal.
- Client-note ownership is enforced before supersession, avoiding cross-household leakage (`pregame/compiler.py:21-33`). The dedicated tests cover shared subjects and ownerless notes (`tests/test_account_isolation.py:35-47`).
- Production brief storage fails closed on enabled guardrails and records a refusal (`pregame/loop.py:102-127`, `pregame/loop.py:150-160`). The latest independent check confirms the prior advice-pattern and blocked-demo defects were fixed (`checks/CHECK_HDY-37_6f68300.md:14-18`).
- Scenario storage refuses changed content under the same ID (`pregame/world/store.py:79-93`), and scenario construction includes stale-value, unaffected, rumor, and impossible cases (`pregame/world/scenarios.py:458-566`).
- Receipts bind every brief to config versions, a config hash, fact IDs, as-of time, and context size (`pregame/compiler.py:55-75`). This is excellent demo material because it makes the change observable and reversible.
- The ledger canonicalizes payloads and BSON time precision, validates sequence order and hashes, and detects edits (`pregame/ledger.py:34-65`, `pregame/ledger.py:124-157`). It is an honest audit mechanism when paired with appropriate database permissions.
- MongoDB is not decorative: it holds facts/events/clock, frozen scenarios, cached evaluations, proposals, version heads/history, briefs/feedback, and the ledger (`pregame/db.py:31-44`); the decisive configuration transition is an Atlas transaction (`pregame/db.py:127-142`, `pregame/versions.py:350-352`).
- The prior Codex reports show meaningful iteration: they found and drove fixes for account isolation, frozen scenario writes, stale commit cleanup, improver handle leakage, premature ledger success, and live guardrails (for example `checks/CHECK_HDY-30_587d6c7.md:1-10`, `checks/CHECK_HDY-32_6527cae.md:14-20`, `checks/CHECK_HDY-37_6f68300.md:14-18`).

## Recommended plan for the next 3 hours, in order

1. **0:00–0:35 — Fix the deployment gate.** Add uncited/guardrail counts, require zero/no-regression, require `pass_k` not to fall, and add focused tests. If that changes the fake winner, select a candidate that genuinely passes; do not weaken the checks.
2. **0:35–0:55 — Make claims exact.** Correct “36 held-out” to 18 overall/6 per field, “~50 calls” to 73 cold calls, and replace “proves improvement” with “wins a small simulated holdout.” State that isolation is in-process.
3. **0:55–1:20 — Version and clear the eval cache.** Add an evaluator-version component, clear `eval_runs`, and prewarm champion tuning/held-out rows using the exact models and prompts intended for the demo.
4. **1:20–2:00 — Produce the stage artifact.** Run one live record pass of the exact three-minute sequence; verify the cassette by replaying from a clean demo database. Save the resulting proposal IDs, expected decisions, scores, and model usage.
5. **2:00–2:20 — Verify Atlas, not mongomock.** From the stage laptop/network, run the transaction smoke test, validator/index setup, ledger verification, and one rollback. Confirm the database name before any reset because setup is destructive.
6. **2:20–2:45 — Rehearse the narrow story.** Event → missed brief → one rejected broad proposal → one accepted narrow proposal → receipt/ledger. Use replay; keep live mode only as a backup talking point. Target 2:30 to leave recovery time.
7. **2:45–3:00 — Freeze the repository and prepare fallbacks.** Keep a known-good database snapshot/cassette, hotspot, terminal commands, and screenshots/text outputs. Do not add vector search, change streams, provider routing, more domains, or a new UI.

**Do not spend time before 4 PM** implementing database roles, transactional event ingestion, statistical significance machinery, more seeds, a second safety classifier, or architectural refactors. Document those as next steps. The one exception is the gate safety omission in finding 1, because it can make the core self-improvement claim false even in the demo.
