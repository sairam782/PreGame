# Pregame: a prep bot whose harness improves itself

Built on 26 Sep 2026 at the MongoDB Harness Engineering & Model Wrangling hackathon, for problem statement one,
**Recursive Harnessing**. All code in this repo was written on the day.

- **See it** (three ways, all showing the stage replay of a live run on real models):
  - **Snapshot, no setup:** [docs/index.html](docs/index.html), the live page with its data built in (the
    self-improvement loop and the banker-book before/after). Download and open it; GitHub Pages will serve it at
    https://sairam782.github.io/PreGame/ once Pages is enabled on `main` `/docs`.
  - **Live page:** `scripts/serve.py` (below) reads MongoDB Atlas and refreshes as a run happens.
  - **Viewer:** [viewer/](viewer/README.md), a read-only page with a database switcher (stage replay, the three live
    runs, the banker book) that also runs offline from its public fixtures.
- **Results:** [below](#results-26-sep-real-models) and [docs/LIVE_RUNS.md](docs/LIVE_RUNS.md).
- **How it works:** [DESIGN.md](DESIGN.md) · module contracts: [INTERFACES.md](INTERFACES.md), [pregame/contracts.py](pregame/contracts.py)
- **Independent checks:** every module was checked by Codex (a different model family) before its card closed;
  the reports are in [checks/](checks/). Whole-system audits and GPT-5.6 Sol reviews (including two that sent our
  guardrail fix back) are in [audits/](audits/).

## What it does

A **financial advisor** meets the same client households again and again: **retirement** (pre-retirees and
retirees), **families** (working households saving for children and a home) and **business owners** (founders and
owners planning liquidity, succession and taxes). Before each review Pregame writes the advisor a prep brief: what
changed in the world and in the client's own life, what it means for this household, the questions the client is
likely to ask, and talking points. Every claim cites the facts it rests on. The brief prepares the advisor; it never
gives the client investment advice.

Two things move, so the advisor has to keep up with both. The **world** moves: rates, markets, tax rules and products
(a new RMD age, a sell-off, a bank failure, a cut to the estate tax exemption). The **clients** move: a new grandchild,
an inheritance, a business sale closing, a retirement date pushed back, and preferences that change ("not now on the
annuity" becomes "ready to talk after the sale closes"). A brief that repeats a client's old answer is as wrong as one
that quotes last quarter's rate. When either kind of change lands, briefs built with the old settings start missing
what matters. Pregame's harness then **changes itself**: its context policy, its drafting rules, its tool access and
its guardrails. It does this the way the research says works, not by rewriting its own prompt in a loop:

1. **Signal.** An event changes the facts. After each simulated client review, the advisor's feedback says what the
   brief missed ("she asked about the new 401(k) limit and I didn't have it").
2. **Propose.** An improver model reads the feedback and the *tuning* results only, and files one small, versioned
   change as a proposal.
3. **Measure.** A frozen oracle scores the candidate against the current version on a **held-out** question set the
   improver has never seen: a reader model answers the client's questions using only the brief, and **code** checks
   the answers against keys computed from the world and client state. No model grades its own work.
4. **Gate.** Code classifies the change by what it touches (its tier). A policy change that wins on held-out data
   commits automatically. A rule change, a tool that is switched on, or a guardrail that is added, loosened or
   reworded waits for a person to approve it by hash. Anything that touches the oracle, the questions or the ledger
   is refused.
5. **Commit.** One MongoDB transaction moves the version head only if it is still at the version the gate checked,
   and only if the other three surfaces are still at the versions the candidate was evaluated with, inserts the new
   version and appends a hash-chained ledger receipt.
6. **Trace and roll back.** Every brief carries a receipt naming the exact versions and fact ids it used. Rollback is
   a new version that carries an old one's body, chosen by id.

## Results (26 Sep, real models)

**The self-improvement loop, live.** Three simultaneous runs on Claude Sonnet 5 (writer), Haiku 4.5 (reader) and Opus
5.5 (proposer), retirement segment ([docs/LIVE_RUNS.md](docs/LIVE_RUNS.md)): the broad change was rejected 3/3; a
narrow change was adopted in the runs that stand after a guardrail correction (0.72 → 0.94 and 0.74 → 0.94 on unseen
test meetings); tampering with the test questions was refused 3/3; every run's audit log verified (21 entries). The
unchanged version scored 0.72–0.74 across runs, so run-to-run noise is about 0.02. The stage demo replays run C.

**A correction we made on the day.** In those runs every "guardrail violation" turned out to be a false positive of our
text checks on compliant prose ("Do not present it as a guaranteed outcome"), which made the gate reject real
improvements in one run. An audit caught it; the checks were fixed, twice sent back by a Sol review for being too
loose, and the 26 real sentences plus 26 adversarial promises are now tests. Run C was re-graded under the fixed
checks by replay and its decisions stand.

**A data teammate's banker book** (6 synthetic clients, 24 call preps written by an assistant with no harness, scored
against his answer key by a separate program the harness never imports; `python -m pregame.cli cabinet`):

| | No harness | Same rules switched off | Pregame's harness |
|---|---|---|---|
| Preps with at least one mistake | 17/24 (71%) | 16/24 | **0/24** |
| Mistakes | 33 | 32 | **0** |
| Promise the rules forbid ("built to protect your capital") | 1 | 1 | **0** |
| Actions the file called for (ask, flag, brief both holders) | 0/10 | 0/10 | **10/10** |

The fixes were written from the answer key's list of mistakes and scored on the same 24 preps, so this shows the
harness applies them from the visible data alone; it is not a test of generalisation. The 20-client version of the
data is the real test.

## Tiers: what the harness may change about itself

| Surface | Example | Tier | How it changes |
|---|---|---|---|
| Context policy (per field) | recency window, max facts, fact kinds, section order, number of likely questions | **G** eval-gated | auto-commits on a held-out win |
| Tools (per field) | switch the `analyst_notes` source off | **G** | tightening auto-commits on a held-out win |
| Tools (per field) | switch `analyst_notes` on | **H** human-gated | held-out win **and** owner approval |
| Drafting rules (per field) | "lead with the change that moves the client's budget" | **H** | held-out win **and** owner approval; replace a rule, never append past the cap |
| Guardrails (global) | enable an existing, unchanged check | **G** | code checks, then commits |
| Guardrails (global) | add, remove, disable or reword a check (its text goes into the drafter's prompt) | **H** | owner approval |
| Oracle, question bank, held-out split, ledger, gate code, metric definitions | | **X** frozen | refused, always |

## Architecture

```
world + client events ──► facts (insert-only)             eval_scenarios (frozen; tuning | heldout)
                        │                                           │
  config versions ──► compiler ──► context + receipt ──► drafter ──► brief (claims cite fact ids)
  (policy/rules/tools/guardrails, insert-only + heads)                 │
        ▲                                                            oracle (frozen): reader model answers
        │                                                            questions from the brief; code checks
      fence (txn) ◄── gate (tier, held-out vs champion) ◄── improver ◄── feedback + tuning aggregates
        │
      ledger (hash-chained)            live page / viewer: steps · proposals · versions · brief before/after ·
                                       banker book before/after · audit log

  teammate's banker book (Atlas `cabinet`, visible data only) ──► pregame/cabinet.py adapter + harness policy
      ──► 24 preps with structured claims ──► score_preps.py (separate process; the only reader of the answer key)
```

The core is **pure functions** (compiler, drafter, oracle, gate decision) over plain dicts, so it tests without a
database or an API key. MongoDB holds all state. A fake LLM makes every path runnable offline; a cassette records live
LLM calls so the stage demo can replay them if the network fails.

## MongoDB

Database `pregame`. Collections: `facts`, `events`, `clock`, `config_versions`, `config_heads`, `briefs`, `feedback`,
`proposals`, `eval_scenarios`, `eval_runs`, `ledger`. Strict `$jsonSchema` validators on `proposals`,
`config_versions` and `ledger`. Unique indexes carry the invariants: `config_versions._id = "<kind>:<key>@v<n>"`,
one head per `<kind>:<key>`, `ledger.seq`, `proposals.idem_key`. The fence is one transaction (head update filtered
on the expected version, version insert, ledger append, proposal status). Connection uses `ServerApi("1")`, never
`strict=True` (strict refuses `$search`). The project must run on the hackathon's Atlas Sandbox. Insert-only facts,
versions and scenarios are enforced by the application, not yet by database permissions. The cabinet command adds
`cabinet_preps` and `cabinet_runs` (it writes only into `pregame*` databases) and reads the teammate's visible data
from the `cabinet` database, never `cabinet_truth`. The stage database is `pregame_demo`, filled by replaying run C.
Opt-in Atlas tests (`PREGAME_ATLAS_TESTS=1`, tests/test_atlas.py) prove on the real cluster that a failure inside the
fence rolls everything back, that two racing commits leave exactly one version, and that validators and unique indexes
reject bad writes; they also caught a ledger time-zone bug the in-memory test database hid.

## Code layout

| Path | Owner | What |
|---|---|---|
| `pregame/contracts.py` | shared | the data shapes and constants every module uses |
| `pregame/config.py`, `pregame/llm.py` | llm | env settings; LLM interface with `live`, `record`, `replay` and `fake` modes; `anthropic` or `claude-cli` provider |
| `pregame/db.py`, `pregame/ledger.py`, `pregame/versions.py` | db | connection, validators, indexes, ledger, versioned config, fence, rollback, seed |
| `pregame/world/` | world | the three fields, scripted events, fact store, scenario and question generator |
| `pregame/compiler.py`, `pregame/drafter.py` | brief | context compiler with receipt; brief drafter and markdown rendering |
| `pregame/oracle.py`, `pregame/metrics.py` | oracle | frozen grader and metrics (pass^k, missed changes, false alarms, drift) |
| `pregame/improver.py`, `pregame/gate.py` | gate | proposal writer; tier classification, evaluation, decision, approval, commit |
| `pregame/loop.py`, `pregame/cli.py`, `pregame/web/` | app | orchestration, command line, live page |
| `pregame/cabinet.py` | cabinet | the data teammate's banker book: adapter, harness policy, preps with claims, scoring by subprocess |
| `scripts/` | tools | `set_env.py`, `smoke_atlas.py`, `serve.py`, `export_page.py` (writes the snapshot page) |
| `viewer/` | viewer | the read-only viewer (its own README) |
| `cassettes/` | runs | recordings of the live runs; `demo.jsonl` is run C, the stage replay |
| `docs/` | docs | the snapshot page, live-run results, Abhishek's first overview |
| `tests/` | each owner | pytest; mongomock + fake LLM, no network (Atlas tests are opt-in) |

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/set_env.py               # writes .env from values you type (git-ignored)
.venv/Scripts/python scripts/smoke_atlas.py           # version, one transaction, one change-stream event
.venv/Scripts/python -m pregame.cli setup             # reset + seed a pregame* database (refuses other names)
.venv/Scripts/python -m pregame.cli demo              # the scripted self-improvement run
.venv/Scripts/python -m pregame.cli cabinet           # the data teammate's book: before/after
.venv/Scripts/python scripts/serve.py                 # the live page on http://127.0.0.1:8000
.venv/Scripts/python scripts/export_page.py --db pregame_demo --out docs/index.html   # refresh the snapshot page
.venv/Scripts/python -m pytest -q                     # no network, no keys
```

**The stage demo** replays run C's recording into a clean database in about a minute, with no model calls:

```bash
PREGAME_LLM_MODE=replay PREGAME_DB=pregame_demo .venv/Scripts/python -m pregame.cli demo
```

(PowerShell: `$env:PREGAME_LLM_MODE="replay"; $env:PREGAME_DB="pregame_demo"; .venv\Scripts\python -m pregame.cli demo`.)
`demo` resets its database first and refuses one whose name doesn't start with `pregame` unless you add `--yes`.
Every command prints a banner with the mode, provider, models and database (host only, never credentials). If Atlas
is unreachable the live page says so within seconds (`PREGAME_MONGO_TIMEOUT_MS`, default 8000) instead of looking empty.

With no model configured everything runs on a deterministic stand-in model. With a live model, briefs are written by
Claude Sonnet 5, a Claude Haiku 4.5 reader plays the advisor answering the client's questions from the brief, and
Claude Opus 5.5 proposes improvements. Two ways to reach the live models: `PREGAME_PROVIDER=claude-cli` runs every
call through your own Claude subscription via headless `claude -p` (sign in once with `claude`; no API key), or set
`ANTHROPIC_API_KEY` for the default `anthropic` provider (API credits). OpenRouter is planned.
`PREGAME_LLM_MODE=record` records live calls to a cassette and `replay` serves them back without a network.
A cold improvement cycle is about 73 model calls (champion and candidate on the tuning and held-out meetings), fewer
once the champion's scores are cached.

Other commands: `python -m pregame.cli --help` (`fire`, `brief`, `improve`, `proposals`, `approve`, `reject`,
`rollback`, `trace`, `ledger --verify`, `tamper`).

## Honest limits

- The markets, accounts and client questions are **simulated** and labelled so; no real company is described.
  Simulated clients are easier than real ones.
- A proposal is judged on 6 held-out meetings in its field (18 across the three fields), which detects only large
  effects: treat a win as "won a small simulated test", not proof. The worst meeting is reported beside the mean.
- Isolation of the improver is in-process (it receives a plain-data snapshot with no database handle); a separate
  database user with narrower rights would make MongoDB itself enforce it.

The ledger check covers the log itself. Receipts name facts, briefs and settings versions by id, not by content, so an
edit made directly to a stored brief or settings version still passes `ledger --verify`. The hash has no secret key, so
someone with write access to the database could rebuild the whole chain, and deleting the newest entries goes unnoticed.
Content hashes in the receipts, plus a copy of the latest hash kept outside the database, would close these gaps.

The teammate's-book result is in-sample: the fixes were written from its answer key's list of mistakes and scored on
the same 24 preps, and today code (not the model) writes those preps' claims. A run where the model writes each prep,
with and without the harness, is being built. The guardrails are word patterns, a backstop; a model-based check is
next. What the pages show on stage is a replay of a live run recorded today, not a run happening in the room.
