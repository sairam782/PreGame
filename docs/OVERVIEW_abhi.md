> Written by Abhishek (sairam782) at 12:54 on 26 Sep as the first README of this repository; kept as he wrote it.
> The current overview is [README.md](../README.md); the design is [DESIGN.md](../DESIGN.md).

# Pregame


## What this is

Pregame drafts prep briefs for client meetings and improves its own configuration over time. Two loops share the same state:

- **The brief path** runs on every client review: it drafts a brief, checks it against guardrails, and gets scored by a simulated client review.
- **The improvement path** runs when feedback piles up: an LLM proposes a single configuration change, a gate scores it against a frozen, held-out benchmark, and — if it wins — a fence commits the new configuration version.

The system is built so the model that proposes improvements (the "improver") can never see the questions it will be graded on, never touches the database directly, and can never commit a change on its own authority.

## Entry points

| Entry point | Module |
|---|---|
| Command line | `cli.py` |
| Live page | `web/app.py` |
| Scripted demo | `loop.run_demo` |

All three call into the orchestrator, `loop.py`, which exposes `setup`, `make_brief`, `market_event`, `improve`, `status`, and `run_demo`.

## The simulated world

- **`world/store.py`** — insert-only facts (`facts_until`, newest-wins at read time) and an event feed that fires each event exactly once (atomic claim); the clock never runs backward.
- **`world/fields.py`** — the scenario content: 6 households, 18 events, 38 facts, with client notes tagged to an owner.
- **`world/scenarios.py`** (**frozen**) — the question bank: 36 meetings with 5–7 questions each, keyed off facts. Months 1–3 are the tuning split; months 4–6 are held out. Loaded once, insert-only; a changed reload is refused.

## The brief path

Runs once per client review:

1. **`compiler.py`** pulls the current facts for the household under the current configuration and writes a receipt.
2. **`drafter.py`** — Claude Sonnet 5 writes the brief; one retry if it fails validation.
3. **Guardrails** check for citations, staleness, and advice-giving; a failing brief is redrafted once, then blocked rather than stored.
4. **Simulated client review** — Claude Haiku 4.5 answers questions using only the brief. Anything the brief missed becomes a feedback record tied to the triggering event.

Only guardrail-passing briefs are stored, each with a receipt of the exact config versions and fact ids used.

## The improvement path

Runs when feedback has accumulated for a field (e.g. `loop.improve("retirement")`):

