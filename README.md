# Pregame

A prep bot for financial advisors whose harness improves itself, safely. Built on 26 September 2026 at the
MongoDB × Cerebral Valley *Harness Engineering & Model Wrangling* hackathon (problem statement 1, **Recursive
Harnessing**). All code in this repository was written on the day.

An advisor meets the same households for years. Before each review, Pregame writes the advisor a one-page brief:
what changed in the client's life and in the world (rates, markets, tax rules), why it matters to this household,
the questions the client is likely to ask, and talking points. Every claim cites the facts it rests on. The brief
prepares the advisor; it never advises the client.

When briefs start missing things, the harness changes **itself**: its context policy, drafting rules, tool access
and guardrails. It proposes one small change at a time; a frozen oracle scores the change on held-out questions the
improver has never seen, graded by code; changes that win commit automatically, changes that loosen anything wait
for a person to sign, and anything that touches the yardstick is refused. Every change is one MongoDB transaction
with a hash-chained ledger receipt.

- **How it works:** [DESIGN.md](DESIGN.md) · module contracts: [INTERFACES.md](INTERFACES.md), [pregame/contracts.py](pregame/contracts.py)
- **Independent checks:** every module was checked by Codex (a different model family) before its card closed;
  the reports are in [checks/](checks/). Independent audits of the whole system are in [audits/](audits/).
- **See it:** a static snapshot of the live page is at [docs/index.html](docs/index.html) (open it directly, no
  server needed). GitHub Pages: https://sairam782.github.io/PreGame/ once Pages is enabled on main /docs.

## Results (26 Sep, real models)

**The self-improvement loop, live.** Three simultaneous runs on Claude Sonnet 5 (writer), Haiku 4.5 (reader) and Opus
5.5 (proposer), retirement segment ([docs/LIVE_RUNS.md](docs/LIVE_RUNS.md)): the broad change was rejected 3/3; a
narrow change was adopted in the runs that stand after a guardrail correction (0.72 → 0.94 and 0.74 → 0.94 on unseen
test meetings); tampering with the test questions was refused 3/3; every run's audit log verified (21 entries). The
unchanged version scored 0.72–0.74 across runs, so run-to-run noise is about 0.02. The stage demo replays run C.

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

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/set_env.py               # writes .env from values you type (git-ignored)
.venv/Scripts/python scripts/smoke_atlas.py           # version, one transaction, one change-stream event
.venv/Scripts/python -m pregame.cli demo              # the scripted self-improvement run
.venv/Scripts/python scripts/serve.py                 # the live page on http://127.0.0.1:8000
.venv/Scripts/python -m pytest -q                     # no network, no keys
```

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

## MongoDB

Atlas (tested on an M10 cluster, MongoDB 8.0.32). Collections: `facts`, `events`, `clock`, `config_versions`,
`config_heads`, `briefs`, `feedback`, `proposals`, `eval_scenarios`, `eval_runs`, `ledger`. Strict `$jsonSchema`
validators on `proposals`, `config_versions` and `ledger`; unique indexes as invariants; a version-conditional commit in
one transaction (the fence); and a hash-chained ledger whose check catches an entry that was changed, reordered or
dropped from the middle of the chain (limits below). Insert-only facts, versions and scenarios are
enforced by the application, not yet by database permissions. Connect with `ServerApi("1")`, never `strict=True`.

## Honest limits

The markets, households and questions are simulated and fictional. Simulated clients are easier than real ones.
A proposal is judged on 6 held-out meetings in its field (18 across the three fields), which detects only large
effects: treat a win as "won a small simulated test", not proof. The worst meeting is reported beside the mean. Isolation of the
improver is in-process (it receives a plain-data snapshot with no database handle); a separate database user with
narrower rights would make MongoDB itself enforce it.

The ledger check covers the log itself. Receipts name facts, briefs and settings versions by id, not by content, so an
edit made directly to a stored brief or settings version still passes `ledger --verify`. The hash has no secret key, so
someone with write access to the database could rebuild the whole chain, and deleting the newest entries goes unnoticed.
Content hashes in the receipts, plus a copy of the latest hash kept outside the database, would close these gaps.
