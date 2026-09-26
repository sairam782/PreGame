"""Shared data shapes and constants. Every module builds against this file; change it only with the lead's say-so.

All records are plain dicts (they go straight into MongoDB). The TypedDicts below document their keys.
Times are timezone-aware UTC datetimes. "Simulated time" (sim time) is the world's clock, not the wall clock.

Domain: Pregame prepares a FINANCIAL ADVISOR for a review meeting with a client household. The advisor has to keep
up with two things that move: the WORLD (rates, markets, tax rules, products) and the CLIENTS (their lives and
preferences). Everything in the prototype is simulated.
"""
from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional, TypedDict

# ---------------------------------------------------------------------------------------------------------------
# Fields (client segments), sources and fact kinds
# ---------------------------------------------------------------------------------------------------------------
# retirement: pre-retirees and retirees. families: working households saving for children and a home.
# business_owners: founders and owners planning liquidity, succession and taxes.
FIELDS = ("retirement", "families", "business_owners")
Field = Literal["retirement", "families", "business_owners"]

# Where a fact came from. The context policy's `tools` switch sources on and off.
# market_feed: verified data about the world (rates, markets, tax rules, products).
# account_notes: the advisor's own notes on the household (client memory: life events, preferences, decisions).
# analyst_notes: unverified commentary and rumour; broader coverage, but some of it is wrong (that is the point).
SOURCES = ("market_feed", "account_notes", "analyst_notes")

# What a fact is about. The context policy's `include_kinds` filters on these. The ids are historical; in the
# advisory domain they mean:
#   price       rates, yields and prices (Treasury yields, mortgage and CD rates, annuity payouts)
#   regulation  tax and rules (contribution limits, RMD age, estate exemption, capital gains rate)
#   competitor  product changes (fund fees, fund closures and mergers, new offerings)
#   disruption  market shocks (a sell-off, a bank failure, a fund gating withdrawals)
#   demand      the economy (inflation, jobs, the Social Security COLA, tuition growth)
#   account     the client's own life and preferences (client memory; owned via account_id)
FACT_KINDS = ("price", "regulation", "competitor", "disruption", "demand", "account")


class Fact(TypedDict):
    """One dated statement about the world or a client. Insert-only; a newer fact with the same (subject, relation)
    supersedes (a client note that replaces an older one supersedes it the same way)."""
    _id: str                    # "<field>:<subject>:<relation>@<valid_from ISO date>"
    field: str                  # one of FIELDS
    subject: str                # e.g. "treasury_yields", "rmd_age", "okafor_risk_preference"
    relation: str               # e.g. "ten_year_pct", "start_age", "stance"
    value: Any                  # unsigned number or short string
    unit: str                   # e.g. "%", "USD", "years", "" (free text)
    text: str                   # one plain sentence a person would read, e.g. "Ten-year Treasury yields stand at 4.8%."
    kind: str                   # one of FACT_KINDS
    source: str                 # one of SOURCES
    valid_from: datetime        # sim time it became true
    event_id: Optional[str]     # the scripted event that produced it, if any
    simulated: bool             # always True in this prototype
    account_id: Optional[str]   # kind "account" only: the client these notes belong to (None otherwise). The compiler
                                # keeps an account note only for its owner, and drops notes with no owner.


class Account(TypedDict):
    id: str                     # e.g. "okafor-household"
    field: str
    name: str                   # fictional household or client name
    counterpart: str            # who the advisor meets, e.g. "Ruth and Daniel Okafor"
    profile: str                # 2-3 sentences: their situation, what they hold with us, what they care about
    exposures: list[str]        # subjects this client is exposed to (drives which changes are "material" to them)


class MarketEvent(TypedDict):
    id: str                     # e.g. "ret-rmd-age"
    field: str
    title: str                  # "RMD starting age rises"
    month: int                  # 1..6, simulated month it lands in
    facts: list[Fact]           # facts it inserts (valid_from = the event's sim time); world and client-life facts
    feedback: str               # scripted advisor feedback after a review briefed with stale settings


# ---------------------------------------------------------------------------------------------------------------
# Evaluation: scenarios and questions (FROZEN; the improver may never read the heldout split)
# ---------------------------------------------------------------------------------------------------------------
Split = Literal["tuning", "heldout"]
QuestionKind = Literal["change", "balance", "impossible"]
# change: about a material change for this client; the answer must reflect the CURRENT value.
# balance: about something that changed but does NOT apply to this client (or did not change); saying it matters is
#          a false alarm.
# impossible: nothing in the facts answers it; the honest answer says it is unknown and needs follow-up.


class Question(TypedDict):
    id: str
    text: str                   # what the client asks, in their words
    kind: str                   # QuestionKind
    key_terms: list[str]        # code-checked: an answer is correct if it contains ALL of these (case-insensitive,
                                # numbers normalised), e.g. ["18%"] or ["strike", "long beach"]
    forbidden_terms: list[str]  # an answer containing any of these is wrong (e.g. the superseded value "6%")
    fact_ids: list[str]         # facts the answer rests on (empty for impossible)


