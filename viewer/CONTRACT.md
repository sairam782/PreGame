# Pregame viewer: API contract

A read-only local viewer for the demo: what the harness's agents are doing (the ledger and everything around it), the
findings, and the banker cabinet data. It never writes to MongoDB.

Round 2 (14:45): marked **(v2)** below. Default port 8877 with fallback; several Pregame databases (`?db=`);
`/api/runs` with the stage run marked; plain-words activity sentences (`summary`, `summary_technical`,
`actor_label`); cabinet data v2 (dates, `instrument_type`, expected actions, fault `level`, vocabulary); harness preps
and `cabinet_runs`; the v2 scorer output.

Round 3 (15:05): marked **(v3)**. `llm_mode` per database from `data/run_info.json` (`llm_mode_source`); every run used
the same setup (Opus 5.5 proposes, Sonnet 5 writes, Haiku 4.5 answers the questions; code grades), so `actor_label` is "Opus (proposer)"
everywhere and `/api/runs` passes `mode`, `writer_model`, `grader_model`; harness preps and `cabinet_runs` fall back
to the most recently written Pregame database that has them (`cabinet_source_db`, `cabinet_source_fallback`,
`?cabinet_db=`); `PREGAME_LLM_MODE=replay` by default.

Round 4 (15:10): marked **(v4)**. The public copy: `server.py --snapshot --public` and `make_public.py`
(see "Public copy" below); `answer_key_included` on the client timeline; `public` on overview; findings summaries-only
in a public snapshot; a baseline file without `preps` is fine.

## Portability (hard requirement: it must run on a different laptop)

- **One self-contained folder.** No absolute paths anywhere in code or config; every path is relative to the folder
  `server.py` lives in. Copy or zip the folder, and it runs.
- **Python standard library plus `pymongo` only.** The server uses `http.server.ThreadingHTTPServer`, not FastAPI.
  `requirements.txt` lists `pymongo` (with `dnspython`, which `mongodb+srv` needs). `pymongo` is imported lazily, so
  `--offline` needs no packages at all. Works on Windows and macOS with Python 3.10+.
- **Start scripts:** `start.ps1` / `start.bat` (Windows) and `start.sh` (macOS/Linux) create `.venv` in the folder if
  it is missing, install `requirements.txt`, and start the server; the server opens the browser at the URL it actually
  bound. Passing `--offline` through them serves the snapshot.
- **Config:** `viewer.env` in the folder (copy from `viewer.env.example`): `MONGODB_URI`, `PREGAME_DB` **(v2: default
  `pregame_demo`)**, `PREGAME_LLM_MODE` **(v3: `replay`, the stage replay; used only for a database without a `mode` in
  `data/run_info.json`)**, `PREGAME_PROVIDER`, `VIEWER_PORT` **(v2: default 8877)**. Environment variables
  override the file. `viewer.env` is listed in `.gitignore`; never print or return the URI.
- **Offline is first-class:** a laptop that can't reach Atlas (not on the Network Access allowlist, venue Wi-Fi) runs
  `start --offline` from `fixtures/`. The header shows OFFLINE SNAPSHOT and the snapshot time.
- **Findings files travel with the folder:** `data/cabinet_baseline.json` and optional `data/cabinet_harness.json`
  (the scorer outputs, copied in), `data/research_findings.json`, and **(v2)** `data/run_info.json` (per database:
  `label`, **(v3)** `mode`, `proposer`, `proposer_model`, `writer_model`, `grader_model`, `stage`, `recorded_from`,
  `replayable`, `note`; keys starting with `_`, such as `_note`, are comments and ignored; see `/api/runs`).

## Server

- `server.py` serves `http://127.0.0.1:<VIEWER_PORT>` **(v2: default 8877; if that port is busy it tries the next 10,
  prints the URL it actually uses and opens that one)**. `--port N` sets the first port tried. `--host 0.0.0.0` is
  opt-in only. On Windows the port is bound exclusively, so a busy port is detected rather than shared.
