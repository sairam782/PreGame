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
  the 25 reports are in [checks/](checks/).

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # macOS/Linux: .venv/bin/pip
.venv/Scripts/python scripts/set_env.py               # writes .env from values you type (git-ignored)
.venv/Scripts/python scripts/smoke_atlas.py           # version, one transaction, one change-stream event
.venv/Scripts/python -m pregame.cli demo              # the scripted self-improvement run
.venv/Scripts/python scripts/serve.py                 # the live page on http://127.0.0.1:8000
.venv/Scripts/python -m pytest -q                     # 252 tests, no network, no keys
```

Without `ANTHROPIC_API_KEY` everything runs on a deterministic stand-in model. With it, briefs are written by
Claude Sonnet 5, a Claude Haiku 4.5 reader plays the advisor answering the client's questions from the brief, and
Claude Opus 5.5 proposes improvements. `PREGAME_LLM_MODE=record` records live calls to a cassette and `replay`
serves them back without a network.

Other commands: `python -m pregame.cli --help` (`fire`, `brief`, `improve`, `proposals`, `approve`, `reject`,
`rollback`, `trace`, `ledger --verify`, `tamper`).

## MongoDB

Atlas (tested on an M10 cluster, MongoDB 8.0.32). Collections: `facts`, `events`, `clock`, `config_versions`,
`config_heads`, `briefs`, `feedback`, `proposals`, `eval_scenarios`, `eval_runs`, `ledger`. Strict `$jsonSchema`
validators, unique indexes as invariants, insert-only versions, a version-conditional commit in one transaction
(the fence), and a hash-chained ledger. Connect with `ServerApi("1")`, never `strict=True`.

## Honest limits

The markets, households and questions are simulated and fictional. Simulated clients are easier than real ones.
36 held-out meetings detect only large effects, so the worst meeting is reported beside the mean. Isolation of the
improver is in-process (it receives a plain-data snapshot with no database handle); a separate database user with
narrower rights would make MongoDB itself enforce it.
