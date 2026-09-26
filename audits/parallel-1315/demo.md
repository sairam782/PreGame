# Pregame — demo/operator-experience audit

## 3-line verdict
1. The scripted `demo` command's numbers and decision text are genuinely readable ("Rejected: held-out accuracy 0.65 -> 0.85, but false alarms rose... context grew 240 -> 437 tokens (+82%..." — verified live via mongomock run) — the gate's *reasoning* is the strongest on-stage asset.
2. But the operator has almost no runtime feedback: no mode banner (fake vs. live), no progress output during the ~70-call live evaluation (gate.py/oracle.py have zero print statements), and the web page fails **silently** (frozen "loading…", no error) on any backend hiccup.
3. In fake mode the demo's most impressive beat — a proposal winning and landing `awaiting_owner` for human approval — is mathematically unreachable (verified: tier-H proposal always rejects, tied 0.97 vs 0.97), so the human-in-the-loop approval command that both the CLI and web UI print is never actually exercised unless running live.

## Findings, ranked

### 1. [Critical] No mode/progress feedback; live mode is the silent default whenever an API key is present
- `pregame/config.py:81` — `default_mode = "live" if (api_key or provider == "claude-cli") else "fake"`; only an explicit `PREGAME_LLM_MODE=fake` env var overrides this.
- `pregame/gate.py` and `pregame/oracle.py`: zero `print`/progress statements (grepped, no matches) — a live `improve`/`demo` run (≈70+ model calls per the earlier audit's finding #5) leaves the terminal completely silent between "-- improve #1 --" and the result line.
- Neither `pregame/cli.py` nor `pregame/loop.py:run_demo` (`loop.py:360-447`) ever prints which mode is active. The only mode-related text in the whole run is the one conditional caveat at `loop.py:437-442`, which only fires when fake mode loses the tier-H proposal.
- **Why it matters**: presenter can be one missing env var away from a real, slow, costly, possibly-rate-limited live run, with a frozen terminal and zero indication to judges of what's happening or whether the numbers on screen are fake-deterministic or real-model output.
- **Smallest fix**: print `mode=<fake|live|record>` as the first line of `cmd_demo`/`cmd_improve`/`run_demo`'s header, and add one heartbeat print inside the evaluation loop (e.g. before/after `gate.tuning_baseline` and each `evaluate_proposal` call in `loop.py:250-261`). ~10-15 min.

### 2. [High] The web page fails silently on any backend error — freezes on "loading…" forever
- `pregame/loop.py:277` — `ok, checked, problem = ledger.verify(db)` is the **only** call in `status()` not wrapped in try/except (every other block — sim_time, per-field config/history, guardrails, events, proposals, briefs, feedback, ledger_tail, llm_usage — is defensively wrapped, lines 272-333).
- `pregame/web/app.py:44-46` `api_state` has no exception handler, so any exception there (e.g. a transient Atlas hiccup during `ledger.verify`) becomes a bare 500.
- `pregame/web/static/index.html:196-204` — `poll()`: `if (!res.ok) return;` and an empty catch block ("network hiccup: keep showing the last good state") — there is no visible error, no stale-data indicator, nothing. If this happens before the first successful poll, the page is stuck on the literal string "loading…" (`index.html:141`) with no way for the presenter to know why, short of alt-tabbing to the server terminal.
- **Smallest fix**: wrap the `ledger.verify(db)` call the same way its neighbors are wrapped (1 line); add a visible "last updated Ns ago" / red banner in the `poll()` failure paths (~10 lines JS). ~15 min total.

### 3. [High] `setup` is a silent, unconfirmed full-database wipe, with no printed target
- `pregame/cli.py:71-77` `cmd_setup` calls `loop.setup` → `db.reset_db` (`pregame/db.py:145-148`, drops all 11 collections) with no print of the resolved Mongo URI/db name and no confirmation.
- Resolution is env/settings-driven (`pregame/db.py:69-94`: `pregame.config.settings()` first, else `MONGODB_URI` env var, else `localhost:27017`/`pregame`). A second terminal opened on stage without the same environment loaded would silently reset whatever `pregame` database *that* shell resolves to.
- Given this session's own guardrail — "a live run is using [the real database]" — this is exactly the failure mode to avoid: an innocent second `setup` from an unprepped shell.
- **Smallest fix**: print the resolved host+db name at the top of `cmd_setup` before calling `reset_db` (`print(f"target: {uri} / db={db_name}")`, needs a tiny helper exposed from `db.py`). ~10 min, non-interactive (keeps the script safe to run unattended on stage).

### 4. [Medium-high] Fake-mode demo can never reach the `awaiting_owner`/approve beat
- Verified by running the provided mongomock script: "improve #3" (tier H, rule change) is always `status=rejected` ("held-out accuracy 0.97 vs champion 0.97 is inside the 0.05 margin"), because the fake drafter doesn't read drafting rules at all (admitted in-line at `loop.py:437-442`).
- Both `cli.py:138-139` and `index.html:268-270` render a ready-to-paste `approve` command only for the `awaiting_owner` path — the single most "wow" beat (a human signing off on an autonomous change) has no fake-mode rehearsal path.
- **Smallest fix**: not a code fix under time pressure — either (a) pre-record one live run that reaches `awaiting_owner` and show that transcript/screenshot as a fallback for this one beat, or (b) narrate it ("here's the command it would print") without executing. ~15-20 min to capture a live recording as fallback.

### 5. [Medium] CLI `demo` never prints brief text — "what changed" is invisible without the web UI
- `run_demo` (`loop.py:373-421`) only ever prints `brief {id} versions={...}`, never brief markdown.
- The only place a judge sees the actual before/after sentence-level diff is the web page's Brief-compare panel (`index.html:320-354`), which nicely highlights new claims in green (`claim.new` / `--chip-g`).
- **Smallest fix**: none needed in code if the web page is kept open and driven alongside the CLI (run-order fix, see below). If a CLI-only fallback is wanted, add a 3-line print of new claim texts after brief2 in `run_demo`. ~10 min.

## Proposed 3-minute run order (web page open in a browser tab the whole time, terminal beside it)

**Pre-stage (before the 3-minute clock, not part of the timed demo):**
`export PREGAME_LLM_MODE=fake` (or whatever was rehearsed) → `python -m pregame.cli setup` → confirm printed counts → `python scripts/serve.py` in a background terminal → open `http://127.0.0.1:8000`, confirm the ledger badge shows green and the loop-strip shows the seed step. Do **not** re-run `setup` after this point.

- **0:00–0:20** — One line over the loaded page: "MongoDB Atlas holds every fact, brief, proposal and this hash-chained ledger, live." Point at the ledger badge and loop-strip.
- **0:20–0:50** — `python -m pregame.cli fire ret-rmd-age` — audience sees `call_accuracy=0.80 missed=1 feedback_filed=yes` printed; point at the web page's loop-strip and Brief panel updating to show the missed change.
- **0:50–1:30** — `python -m pregame.cli improve retirement` (run #1, the over-broad rejection) then immediately again (run #2, the win) — point at the printed decision text (`Rejected: ... false alarms rose...` then `Committed automatically (tier G): held-out accuracy 0.65 -> 0.97...`). This is the strongest, clearest text in the whole system — let it read on screen a beat before moving on.
- **1:30–2:00** — Switch to the browser: point at Versions panel (policy v1 → v2) and the Brief-compare panel's green-highlighted new claim — this is the actual "what changed" proof, not just a version number.
- **2:00–2:30** — `python -m pregame.cli tamper retirement` — fast, always-succeeds, proves the improver-can't-touch-its-own-yardstick isolation story ("Refused: scenarios is frozen...").
- **2:30–3:00** — Close on the ledger badge/hash chain already on screen as the "everything here is provably real and tamper-evident" line. Skip the tier-H/`awaiting_owner` beat live (finding #4) — mention it verbally or show a pre-recorded screenshot instead.

## Smallest code/output changes, ranked by value/minute
1. Wrap `ledger.verify(db)` in try/except in `loop.status` (finding #2) — ~5 min, removes the single biggest "silent freeze" risk.
2. Print resolved Mongo URI/db name at the top of `cmd_setup` before `reset_db` (finding #3) — ~10 min, removes the "wrong database" risk.
3. Print `mode=<fake|live|...>` at the top of `demo`/`improve` output (finding #1) — ~10 min, removes audience confusion about what's real.
4. Add a visible stale/error indicator to `index.html`'s `poll()` failure paths (finding #2) — ~10 min.
5. Capture one pre-recorded live run reaching `awaiting_owner` as a fallback screenshot/transcript for the beat fake mode can't reach (finding #4) — ~15-20 min (not a code change, a rehearsal asset).
