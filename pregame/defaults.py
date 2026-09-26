"""v1 defaults for the self-modifiable harness surfaces: policy, rules, tools, guardrails.

Owned by the db agent per INTERFACES.md. v1 is deliberately *reasonable but improvable* --
narrower than it should be in a couple of places (see include_kinds below) so there is
obvious room for the harness to improve itself against held-out evidence.

These are plain dicts (contracts.py's TypedDicts document their shape; TypedDicts don't
enforce anything at runtime, so what's here is what versions.seed_configs will store as the
v1 body for each key).
"""
from __future__ import annotations

from pregame.contracts import BRIEF_SECTIONS, FIELDS, Guardrail, Policy, Rule, Tools

# ---------------------------------------------------------------------------------------------------------------
# Policy (per field, but v1 is identical across fields -- the improver differentiates them later)
# ---------------------------------------------------------------------------------------------------------------
DEFAULT_POLICY: Policy = {
    "recency_days": 180,
    "max_facts": 6,
    # Deliberately WITHOUT "regulation" and "disruption": v1 undercovers exactly the kinds of
    # facts that world events (tax rules, market shocks) tend to produce, so the first few proposals have real signal
    # to work with.
    "include_kinds": ["price", "competitor", "demand", "account"],
    "section_order": list(BRIEF_SECTIONS),
    "likely_questions": 3,
    "prefer_exposed": False,
}

# ---------------------------------------------------------------------------------------------------------------
# Drafting rules (per field): 2-3 generic, sensible starting instructions each.
# ---------------------------------------------------------------------------------------------------------------
_GENERIC_RULES: list[Rule] = [
    {
        "id": "lead-with-change",
        "text": "Open with the change most likely to come up with this household.",
    },
    {
        "id": "cite-every-number",
        "text": "Every number must cite a fact. Never state a figure that isn't in the context.",
    },
    {
        "id": "plain-language",
        "text": "Write for an advisor about to walk into a client review: short sentences, no jargon, no "
                "hedging.",
    },
]

DEFAULT_RULES: dict[str, list[Rule]] = {field: [dict(rule) for rule in _GENERIC_RULES] for field in FIELDS}

# ---------------------------------------------------------------------------------------------------------------
# Tools (per field): which fact sources the compiler may draw from.
# ---------------------------------------------------------------------------------------------------------------
DEFAULT_TOOLS: Tools = {
    "market_feed": True,
    "account_notes": True,
    # Off by default: broader coverage but some of it is wrong (contracts.py's SOURCES doc).
    # Switching it on is a tier-H (human-gated) change.
    "analyst_notes": False,
}

# ---------------------------------------------------------------------------------------------------------------
# Guardrails (global): the checks oracle.GUARDRAIL_CHECKS enforces against every brief.
# ---------------------------------------------------------------------------------------------------------------
DEFAULT_GUARDRAILS: list[Guardrail] = [
    {
        "id": "cite-facts",
        "text": "Every claim must cite at least one fact id that is present in the context.",
        "check": "cite-facts",
        "enabled": True,
    },
    {
        "id": "no-stale-facts",
        "text": "A claim may not cite a fact that a newer fact has superseded.",
        "check": "no-stale-facts",
        "enabled": True,
    },
    {
        "id": "no-advice",
        "text": "The brief prepares the advisor with facts and questions; it never gives the client investment "
                "advice (no buy or sell instructions, no promised returns).",
        "check": "no-advice",
        "enabled": True,
    },
    {
        "id": "approved-language",
        "text": "Disclosures are added by code in Compliance's approved wording; a claim never rewords one or "
                "promises an outcome (protected capital, guaranteed returns, no risk).",
        "check": "approved-language",
        "enabled": True,
    },
]