- **(v2) Databases read:** every database whose name starts with `pregame_`, discovered at runtime with
  `list_database_names` (cached 30 s), plus `PREGAME_DB` (the default, `pregame_demo`); and `cabinet`, `cabinet_truth`,
  `harness`. Today: `pregame_demo` (the stage copy, replayed from a recorded live run), `pregame_run_a`/`_b`/`_c`
  (three live runs), `pregame_alex`, and the Build thread's `pregame_cabinet_dev`, `pregame_regrade_a`/`_b`/`_c` and `pregame_verify_c`.
  A database or collection that is missing or empty is shown as empty, not an error. If the user may not list
  databases, only `PREGAME_DB` is offered.
- Only `find`, `count_documents`, `aggregate` without `$out`/`$merge`, `list_collection_names`, and **(v2)**
  `list_database_names`. No writes, ever.
- **(v2) `?db=<name>`** selects the Pregame database on `overview`, `activity`, `proposals`, `versions`, `briefs`,
  `world`, `findings`, `cabinet/clients` and `cabinet/client/{cid}`. Omitted or empty means `PREGAME_DB`. A name that
  is not an allowed Pregame database (any other database, including `cabinet`) is 403; offline, a database that is not
  in the snapshot is 403.
- **(v3) Cabinet source.** Harness preps (`cabinet_preps`) and `cabinet_runs` come from the selected database when it
  has either; otherwise from the most recently written Pregame database that has them (by the newest `created_at` or
  ObjectId time in its `cabinet_runs` / `cabinet_preps`; today `pregame_cabinet_dev`); if none has them, from the
  selected database (empty). `?cabinet_db=<name>` overrides the choice (validated like `?db=`, 403 otherwise; no
  fallback then). `/api/cabinet/clients` (on every item), `/api/cabinet/client/{cid}` and `/api/findings` say which:
  `"cabinet_source_db": "pregame_cabinet_dev", "cabinet_source_fallback": true` (`true` when it is not the selected
  database and no `?cabinet_db=` was given).
- Files read: `data/cabinet_baseline.json`, `data/cabinet_harness.json` (may not exist), `data/research_findings.json`,
  `data/run_info.json` (may not exist).
- Every endpoint returns JSON. Datetimes as ISO strings in UTC ending in `Z`, whole seconds
  (`2026-03-17T00:00:00Z`); strings stored inside ledger payloads are returned as stored. Mongo `_id` returned as `id`
  (string); a document that already has its own `id` keeps it.
- Errors are JSON `{"error": "...", "detail"?: "...", "hint"?: "..."}`: 400 bad parameter, 403 database or collection
  not allowed, 404 unknown endpoint / client / file, 405 any method but GET and HEAD, 503 database unreachable, no
  `MONGODB_URI`, pymongo missing, or (offline) a fixture missing from the snapshot. After a connection failure the
  server answers 503 immediately for 8 s instead of waiting on every poll.
