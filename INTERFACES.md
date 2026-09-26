# Module interfaces (the contract between build agents)

Data shapes live in `pregame/contracts.py`. Signatures below are binding: implement them exactly, and call other
modules only through them. `db` is a `pymongo.database.Database` (or mongomock's). `llm` is a `pregame.llm.LLM`.
Times are tz-aware UTC datetimes. Pure functions take and return plain dicts and never touch the database.

## pregame/config.py
- `@dataclass Settings`: `mongodb_uri: str`, `db_name: str = "pregame"`, `anthropic_api_key: str | None`,
  `llm_mode: str` (`"fake" | "live" | "record" | "replay"`), `models: dict[str, str]` (roles `drafter`, `reader`,
  `improver`), `cassette_path: str`, `k: int = 2`. Read from environment / `.env` (python-dotenv). Defaults: mode
  `fake` when no API key, `live` otherwise; `MONGODB_URI` default `mongodb://localhost:27017`.
- `settings() -> Settings` (cached).

## pregame/llm.py
- `class LLM`: `is_fake: bool`; `model_id(role) -> str`;
  `complete_json(role, system, prompt, max_tokens=2000) -> dict` (returns the parsed JSON object the model wrote;
  retries once on invalid JSON; raises `LLMError`). In fake mode `complete_json` raises `LLMError` — each module
  implements its own deterministic fake path when `llm.is_fake` is true.
- `get_llm(settings=None) -> LLM`. Modes: `live` (Anthropic SDK), `fake`, `record` (live + append each call to the
  cassette JSONL keyed by sha256 of (role, model, system, prompt)), `replay` (serve from the cassette; missing key ->
  `LLMError`). Thread-safe (the oracle calls it from a thread pool).

## pregame/db.py
- `get_db(settings=None) -> Database` — `MongoClient(uri, server_api=ServerApi("1"))` (never `strict=True`), `appname="pregame"`.
- `init_db(db) -> None` — idempotent: collections, strict `$jsonSchema` validators on `proposals`,
  `config_versions`, `ledger`; indexes: unique `config_heads._id` (natural), unique `proposals.idem_key`,
  `facts` `(field, subject, relation, valid_from)`, `briefs` `(field, as_of)`, `eval_runs` `(config_hash, scenario_id, run)` unique.
  Under mongomock, skip validators silently.
- `run_txn(db, fn) -> Any` — calls `fn(session)` inside `with_transaction`; under mongomock (no transactions) calls
  `fn(None)`. `fn` must be side-effect free outside the database (it may run twice).
- `reset_db(db) -> None` — drops every pregame collection.
- `is_mock(db) -> bool`.

## pregame/ledger.py
- `append(db, kind, actor, payload, sim_time, session=None) -> LedgerEntry` — seq = last seq + 1, hash chain.
- `verify(db) -> tuple[bool, int, str]` — (ok, entries checked, first problem or "").
- `tail(db, n=30) -> list[LedgerEntry]` (newest first).

## pregame/defaults.py  (owned by the db agent)
- `DEFAULT_POLICY: Policy`, `DEFAULT_RULES: dict[field, list[Rule]]`, `DEFAULT_TOOLS: Tools`,
  `DEFAULT_GUARDRAILS: list[Guardrail]`. v1 is deliberately *reasonable but improvable*: e.g. recency 180 days,
  max_facts 6, include_kinds without `regulation` and `disruption`, likely_questions 3, prefer_exposed False,
  analyst_notes off.

## pregame/versions.py
- `class StaleVersion(Exception)`.
- `seed_configs(db, sim_time) -> None` — v1 for `policy:<field>`, `rules:<field>`, `tools:<field>` (each field) and `guardrails:global`; heads at 1; ledger `seed`.
- `head(db, kind, key) -> int`; `get_version(db, kind, key, version=None) -> ConfigVersion` (None = head).
- `history(db, kind, key) -> list[ConfigVersion]` (oldest first).
- `resolve_field_config(db, field) -> FieldConfig`.
- `with_change(cfg: FieldConfig, kind, body) -> FieldConfig` — pure: a copy with one surface replaced (for candidates).
- `config_hash(cfg: FieldConfig) -> str` — sha256 of the bodies (not the version numbers).
- `commit(db, kind, key, base_version, body, *, rationale, sim_time, proposal_id=None, approval_hash=None, approved_by="gate", restores=None) -> ConfigVersion`
  — ONE transaction: head update filtered on `version == base_version` (zero matches -> `StaleVersion`), insert
  `"<kind>:<key>@v<n+1>"`, ledger `commit` (or `rollback` when `restores` is set), and if `proposal_id`, proposal
  status `committed`. No model calls inside.
- `rollback(db, kind, key, to_version, actor, sim_time) -> ConfigVersion` — a new version carrying `to_version`'s body.

## pregame/world/  (fields.py, scenarios.py pure; store.py uses the db)
- `fields.py`: `SIM_START: datetime`; `sim_date(month: int, day: int = 1) -> datetime`;
  `ACCOUNTS: dict[str, list[Account]]` (first account per field is the demo account);
  `BASE_FACTS: dict[str, list[Fact]]` (month 0); `EVENTS: dict[str, list[MarketEvent]]` (months 1..6, 5-6 per field);
  `event_by_id(event_id) -> MarketEvent`.
- `scenarios.py`: `build_scenarios(seeds=(1, 2)) -> list[Scenario]` (two seeds: the lead chose them to keep live gate runs affordable; 36 scenarios) — months 1-3 are `tuning`, months 4-6 are
  `heldout` (a temporal split); seeds vary account, question wording/order and nudge numeric values so answers cannot
  be memorised. `live_scenario(field, account, facts, as_of) -> Scenario` — the questions a client would ask on a
  call today (used for the post-call feedback, never for the gate).
- `store.py`: `load_world(db) -> None` (base facts, event definitions, clock at `sim_date(0)`, ledger `event` entry for
  the load); `load_scenarios(db, scenarios) -> None`; `sim_now(db) -> datetime`; `fire_event(db, event_id) -> list[Fact]`
  (inserts the event's facts, marks it fired, moves the clock to the event's time, ledger `event`);
  `facts_until(db, field, as_of) -> list[Fact]`; `list_events(db, field=None) -> list[dict]` (with `fired: bool`);
  `get_account(field, account_id=None) -> Account`.

## pregame/compiler.py  (pure)
- `compile_context(cfg: FieldConfig, account: Account, facts: list[Fact], as_of: datetime) -> Context` —
  drop facts after `as_of`; keep only the newest per `(subject, relation)` (count the rest as `excluded_superseded`);
  keep sources whose tool is on; keep `include_kinds`; keep within `recency_days` (account facts exempt); rank
  exposures first when `prefer_exposed`, then newest; cap at `max_facts`; build the `Receipt`.

## pregame/drafter.py
- `draft_brief(ctx: Context, llm, config_label="live") -> Brief` — live: the drafter model gets the rules and
  guardrails verbatim, the facts with ids, the policy's section order and question count, and must return JSON
  `{"sections": {name: [{"text", "fact_ids"}]}}`. Fake: deterministic claims built from the facts. `_id` = uuid4 hex.
- `render_markdown(sections, ctx) -> str` — the sections, then a Disclosures block that inserts `contracts.APPROVED_LANGUAGE`
  (AS-01, AS-02) word for word; the model never writes a disclosure.

## pregame/oracle.py  (FROZEN at runtime: the improver never imports or calls it)
- `GUARDRAIL_CHECKS: dict[str, Callable[[Brief, Context], list[str]]]` — at least `cite-facts`, `no-stale-facts`, `no-advice`,
  `approved-language`.
- `check_answer(question, answer) -> tuple[bool, str]` — pure; numbers normalised; `impossible` is correct only if
  the answer says it is unknown / needs follow-up.
- `grade(brief, ctx, scenario, llm) -> Grade` — the reader model sees ONLY the brief's markdown and the questions and
  answers each in one sentence (or "unknown"); correctness is decided by `check_answer`. Fake reader: sentence
  retrieval from the markdown by keyword overlap.
- `evaluate_grades(cfg, scenarios, llm, k=2, config_label="champion") -> dict[scenario_id, list[Grade]]` — the per-run grades (the gate stores tuning failure reasons from them); `evaluate` wraps it.
- `evaluate(cfg, scenarios, llm, k=2, config_label="champion") -> EvalSummary` — for each scenario and run: compile,
  draft, grade (thread pool when live); aggregate via `metrics.summarize`.

## pregame/metrics.py  (pure, FROZEN)
- `summarize(config_label, split, field, k, grades: dict[str, list[Grade]]) -> EvalSummary`.
- `compare(candidate: EvalSummary, champion: EvalSummary) -> dict` — `{"win": bool, "delta_accuracy", "reasons": [...]}`:
  win needs mean accuracy >= champion + WIN_MARGIN, worst scenario not lower, pass^k not lower, and no companion
  metric worse (missed changes, false alarms, stale claims, uncited claims, guardrail violations; context tokens at
  most +50%). Live briefs fail closed on guardrails, so a candidate may not buy accuracy with violations.

## pregame/improver.py
- `class ImproverView` — a plain-data snapshot built by trusted code (`build_snapshot(db)` / `ImproverView(db)`); it
  holds NO database, client, collection, cursor or closure over one. It exposes only `feedback`, live `briefs`,
  config versions and heads, `eval_runs` rows where `split == "tuning"` (aggregates and per-question reasons), and past
  proposals with held-out results and hashes removed. Any other collection, or heldout rows, raises `PermissionError`
  and queues the attempt on `view.refusals`. Because the view cannot write, logging is deferred by design: trusted code
  (`loop.improve`, in a `finally`) persists the queue with `record_refusals(db, view, sim_time)` as ledger `refused`
  entries. Any caller that builds a view must do the same.
- `propose(view: ImproverView, field, llm, sim_time) -> Proposal | None` — one small change (policy knobs, a rule
  replacement, a tool switch, a guardrail change) with `diff`, `rationale`, `evidence`. Fake: heuristics from feedback text.
- `tamper_proposal(field, sim_time) -> Proposal` — a proposal against a frozen surface (`kind="scenarios"`), for the demo.

## pregame/gate.py
- `classify(proposal, current_body) -> Tier` — pure, per the tier table in DESIGN.md.
- `validate(proposal) -> list[str]` — pure: bounds, known kinds/checks, RULES_CAP.
- `file_proposal(db, proposal) -> Proposal` — insert (idempotent on `idem_key`), ledger `proposal`.
- `evaluate_proposal(db, proposal_id, llm, k=None) -> Proposal` — X -> `rejected` + ledger `refused`; invalid ->
  `rejected`; else screen on tuning, then heldout candidate vs champion (champion results cached in `eval_runs` by
  `config_hash`); lose -> `rejected`; G + win -> `versions.commit(approved_by="gate")` -> `committed`; H + win ->
  `awaiting_owner` with `approval_hash`. Ledger `eval` + decision. Stores the three summaries on the proposal.
  Cached evaluations are `EvalRun` rows (contracts.py) in `eval_runs`; only the gate writes them.
- `approve(db, proposal_id, approval_hash, owner, sim_time) -> ConfigVersion` — hash must match; head must still be at
  `base_version` (else proposal `stale`); commits with `approved_by=f"owner:{owner}"`; ledger `approve`.
- `reject(db, proposal_id, owner, reason, sim_time) -> Proposal`.

## pregame/loop.py, pregame/cli.py, pregame/web/
- `loop.setup(db, llm=None) -> dict`, `loop.make_brief(db, field, account_id, llm) -> Brief`,
  `loop.market_event(db, event_id, llm) -> dict` (fire, brief, simulated call via `live_scenario`, feedback if the
  brief missed a material change), `loop.improve(db, field, llm) -> Proposal`, `loop.status(db) -> dict`,
  `loop.run_demo(db, llm) -> None`.
- `python -m pregame.cli <command>`: `setup`, `events`, `fire <event_id>`, `brief <field> [account]`, `improve <field>`,
  `proposals`, `approve <id> <hash>`, `reject <id>`, `rollback <kind:key> <version>`, `trace <brief_id>`,
  `ledger [--verify]`, `tamper <field>`, `demo`, `serve`.
- `pregame/web/app.py`: FastAPI; `GET /` serves `static/index.html`; `GET /api/state` returns `loop.status(db)`;
  `GET /api/brief/{id}`. The page polls every 2 s. It is a lens on the loop, not the product.
