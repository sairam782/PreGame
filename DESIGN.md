# Pregame: a prep bot whose harness improves itself

Built on 26 Sep 2026 at the MongoDB Harness Engineering & Model Wrangling hackathon, for problem statement one,
**Recursive Harnessing**. All code in this repo was written on the day.

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
   commits automatically. A rule change, a tool that is switched on, or a guardrail that is loosened waits for a
   person to approve it by hash. Anything that touches the oracle, the questions or the ledger is refused.
5. **Commit.** One MongoDB transaction moves the version head only if it is still at the version the gate checked,
   inserts the new version and appends a hash-chained ledger receipt.
6. **Trace and roll back.** Every brief carries a receipt naming the exact versions and fact ids it used. Rollback is
   a new version that carries an old one's body, chosen by id.

## Tiers: what the harness may change about itself

| Surface | Example | Tier | How it changes |
|---|---|---|---|
| Context policy (per field) | recency window, max facts, fact kinds, section order, number of likely questions | **G** eval-gated | auto-commits on a held-out win |
| Tools (per field) | switch the `analyst_notes` source off | **G** | tightening auto-commits on a held-out win |
| Tools (per field) | switch `analyst_notes` on | **H** human-gated | held-out win **and** owner approval |
| Drafting rules (per field) | "lead with the change that moves the client's budget" | **H** | held-out win **and** owner approval; replace a rule, never append past the cap |
| Guardrails (global) | add or tighten a check | **G** | code checks, then commits |
| Guardrails (global) | remove or loosen a check | **H** | owner approval |
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
      ledger (hash-chained)            live page: timeline · versions · proposals · brief v1 vs v2 · ledger
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
`strict=True` (strict refuses `$search`). The project must run on the hackathon's Atlas Sandbox.

## Code layout

| Path | Owner | What |
|---|---|---|
| `pregame/contracts.py` | shared | the data shapes and constants every module uses |
| `pregame/config.py`, `pregame/llm.py` | llm | env settings; LLM interface with `live`, `fake`, `cassette` modes |
| `pregame/db.py`, `pregame/ledger.py`, `pregame/versions.py` | db | connection, validators, indexes, ledger, versioned config, fence, rollback, seed |
| `pregame/world/` | world | the three fields, scripted events, fact store, scenario and question generator |
| `pregame/compiler.py`, `pregame/drafter.py` | brief | context compiler with receipt; brief drafter and markdown rendering |
| `pregame/oracle.py`, `pregame/metrics.py` | oracle | frozen grader and metrics (pass^k, missed changes, false alarms, drift) |
| `pregame/improver.py`, `pregame/gate.py` | gate | proposal writer; tier classification, evaluation, decision, approval, commit |
| `pregame/loop.py`, `pregame/cli.py`, `pregame/web/` | app | orchestration, command line, live page |
| `tests/` | each owner | pytest; mongomock + fake LLM, no network |

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
copy .env.example .env      # then fill in MONGODB_URI and ANTHROPIC_API_KEY yourself
.venv/Scripts/python -m pregame.cli setup
.venv/Scripts/python -m pregame.cli demo
```

## Honest limits

- The markets, accounts and client questions are **simulated** and labelled so; no real company is described.
- Simulated clients are easier than real ones.
- Held-out scores on a few dozen questions detect only large effects; we report the worst seed beside the mean.