- Offline fallback for the stage: `server.py --snapshot` writes every endpoint's response to `fixtures\<name>.json`;
  `server.py --offline` serves those files instead of MongoDB (the header shows OFFLINE SNAPSHOT and the snapshot time).
  Fixture names: `overview`, `activity`, `proposals`, `versions`, `briefs`, `world`, `findings`, `cabinet_clients`,
  `cabinet_client_<cid>` (faults=1), `dbs`, and `db_<db>.<collection>`, plus `_meta.json`. **(v2)** Also `runs` and
  `cabinet_vocabulary`. The default database's fixtures have the plain names; **every other snapshotted Pregame
  database's per-db fixtures carry `@<db>`**: `overview@pregame_run_a.json`, `activity@…`, `proposals@…`,
  `versions@…`, `briefs@…`, `world@…`, `findings@…`, and, only for a database that has cabinet data of its own,
  `cabinet_clients@…` and `cabinet_client_<cid>@…` built from that database. **(v3)** The plain cabinet fixtures (the
  default database's) are built with the cabinet-source fallback applied; offline, the same choice is made from
  `_meta.json` (its rows carry the cabinet counts and `cabinet_written_at`), and the chosen database's own cabinet
  fixtures are served (a fixture whose harness preps belong to another database has them removed). `db_<db>.<collection>` holds the latest 50 documents (20 for non-default Pregame databases).
  `_meta.json`: `{"snapshot_at", "pregame_db" (the default), "pregame_dbs": [{"name", "ledger", "proposals", "briefs",
  "cabinet_preps", "cabinet_runs", "cabinet_written_at", "in_snapshot": true, "fixtures": ["overview", ...]}],
  "llm_mode", "fixtures": [...]}`.
  Every `overview*.json` is written with `offline: true`, `snapshot_at` and that `pregame_dbs` list, so the page shows
  the snapshot badge and the database choice in `?fixtures=1` mode too. Offline, `faults=0` strips the answer side
  (`faults`, `expected`) from the client fixture, `limit`/`after_seq` are applied to the fixture, and findings still
  read `data/` fresh (only `pregame_eval` and `cabinet_runs` come from the snapshot). The snapshot refuses to write
  anything if a fixture would contain `mongodb+srv://` or the URI's password.
- **(v4) Public copy.** `server.py --snapshot --public` replaces `fixtures/` with the public set: the default
  database only (`overview`, `activity`, `proposals`, `versions`, `briefs`, `world`, `findings`, and its
  `db_<db>.<collection>` files), `runs` (the default database and `pregame_run_*` rows), the visible cabinet
  (`cabinet_clients` without `counts.faults`, `cabinet_client_<cid>` with the answer key off and
  `"answer_key_included": false`, `cabinet_vocabulary`, `db_cabinet.<collection>`), `dbs` (the default database and
  `cabinet`), and `_meta.json` with `"public": true, "answer_key_included": false`. Nothing from `cabinet_truth` or
  `harness`, no other `pregame_*` database, and harness preps / cabinet runs from the default database itself (no
  fallback). Findings keep the scorer summaries (`baseline`, `harness`, and each cabinet run's `scores`: baseline /
  naive / harness) without `faulty_preps_by_client`, and drop `baseline_preps`, `harness_preps`,
  `*_faults_by_client`; a cabinet run keeps `id, created_at, data_source, policy, prep_count, preps_collection,
  scores`. Local absolute paths inside strings are cut to the file name. Served offline, a public snapshot never
  returns the answer key: `faults=1` gives the timeline without `faults` / `expected` and
  `"answer_key_included": false`, and findings are filtered the same way even if `data/` holds the full files.
  `make_public.py` then builds `dist/viewer/` (server, page, docs, start scripts, tests, `.gitignore`,
  `viewer.env.example`, `data/research_findings.json`, `data/run_info.json`, `data/cabinet_baseline.json` reduced to
  `source` + `summary`, and the public fixtures), prints a manifest and scans every file for the connection string,
  password, cluster host and user name, local paths, and answer-side data in JSON (exit 1 if anything is found).
- The page also works with no server at all for development: `static/index.html?fixtures=1` loads `../fixtures/*.json`
  (so fixtures must be reachable at `/fixtures/<name>.json` when served, and relative to the page when opened as a file).

## Endpoints

### `GET /api/overview?db=` (fixture `overview.json`, `overview@<db>.json`)
```json
{"generated_at": "2026-09-26T17:50:00Z", "offline": false, "snapshot_at": null,
 "pregame_db": "pregame_demo", "active_db": "pregame_demo", "default_db": "pregame_demo",
 "pregame_dbs": [{"name": "pregame_demo", "ledger": 21, "proposals": 4, "briefs": 4, "cabinet_preps": 0, "cabinet_runs": 0},
                 {"name": "pregame_run_a", "ledger": 21, "proposals": 4, "briefs": 4, "cabinet_preps": 0, "cabinet_runs": 0}],
 "sim_time": "2026-03-17T00:00:00Z", "llm_mode": "fake", "provider": "anthropic",
 "counts": {"ledger": 21, "proposals": 4, "briefs": 4, "feedback": 2, "facts": 44, "events_fired": 3, "eval_runs": 8},
 "chain": {"entries": 21, "links_ok": true, "first_break_seq": null, "last_seq": 21, "last_hash": "532ffdc4..."},
 "proposals_by_status": {"committed": 1, "rejected": 3, "pending": 0, "awaiting_approval": 0},
 "latest": {"commit": {...activity item...}, "reject": {...}, "refused": {...}, "brief": {...}}}
```
**(v2)** `active_db` is the database this response describes (`pregame_db` says the same, kept for the page);
`default_db` is `PREGAME_DB`. `pregame_dbs` lists every allowed Pregame database with counts (cached 5 s), ordered
`pregame_demo`, `pregame_run_*`, then the rest. Offline it lists only the snapshotted databases, each with
`"in_snapshot": true` and `"fixtures"` (which fixture names exist for it).
**(v3)** `llm_mode` is per database: `data/run_info.json`'s `mode` for that database when present
(`"llm_mode_source": "run_info"`), else `PREGAME_LLM_MODE` from the env file (`"llm_mode_source": "env"`; `null` when
unset). Today: `pregame_demo` -> `replay`; `pregame_run_a`/`_b`/`_c` -> `recorded` (a finished live run, not happening
now; suggested badge RECORDED RUN); other databases -> the env value, `replay`. The harness's own modes are `fake`,
`live`, `record` (live calls being recorded to a cassette) and `replay` (served from the cassette). Offline, the mode is
worked out the same way from the current `data/run_info.json` and `viewer.env`. `provider` comes from
`PREGAME_PROVIDER`.
`chain.links_ok`: every ledger entry's `prev_hash` equals the previous entry's `hash`, in `seq` order (no payload
re-hashing); the first entry's is `GENESIS`, and a gap in `seq` counts as a break. `last_hash` is the full hash.
`proposals_by_status` has every harness status (`pending`, `evaluating`, `awaiting_owner`, `committed`, `rejected`,
`stale`) plus `awaiting_approval`, an alias with the same count as `awaiting_owner` (the harness's name for "needs a
person"). `latest.*` is an activity item or `null`.

### `GET /api/activity?db=&after_seq=0&limit=300` (fixture `activity.json`, `activity@<db>.json`)
The ledger, newest last, as agent actions:
```json
{"db": "pregame_demo", "items": [
  {"seq": 21, "kind": "reject", "actor": "gate", "actor_label": "test gate (code)",
   "sim_time": "2026-03-17T00:00:00Z", "recorded_at": "2026-09-26T17:40:00Z", "field": "retirement",
   "summary": "Test gate rejected the proposed change to what goes into the brief (retirement): the proposed version answered 97% of unseen test meetings' questions correctly vs 94% for the current version, a gain too small to trust.",
   "summary_technical": "Rejected prop-92fe0734: held-out accuracy 0.97 vs champion 0.94 is inside the 0.05 margin.",
   "status": "rejected", "ref": {"type": "proposal", "id": "prop-92fe0734"}, "hash": "532f...", "prev_hash": "8186...",
   "payload": {...the ledger payload exactly as stored...}}],
 "last_seq": 21}
```
**(v2) Plain words for judges.** `summary` is now the plain sentence, built at display time with the shared
vocabulary; `summary_technical` holds the engineering sentence (the v1 `summary`); `actor` and `payload` (including
its `decision`) are exactly as stored, and `actor_label` is added. Stored text is never altered.
Vocabulary: held-out -> "unseen test meetings"; tuning -> "practice meetings"; champion / candidate -> "current
version" / "proposed version"; accuracy -> "questions answered correctly" (shown as a percentage); proposal ->
"proposed change"; committed -> "adopted"; tiers G / H / X -> "adopted automatically if it wins" / "needs a person's
sign-off" / "never allowed: it would change how the system is graded"; policy / rules / tools / guardrails -> "what
goes into the brief" / "writing instructions" / "data sources" / "safety checks" (the frozen `scenarios` surface ->
"the unseen test meetings' questions"); false alarms -> "false alarms (flagging news that doesn't affect the client)";
context tokens -> "facts given to the writer"; ledger -> "audit log". Diff lines are put in words (`max_facts: 6 -> 10` -> "use up
to 10 facts (was 6)", `include_kinds: + regulation` -> "also include tax and rule facts", `prefer_exposed: False ->
True` -> "put the facts that affect this client first"). `actor_label`: `improver` -> "<proposer> (proposer)" (the
proposer named for that database in `data/run_info.json`, default "Opus"; **(v3)** "Opus (proposer)" in every run
today), `gate` -> "test gate (code)", `drafter` ->
"Sonnet (writer)", `world` -> "simulated world", `guardrails` -> "safety checks (code)", `owner:<name>` -> "<name>
(person)". Gate sentences are built from the numbers: an eval or reject entry reads the proposal's held-out means
(from the eval payload, else the `proposals` collection) and the reasons from the stored decision (margin -> "a gain
too small to trust", guardrail violations -> "it failed more safety checks", context -> "the facts given to the writer grew too
much", false alarms, worst scenario, pass^k, missed / stale / uncited claims); without numbers the sentence falls back
to plain words, and an unknown kind to "<actor_label> <kind>".
Actors seen today: `world` (seed, event, feedback), `drafter` (brief), `improver` (proposal), `gate` (eval, commit,
reject, refused). Kinds seen: `seed`, `event`, `feedback`, `brief`, `proposal`, `eval`, `commit`, `reject`, `refused`.
`summary_technical` is one sentence built per kind from the payload; unknown kinds fall back to `"<actor> <kind>"`. `status` is set for proposal-related kinds: `proposal` -> `pending`, `eval` -> the
gate's outcome (`rejected`, `committed`, `awaiting_owner`), `commit`/`approve` -> `committed`, `reject` -> `rejected`
or `stale`, `refused` of a proposal -> `refused`; otherwise `null`. The harness can also write `approve` and
`rollback`, and `refused` entries from `improver` (a blocked read) and `guardrails` (a blocked brief); each has its
own sentence. `field` comes from the payload (a feedback entry's from its feedback document; a commit's from its key,
so guardrails commits say `global`). `ref` is `{"type": "proposal"|"brief"|"feedback"|"event"|"version", "id"}` or
`null`. Items are the newest `limit` entries with `seq > after_seq`, oldest first; `last_seq` is the ledger's highest
seq (if it is lower than `after_seq`, the ledger was reset: reload from 0). **(v2)** `db` names the database; when the
page switches database it should restart from `after_seq=0`.

### `GET /api/proposals?db=` (fixture `proposals.json`, `proposals@<db>.json`)
A JSON array, newest first: `id, field, kind, key, tier, status, filed_by, created_sim, created_at, base_version,
rationale, diff (list of strings), evidence, decision, tuning, heldout_candidate, heldout_champion, committed_version,
evaluated_versions, body`. `tuning`/`heldout_*` are EvalSummary objects (`mean_accuracy`, `worst_accuracy`, `pass_k`,
... `per_scenario`) or `null` (a refused X proposal has none).
Tiers: G (auto on held-out win), H (needs a person), X (frozen, always refused).

### `GET /api/versions?db=` (fixture `versions.json`, `versions@<db>.json`)
`{"heads": [{"id": "policy:retirement", "kind": "policy", "key": "retirement", "version": 2}],
  "versions": [{"id", "kind", "key", "version", "body", "rationale", "proposal_id", "approved_by", "supersedes",
                "restores", "created_sim", "created_at", "changed_keys": ["recency_days"]}]}`
`changed_keys` compares the body with the previous version of the same kind:key: changed dict keys for policy and
tools, changed rule / guardrail ids for rules and guardrails (`["order"]` if only the order moved), `[]` for v1.
Heads are sorted policy, rules, tools, guardrails; versions newest first.

### `GET /api/briefs?db=&limit=20` (fixture `briefs.json`, `briefs@<db>.json`, all briefs)
A JSON array, newest first (by ledger order): `id, seq, field, account_id, as_of, model, config_label, markdown,
sections, receipt, dropped_claims`.

### `GET /api/world?db=` (fixture `world.json`, `world@<db>.json`)
`{"events": [{"id", "field", "title", "month", "at", "fired", "fired_at", "feedback", "fact_count"}], "feedback": [{"id",
"field", "brief_id", "event_id", "text", "sim_time"}], "facts_by_field": {"retirement": 20}}` (events by `at`; an
event's `feedback` is its scripted advisor feedback text).

### (v2) `GET /api/runs` (fixture `runs.json`)
One row per Pregame database (every allowed `pregame_*` database; `pregame_demo` first, then `pregame_run_*`, then the
rest), comparing the runs:
```json
{"runs": [{"db": "pregame_demo", "label": "Stage copy (replay of run C)", "proposer": "Opus",
           "proposer_model": "Opus 5.5", "mode": "replay", "writer_model": "Sonnet 5", "grader_model": "Haiku 4.5",
           "stage": true, "recorded_from": "run C", "replayable": null,
           "note": "The stage copy, reloaded at 14:31 from run C's recorded live run.",
           "chain": {"entries": 21, "links_ok": true, "last_seq": 21},
           "proposals_by_status": {"committed": 1, "rejected": 3, "pending": 0, "awaiting_approval": 0, ...},
           "models": ["claude-sonnet-5"],
           "first_at": "2026-09-26T17:50:01Z", "last_at": "2026-09-26T18:20:35Z",
           "committed": [{"id", "field", "kind", "diff", "decision", "heldout_champion": {...}, "heldout_candidate": {...}}],
           "rejected": [{"id", "field", "kind", "tier", "status", "diff", "decision",
                         "heldout_champion_mean", "heldout_candidate_mean"}],
           "refused": 1,
           "champion_mean_accuracy": [0.719841, 0.719841, 0.938889]}]}
```
`models` are the distinct `briefs.model` values (the drafter's model: today `claude-sonnet-5` in every run; the
databases do not record the other models). `label`, **(v3)** `mode`, `proposer`, `proposer_model`, **(v3)**
`writer_model`, `grader_model`, `stage`, `recorded_from`, `replayable` and `note` come from `data/run_info.json`
(`null`, or `false` for `stage`, when a database is not listed). Today: every run used the same setup (Opus 5.5
proposes, Sonnet 5 writes, Haiku 4.5 answers the questions; code grades), so differences between runs are run-to-run variation; `pregame_demo` is
`stage: true, recorded_from: "run C", mode: "replay"`; `pregame_run_a`/`_b`/`_c` are `mode: "recorded"`, A and B
`replayable: false` ("earlier live run"), C `replayable: true`. `first_at`/`last_at` are the ledger's earliest and latest
`recorded_at`. `committed` summaries drop `per_scenario`. `rejected` lists rejected or stale proposals that the gate
evaluated (tier G/H); tier X (frozen-surface) proposals are counted in `refused` instead, which is the number of
`refused` ledger entries. `champion_mean_accuracy` is the champion's held-out `mean_accuracy` for each evaluated
proposal, oldest first (so it moves after a commit).

### `GET /api/findings?db=` (fixture `findings.json`, `findings@<db>.json`)
```json
{"db": "pregame_demo", "cabinet_source_db": "pregame_cabinet_dev", "cabinet_source_fallback": true,
 "cabinet": {"baseline": {...the summary object of data/cabinet_baseline.json, unchanged...},
             "harness": null,
             "baseline_faults_by_client": {"C01": "2/4"}},
 "research": {...contents of data/research_findings.json...},
 "pregame_eval": [{"field", "split", "config_label", "k", "summary": {...eval_runs summary...}, "created_at"}],
 "cabinet_runs": [{...latest cabinet_runs documents...}] | null}
```
`baseline` is the `summary` object of `data/cabinet_baseline.json`, passed through unchanged. **(v2)** The scorer's v2
output adds `preps_with_a_fault_ignoring_warnings`, `trusted_prep_rate_ignoring_warnings`, `warnings_total`,
`history_claims`, `expected_actions`, `expected_actions_source`, `expected_actions_by_type`, `actions_hit`,
`actions_hit_by_type` and `action_recall` to the v1 keys (`preps`, `preps_with_a_fault`, `trusted_prep_rate`,
`faults_total`, `faults_by_type`, `faulty_preps_by_client`, `compliance_drift_by_severity`, `forbidden_promises`,
`questions_expected`, `questions_hit`, `question_recall`, `questions_unneeded`).
`harness` is the same summary from `data/cabinet_harness.json` when that file exists, else null. Also in `cabinet`:
`harness_faults_by_client`, `baseline_source`, `harness_source`, and `baseline_preps` / `harness_preps`
(`[{"prep_id", "client_id", "date", "fault_types": [...], "warnings": n, "questions_expected": n, "questions_hit": n,
"expected_actions": n, "actions_hit": n}]`; **(v2)** `warnings`, `expected_actions`, `actions_hit` are new). `research`
is null if the file is missing. `pregame_eval` holds the selected database's `eval_runs` summary rows, oldest first.
**(v2)** `cabinet_runs` is the latest 20 `cabinet_runs` documents (newest first, every field kept; today only
`pregame_cabinet_dev` has one: `policy`, `prep_ids`, `scores` per policy, ...), or `null` when there are none,
**(v3)** read from the cabinet source database (see Cabinet source; `?cabinet_db=` overrides). If the database is unreachable the files still load, `pregame_eval` is `[]`, `cabinet_runs` is `null` and
`pregame_eval_error` says why. `errors` lists data files that could not be parsed.

### `GET /api/cabinet/clients?db=` (fixture `cabinet_clients.json`)
`[{"client_id": "C01", "name": "Daniel Reyes", "age": 52, "tier": "B", "household": [...], "accounts": [...],
   "counts": {"notes": 10, "preps": 4, "events": 82, "faults": 7, "harness_preps": 0}}]`
**(v2)** `counts.harness_preps` counts the client's `cabinet_preps` **(v3)** in the cabinet source database; every
item also carries `cabinet_source_db` and `cabinet_source_fallback` (the response stays an array). The response
stays a JSON array; the vocabulary has its own endpoint.

### (v2) `GET /api/cabinet/vocabulary` (fixture `cabinet_vocabulary.json`)
`{"vocabulary": {...the cabinet.vocabulary document without its id...}}`: the allowed values per attribute
(`risk_attitude`, `investing_style` with a sentence each; `decision_maker`, `retirement_date`, `fee_rate`,
`disclosure` described) and `_rules` (`as_of`, `instrument_type`). `null` if the collection is empty.

### `GET /api/cabinet/client/{cid}?faults=0|1&db=` (fixtures `cabinet_client_C01.json` ... `C06`, all with faults=1)
```json
{"client": {...}, "db": "pregame_demo", "cabinet_source_db": "pregame_cabinet_dev", "cabinet_source_fallback": true,
 "timeline": [
  {"date": "2026-05-12", "kind": "feed", "id": "E-0316", "type": "trade_sell", "instrument_type": "fund",
   "text": "trade_sell instrument=Balanced Growth Fund, instrument_type=fund, amount=110000", "notable": true},
  {"date": "2026-05-13", "kind": "note", "id": "N-0010", "author": "banker", "text": "Called him after..."},
  {"date": "2026-07-18", "kind": "prep", "id": "P-0003", "text": "CALL PREP: ...",
   "faults": [{"fault_type": "sticky_label", "level": "fault", "attribute": "risk_attitude", "claimed": "cautious", "truth": "growth", "fix": "...", "evidence": ["E-0320"]}],
   "expected": [{"attribute": "risk_attitude", "action": "ask", "reason": "...", "evidence": ["E-0320", "E-0321"]}]},
  {"date": "2026-07-18", "kind": "harness_prep", "id": "CR-...:P-0003", "client_id": "C01", "prep_id": "P-0003",
   "text": "CALL PREP: ...", "policy": "harness", "label": null, "run_id": "CR-...", "fields": {...every field...}}]}
```
Feed items: include every event but mark `notable: true` for trades, tool use, client emails, document requests,
reply lags, tech-stock articles and non-newsletter emails (the page hides the rest by default). **(v2)** Feed items
carry `instrument_type` (`fund`, `index_fund`, `single_stock`, or `null`), also in `text`.
**(v2) Dates:** cabinet v2 stores every date as a BSON date (notes, preps, book summary, reference data, and the
hidden side); v1 stored strings. Both are normalised to `YYYY-MM-DD` for the timeline.
The answer side appears only when `faults=1` (a presenter toggle): `faults` read from `cabinet_truth.answer_key` by
`doc_id`, on every note and prep (an empty list when clean; notes have faults too, e.g. drifted disclosures), never on
feed items. A fault also carries `fault_id`, and `severity` / `claimed_text` when the answer key has them, and
**(v2)** `level` (`warning` for severity-1 compliance drift, else `fault`) when the answer key has it (it is set on the
21 compliance-drift faults only). **(v2)** Preps also get `expected`: the expected actions from
`cabinet_truth.expected_actions` for that `prep_id` (`attribute`, `action` = `ask` | `flag` | `brief_both`, `note_id`
for flags, `reason`, `evidence`), an empty list when none. `faults=0` removes both `faults` and `expected`.
Preps also carry `used_note_ids`. `client` includes `holdings` and the same `counts` as the list.
**(v2) Harness preps:** **(v3)** from the cabinet source database (the selected one, or the fallback) when its
`cabinet_preps` is non-empty (today only
`pregame_cabinet_dev`, 24 preps from one run), its documents for this client (matched on `client_id` or `client`) join
the timeline as `kind: "harness_prep"`, with `date` (from `date`, `as_of`, ... normalised), `id` (the document's id),
`client_id`, `prep_id`, `text` (from `text` or `markdown` ...), `policy`, `label` and `run_id` when found, and
`fields`: the whole document. With `faults=1` a harness prep also gets `expected` for its `prep_id`.
Items sort by date, then kind (feed, note, prep, harness_prep), then id. Unknown client: 404.
**(v4)** `answer_key_included` is `true` when the response carries the answer side (`faults=1` on a full snapshot or
live) and `false` otherwise; a public snapshot always says `false`, so the presenter toggle should then say the answer
key is not in the public snapshot. `/api/overview` also says `"public": true|false`.

### `GET /api/dbs` (fixture `dbs.json`) and `GET /api/db/{db}/{collection}?limit=20`
Generic read-only browser over the allowed databases: names, collection counts, and the latest `limit` documents
(sorted by `_id` descending) as JSON. Refuse any other database with 403.
`/api/dbs`: `{"dbs": [{"name": "pregame_demo", "role": "pregame", "default": true, "collections": [{"name": "ledger",
"count": 21}]}]}` (**(v2)** every `pregame_*` database has role `pregame`; `default` marks `PREGAME_DB`; `system.*`
collections are hidden). `/api/db/{db}/{collection}` (limit 1-200):
`{"db", "collection", "count", "limit", "docs": [...]}`; number arrays longer than 32 (embeddings) are cut to 8 plus
a `"... N more numbers"` marker, strings to 4000 chars. A `system.*` collection is 403; a missing collection is empty.

## Page (static/)

`static/index.html`, `static/app.js`, `static/styles.css`, theme in `static/theme/tokens.css` (the prep kit's
corporate look: navy hero, slate text, blue accent, IBM Plex Sans embedded). Tabs:

1. **Activity**: live agent feed from `/api/activity`, polling every 2 s with `after_seq`; actor chips; filters by
   actor and field; new items highlighted; click to expand the payload. A side column: chain status, latest commit,
   latest rejection or refusal, proposal counts.
2. **Proposals & gate**: one card per proposal: tier and status badges, diff, rationale, decision text, held-out
   candidate vs champion shown as two labelled bars.
3. **Versions**: current head per kind and field, and version history with what changed.
4. **Briefs**: pick a brief; rendered markdown, receipt, claims with their fact-id citations, dropped claims.
5. **Findings**: cabinet baseline vs harness (tiles: trusted prep rate, preps with a fault, forbidden promises,
   question recall; faults by type as paired bars; per-client grid); "not scored yet" state for the harness; the
   research table (static bot vs time-aware design, with caveats); Pregame held-out eval rows.
6. **Cabinet (demo data)**: client list; per-client timeline of notes, preps and notable feed events; a presenter
   toggle "Show answer key" that overlays the faults and their fixes.
7. **Databases**: the generic browser.

Header: title, sim clock, database, mode badge (`llm_mode`: fake shows STAND-IN, cassette/`replay` shows REPLAY,
live and `record` show LIVE), READ-ONLY badge, connection dot with last refresh time, OFFLINE SNAPSHOT badge when offline.