class Scenario(TypedDict):
    _id: str                    # "<field>:<split>:<seed>:m<month>"
    field: str
    split: str                  # Split
    seed: int
    month: int
    as_of: datetime             # the meeting date
    account: Account
    facts: list[Fact]           # the full fact history up to as_of (the compiler decides what is current/relevant)
    questions: list[Question]


# ---------------------------------------------------------------------------------------------------------------
# Versioned harness configuration (the self-modifiable surface)
# ---------------------------------------------------------------------------------------------------------------
ConfigKind = Literal["policy", "rules", "tools", "guardrails"]
# Keys: policy:<field>, rules:<field>, tools:<field>, guardrails:global


class Policy(TypedDict):
    recency_days: int           # only facts with valid_from within this many days of as_of (3..365)
    max_facts: int              # at most this many facts in the context (3..30)
    include_kinds: list[str]    # subset of FACT_KINDS
    section_order: list[str]    # permutation of BRIEF_SECTIONS
    likely_questions: int       # how many likely client questions the brief lists (2..8)
    prefer_exposed: bool        # rank facts about the account's exposures first


POLICY_BOUNDS = {"recency_days": (3, 365), "max_facts": (3, 30), "likely_questions": (2, 8)}
BRIEF_SECTIONS = ("what_changed", "why_it_matters", "likely_questions", "talking_points", "watch_outs")


class Rule(TypedDict):
    id: str                     # e.g. "lead-with-budget"
    text: str                   # one instruction to the drafter, <= 300 chars


RULES_CAP = 8                   # a rules version may hold at most this many rules: corrections replace, not append


class Tools(TypedDict):
    market_feed: bool
    account_notes: bool
    analyst_notes: bool


class Guardrail(TypedDict):
    id: str                     # e.g. "cite-facts", "no-advice", "no-stale-facts"
    text: str
    check: str                  # name of the code check in oracle.GUARDRAIL_CHECKS that enforces it
    enabled: bool


class ConfigVersion(TypedDict):
    _id: str                    # "<kind>:<key>@v<n>", e.g. "policy:retirement@v2"
    kind: str                   # ConfigKind
    key: str                    # field name, or "global" for guardrails
    version: int                # 1, 2, 3 ...
    body: Any                   # Policy | list[Rule] | Tools | list[Guardrail]
    rationale: str
    proposal_id: Optional[str]  # None for the seeded v1
    approval_hash: Optional[str]
    approved_by: Optional[str]  # "gate" for auto-commits, "owner:<name>" for human approvals, None for seed
    supersedes: Optional[int]   # previous version number
    restores: Optional[int]     # set on a rollback: the version whose body this one carries
    created_sim: datetime
    created_at: datetime        # wall clock


class Head(TypedDict):
    _id: str                    # "<kind>:<key>"
    version: int


class FieldConfig(TypedDict):
    """Everything the compiler/drafter need for one field, resolved from the four heads."""
    field: str
    policy: Policy
    rules: list[Rule]
    tools: Tools
    guardrails: list[Guardrail]
    versions: dict[str, int]    # {"policy": 2, "rules": 1, "tools": 1, "guardrails": 1}


# ---------------------------------------------------------------------------------------------------------------
# Context, brief, receipt
# ---------------------------------------------------------------------------------------------------------------
class Receipt(TypedDict):
    field: str
    account_id: str
    as_of: datetime
    versions: dict[str, int]    # config versions used
    config_hash: str            # sha256 of the resolved FieldConfig body
    fact_ids: list[str]         # facts put in the context, in order
    excluded_superseded: int    # how many facts were left out because a newer one replaced them
    context_tokens: int         # rough size (chars/4)


class Context(TypedDict):
    field: str
    account: Account
    as_of: datetime
    facts: list[Fact]           # current, relevant, ordered, capped
    rules: list[Rule]
    guardrails: list[Guardrail]
    policy: Policy
    receipt: Receipt


class Claim(TypedDict):
    text: str
    fact_ids: list[str]         # must be non-empty and must all be in the context (guardrail "cite-facts")


# Compliance's locked disclosures (cabinet-eval approved_language.json, version 1), by claim id. FROZEN: not a config
# surface, and no proposal can change them. The drafter model never writes a disclosure: drafter.render_markdown
# inserts every one of these word for word at the end of each brief, and the "approved-language" guardrail blocks a
# claim that repeats or resembles one (the source data's disclosures drifted, one rewording at a time, into
# "This portfolio is built to protect your capital.").
APPROVED_LANGUAGE: Mapping[str, str] = MappingProxyType({
    "AS-01": "Past performance is not indicative of future results.",
    "AS-02": "The value of investments can fall as well as rise, and you may get back less than you invested.",
})


class Brief(TypedDict):
    _id: str
    field: str
    account_id: str
    as_of: datetime
    sections: dict[str, list[Claim]]   # keyed by BRIEF_SECTIONS; likely_questions holds the questions as claims
    markdown: str
    receipt: Receipt
    model: str                  # model id, or "fake"
    config_label: str           # "champion" or "candidate:<proposal_id>" or "live"


