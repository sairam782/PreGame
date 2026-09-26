# Pregame viewer

A read-only local web page for the Pregame demo. Pregame is a self-improving call-prep harness for a financial
advisor; its state lives in MongoDB Atlas. The viewer shows what the harness's agents are doing (the hash-chained
ledger and the proposals, config versions and briefs around it), the findings, and the synthetic banker dataset
(the "cabinet").

**It never writes to the database.** Every database call goes through one small wrapper that can only `find`,
`count_documents`, `aggregate` (with `$out` and `$merge` refused) and `list_collection_names`, and the page is served
with GET only. It reads every Pregame database (any database whose name starts with `pregame_`; the default is
`PREGAME_DB`, today `pregame_demo`, the stage copy), `cabinet`, `cabinet_truth` and `harness`, and nothing else (any
other database answers 403). The page can switch between the Pregame databases (`pregame_demo`, the live runs
`pregame_run_a`/`_b`/`_c`, ...).

The folder is self-contained: `server.py` (Python standard library plus `pymongo`), the page in `static/`, a snapshot
of every endpoint in `fixtures/`, and the findings files in `data/`. No absolute paths.

## Two-minute setup on a new laptop

1. Copy (or unzip) this whole folder anywhere. Leave `.venv` behind if you can; the start script rebuilds it if it was
   copied from another machine anyway.
2. Copy `viewer.env.example` to `viewer.env` and fill in `MONGODB_URI` (ask the team). `PREGAME_DB` is already
   `pregame_demo`. Never commit or share `viewer.env`.
3. Run the start script. It needs Python 3.10 or newer; the first run creates `.venv` and installs `pymongo` and
   `dnspython` (about a minute), then opens http://127.0.0.1:8877/. If 8877 is busy it uses the next free port
   (up to 8887), prints the address it chose, and opens that one.
   - Windows: double-click `start.bat`, or in PowerShell `.\start.ps1`
   - macOS / Linux: `bash start.sh`
4. If the page says the database is unavailable, either add this laptop's IP address to Atlas
   (Atlas > Network Access > Add IP Address) or run the snapshot instead:
   - Windows: `start.bat --offline` (or `.\start.ps1 --offline`)
   - macOS / Linux: `bash start.sh --offline`

`--offline` serves `fixtures/` and needs no packages and no network; the header shows OFFLINE SNAPSHOT with the
time the snapshot was taken.

## Options

Arguments to the start scripts pass straight through to `server.py`:

| Flag | What it does |
| --- | --- |
| `--offline` | Serve the snapshot in `fixtures/` instead of MongoDB. |
| `--snapshot` | Read every endpoint from MongoDB and write it to `fixtures/` (plus `fixtures/_meta.json` with the time), then exit. Run it on a laptop that can reach Atlas just before the demo, then copy the folder. |
| `--port 8900` | Start from another port (default `VIEWER_PORT` from `viewer.env`, else 8877); if it is busy, the next 10 are tried. |
| `--host 0.0.0.0` | Let other machines on the network open it (off by default). |
| `--no-browser` | Do not open a browser tab. |

Environment variables override `viewer.env` (`MONGODB_URI`, `PREGAME_DB`, `PREGAME_LLM_MODE`, `PREGAME_PROVIDER`,
`VIEWER_PORT`). The connection string is never printed, logged, returned by an endpoint or written to a fixture.

## The tabs

1. **Activity**: the audit log (the hash-chained ledger) as a live agent feed (polls every 2 s): who did what (the
   simulated world fires market news and writes advisor feedback, Sonnet (writer) writes briefs, Opus (proposer)
   proposes changes, the test gate (code) scores them on unseen test meetings and adopts, rejects or refuses them),
   one plain-words sentence per entry (the engineering wording and the raw record are one click away), filters by
   actor and field. The side column shows whether the hash chain links up, the latest commit, rejection and refusal, and the
   proposal counts.
2. **Proposals & gate**: one card per proposal: tier (G auto-commits on a held-out win, H needs a person, X is frozen
   and always refused), status, diff, rationale, the gate's decision, and held-out accuracy of candidate vs champion.
3. **Versions**: the current version of each config surface (policy, rules, tools per field, and the global
   guardrails) and the history of what changed.
4. **Briefs**: each prep brief as rendered markdown, with its receipt (config versions and facts used), claims with
   their fact citations, and dropped claims.
5. **Findings**: the cabinet baseline vs the harness (trusted prep rate, preps with a fault, forbidden promises,
   question recall, faults by type and by client; "not scored yet" until `data/cabinet_harness.json` exists), the
   published research on static vs time-aware designs, and Pregame's own held-out eval rows.
6. **Cabinet (demo data)**: the six synthetic clients; each one's timeline of banker notes, call preps and notable
   feed events (trades, tool use, client emails, document requests, reply lags, tech-stock reads). The presenter
   toggle "Show answer key" overlays the planted faults and their fixes.
7. **Databases**: a generic read-only browser over the allowed databases (collection counts and the latest documents).

## Runs

`/api/runs` compares every Pregame database: chain length, proposals adopted / rejected / refused, the current
version's score on unseen test meetings, and when each run happened. `data/run_info.json` says which database is on
stage (`pregame_demo`, a replay recorded from run C), each database's mode (`replay`, or `recorded` for a finished
live run; the header badge follows it), and the setup: every run used the same models (Opus 5.5 proposes, Sonnet 5
writes, Haiku 4.5 grades), so differences between runs are run-to-run variation. Edit it if that changes.

Harness preps (the Cabinet tab) and cabinet runs (Findings) come from the selected database when it has them,
otherwise from the most recently written Pregame database that does (today `pregame_cabinet_dev`); the responses
say which.

## Refreshing the findings

Copy the scorer outputs into `data/`: `cabinet_baseline.json` (already there) and `cabinet_harness.json` when the
harness has been scored. They are read fresh on every request, online and offline; no restart needed.

## Troubleshooting

- **"port 8877 is busy; using 8878 instead"**: fine, the browser opens the right address. **"cannot listen on ...
  or the next 10 ports"**: pass `--port` with a free port, e.g. `--port 8900`.
- **503 "database unavailable"**: the laptop's IP is not on the Atlas allowlist, the Wi-Fi blocks port 27017, or
  `viewer.env` is wrong. Use `--offline`.
- **"MONGODB_URI is not set"**: create `viewer.env` from `viewer.env.example`.
- **pip install fails** (no internet): `--offline` still works; online mode needs `pymongo`.
- **Opening `static/index.html` directly from disk**: the page falls back to `../fixtures/*.json`; some browsers block
  that, so prefer the start script.

## Public copy

The team repo carries a public copy: `python server.py --snapshot --public` writes the public fixture set (the stage
database and the visible cabinet only; no answer key, nothing from `cabinet_truth`), then `python make_public.py`
builds `dist/viewer/` with exactly the files to publish and scans them for secrets, local paths and answer-side data
(it exits non-zero if it finds any). In the public copy the "Show answer key" toggle has nothing to show:
the client timeline says `answer_key_included: false`. Run it with `start --offline`.

## Tests

No network needed:

```
python -m unittest discover -s tests -v
```

They cover the activity sentence for every ledger kind, the chain-link check, offline serving of fixtures, the
database allowlist (403), path traversal, and that no response or fixture contains the connection string or its
password. With `pymongo` installed they also check that an unreachable database is a clean 503.