1. **Tuning baseline** — the gate scores the current champion configuration on the months 1–3 tuning questions (cached by config hash).
2. **`build_snapshot`** (trusted code) reads only permitted rows out of MongoDB, copies them to plain data, and drops the database handle before anything reaches the improver.
3. **`improver.py`** — Claude Opus 5.5 proposes exactly one small diff: body, rationale, evidence. It runs once per cycle and is the only place Opus is called.
4. **`gate.py`** files the proposal (idempotent on `idem_key`, logged to the ledger), then classifies its tier, validates it (bounds, rule cap), runs it through the tuning screen (k=1), scores it against the champion on held-out questions (k=2, champion cached by config hash), and compares the scores.
5. A **tier G** win commits immediately. A **tier H** win waits for the owner to approve by hash. A **tier X** proposal — one that targets a frozen surface — is refused before scoring.
6. **The fence** (`versions.commit`) writes the new config version, moves the head (only if it's still on the base version), and writes a ledger receipt — all in one MongoDB transaction.
7. Any blocked read the improver attempted while building its snapshot is written to the ledger as `refused`.

**Cost note:** each proposal costs roughly 50 model calls on the live model, split across the tuning screen and held-out scoring; the champion's scores are cached by configuration hash so they're paid once.

## The trust boundary

The improver is a model with no tools — it only ever sees text that trusted code chose to show it.

**In the improver's snapshot:** post-review feedback in the advisor's words; recent live briefs (never the gate's evaluation briefs); the current configuration and its version history; tuning results for months 1–3 with each failed question and why; its own past proposals and decisions, with held-out numbers masked and hashes removed.

**Never reachable:** held-out questions and answer keys (months 4–6); held-out scores beyond "it lost"; the oracle, metrics, and the gate's code; any database, client, collection, or cursor object; the power to commit — it can only propose.

**If it asks anyway:** the view raises `PermissionError` and queues the attempt; trusted code writes each queued attempt to the ledger as `refused`; a proposal targeting a frozen surface is tier X and refused before scoring; tests include an object-graph walk confirming no database object is reachable, and a planted marker that never reaches the improver's prompt.

## Model layer (`llm.py`)

| Role | Model |
|---|---|
| Drafter | Claude Sonnet 5 |
| Reader (simulated review) | Claude Haiku 4.5 |
| Improver | Claude Opus 5.5 |

Providers: `claude-cli` (subscription, current), `anthropic` (API credits), `openrouter` (planned). A cassette mode records live calls for offline replay; a stand-in ("fake") mode gives every module a deterministic path for testing without live calls.

## Data model

MongoDB Atlas · Cluster0 (M10, 8.0.32) · database `pregame_alex`

| Collection | Holds | Written by | Guarantee |
|---|---|---|---|
| `facts` | Dated market and client facts; client notes carry an owner id | `world/store.py` | Insert-only; newest per subject/relation wins at read time |
| `events`, `clock` | Scripted event feed and simulated time | `world/store.py` | Each event fires once (atomic claim); clock never goes back |
| `config_versions` | Every version of policy, rules, tools, guardrails | `versions.commit` | Insert-only; `_id = kind:key@vN`; strict validator |
| `config_heads` | Current version number per setting | `versions.commit` | Moves only if still at the version the gate checked |
| `briefs` | Briefs with receipts (versions, fact ids, config hash) | `loop.py` | Only guardrail-passing briefs are stored |
| `feedback` | What a simulated review found missing | `loop.market_event` | Tied to the event and brief that caused it |
| `proposals` | Every proposed change, its tier, decision, and scores | `gate.py` | Unique `idem_key`; status moves are conditional; strict validator |
| `eval_scenarios` | The question bank, tuning and held-out | `world/store.py` | Insert-only; a changed reload is refused (frozen) |
| `eval_runs` | Cached scores per configuration hash and split | `gate.py` | Unique per (config hash, run key, k); held-out rows keep no per-question detail |
| `ledger` | Receipts for every event, brief, proposal, commit, refusal | `ledger.append` | Hash-chained; `_id == seq`; `verify()` detects any edit |

Strict `$jsonSchema` validators sit on `proposals`, `config_versions`, and `ledger`; unique indexes carry the invariants elsewhere.

## Roadmap — numbered hooks

Effort estimates assume one person working with agents.

| # | Hook | What changes | Why | Effort | Open question |
|---|---|---|---|---|---|
| 1 | Model layer, `llm.py` providers | Add OpenRouter; route each role to the best-value model | The reader could be cheap, the improver strong | Small | Do the gate's decisions hold when the reader's model changes? |
| 2 | Gate, step 4 | Add a best-of-N control: the champion resampled at the same token budget | One study found plain resampling beat evolved harnesses | Medium | How many samples make a fair budget, and does it double gate-run cost? |
| 3 | `config_versions` keys | Per-household policies (`policy:<household>`) inheriting from the segment | The brief is meant to fit a specific user | Medium | With so few meetings per household, how do we avoid tuning to noise? |
| 4 | Orchestrator, `loop.py` | A change stream on `facts`/`feedback` wakes the improvement path | Today a person or the demo calls `improve` manually | Small | How much feedback should accumulate before a cycle is worth ~50 model calls? |
| 5 | Snapshot ↔ MongoDB | A separate, read-only database user for the snapshot builder | MongoDB itself should refuse the reach, not just our code | Small | Needs the project owner to create a custom role |
| 6 | `eval_scenarios` and beyond | More seeds and meetings; probation that auto-reverts by id on a live-metric breach | 36 meetings only detect large effects | Medium | Which live metric is trustworthy enough to trigger an automatic revert? |
| 7 | Compiler | Atlas Vector Search with pre-filters (household, validity) for client notes | Needed once a household has hundreds of notes | Medium | Embedding cost and rate limits; exact search suffices below ~10,000 records |
| 8 | Oracle / the reader | Check the reader against ~30 human-labelled answers | The reader is part of the yardstick; reliable ≠ correct | Small | Who labels, and what agreement threshold before trusting the scores? |
| 9 | Guardrails | Add a classifier as a second no-advice check, alongside the patterns | Patterns can't list every phrasing of advice | Medium | "Advice" is a compliance definition someone qualified should own |
| 10 | The fence, human tier | Show the owner a rendered diff card and require a reason with each signature | Approval by hash is safe but hard to actually read | Small | Should rule changes also expire unless re-approved? |

## Provenance

Drawn from the code at commit `22456ba` in `C:\Projects\prep-harness`. Every module named above exists with that name in the codebase; see `DESIGN.md` and `INTERFACES.md` for the full contracts.