# ---------------------------------------------------------------------------------------------------------------
# Grading (oracle) and proposals (improver / gate)
# ---------------------------------------------------------------------------------------------------------------
class QuestionResult(TypedDict):
    question_id: str
    kind: str
    answer: str                 # what the reader model answered using only the brief
    correct: bool               # code-checked against key_terms / forbidden_terms
    reason: str                 # short, code-generated


class Grade(TypedDict):
    scenario_id: str
    split: str
    accuracy: float             # correct / questions
    missed_changes: int         # change questions answered wrong
    false_alarms: int           # balance questions answered wrong
    honest_unknowns: int        # impossible questions answered "unknown"
    stale_claims: int           # claims citing a superseded fact (drift events)
    uncited_claims: int         # claims with no valid fact id
    guardrail_violations: list[str]
    context_tokens: int
    results: list[QuestionResult]


class EvalSummary(TypedDict):
    config_label: str
    split: str
    field: str
    k: int
    n_scenarios: int
    mean_accuracy: float
    worst_accuracy: float       # the worst scenario's mean over k runs
    pass_k: float               # share of scenarios where ALL k runs had accuracy >= PASS_THRESHOLD and 0 stale claims
    missed_changes: float       # mean per run
    false_alarms: float
    stale_claims: float
    uncited_claims: float       # mean per run
    guardrail_violations: float # mean per run (count of violations of the guardrails enabled for that config)
    context_tokens: float
    per_scenario: dict[str, float]


class EvalRun(TypedDict):
    """One cached evaluation in `eval_runs`, written only by the gate (trusted code).

    Unique index: (config_hash, scenario_id, run). For a summary row, `scenario_id` holds the run key
    "summary:<field>:<split>:<scenario-set hash>:<model tag>:<evaluator tag>" and `run` holds k, so a champion is never
    re-scored for the same scenarios, split, k, models and evaluator code (a hash of contracts, compiler, drafter,
    oracle and metrics: changing a prompt or a check starts a fresh cache). The improver may read rows whose `split` is "tuning" and nothing else; heldout
    rows keep no per-question failures.
    """
    _id: str                    # "<config_hash[:24]>:<run key>:k<k>"
    row: str                    # "summary"
    config_hash: str
    scenario_id: str            # the run key (see above)
    run: int                    # k
    field: str
    split: str                  # Split
    k: int
    config_label: str           # "champion" or "candidate:<proposal_id>"
    summary: EvalSummary
    failures: list[dict]        # tuning only: {scenario_id, question_id, kind, question, answer, reason}; [] for heldout
    created_at: datetime


PASS_THRESHOLD = 0.8
WIN_MARGIN = 0.05               # candidate mean accuracy must beat champion by at least this on heldout

Tier = Literal["G", "H", "X"]
ProposalStatus = Literal["pending", "evaluating", "rejected", "awaiting_owner", "committed", "stale"]


class Proposal(TypedDict):
    _id: str                    # "prop-<n>"
    field: str
    kind: str                   # ConfigKind, or a frozen surface name the improver tried to touch
    key: str
    base_version: int
    body: Any                   # the proposed NEW body for kind:key (whole body, not a patch)
    diff: list[str]             # human-readable lines: "recency_days: 90 -> 21", "rule lead-with-budget: replaced"
    rationale: str
    evidence: list[str]         # feedback ids and tuning scenario ids it cites
    filed_by: str               # "improver" or "owner:<name>"
    idem_key: str               # sha256 of (kind, key, base_version, body)
    status: str                 # ProposalStatus
    tier: Optional[str]         # set by gate.classify
    decision: Optional[str]     # plain sentence, set by the gate
    tuning: Optional[EvalSummary]
    heldout_candidate: Optional[EvalSummary]
    heldout_champion: Optional[EvalSummary]
    evaluated_versions: Optional[dict[str, int]]   # versions of all four surfaces the gate evaluated; commit and
                                                   # approve require every head unchanged (else stale)
    approval_hash: Optional[str]   # sha256 of the proposal's reviewable content; the owner approves THIS
    created_sim: datetime
    created_at: datetime


class Feedback(TypedDict):
    _id: str
    field: str
    brief_id: Optional[str]
    event_id: Optional[str]
    text: str
    sim_time: datetime


LedgerKind = Literal["seed", "event", "brief", "feedback", "proposal", "eval", "reject", "commit", "approve",
                     "rollback", "refused"]


class LedgerEntry(TypedDict):
    _id: int                    # == seq
    seq: int
    prev_hash: str
    hash: str                   # sha256 over (seq, prev_hash, kind, actor, payload, sim_time)
    kind: str                   # LedgerKind
    actor: str                  # "improver", "gate", "owner:<name>", "world", "drafter"
    payload: dict
    sim_time: datetime
    recorded_at: datetime


# Model roles -> the LLM interface picks the model id from config.
ModelRole = Literal["drafter", "reader", "improver"]
