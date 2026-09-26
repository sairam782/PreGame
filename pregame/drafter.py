"""Brief drafter: Context -> Brief, either via the live drafter model or a deterministic fake path.

Live: the system prompt pins the drafter's job plus the rules and guardrails VERBATIM (never
summarised), and the fixed instruction to use only the given facts, cite fact ids on every claim,
and flag any likely question the facts can't answer as needing follow-up. The user prompt carries
the account profile, as_of, the facts as "[fact_id] text" lines, the section order and the
likely_questions count. The model must return JSON {"sections": {section: [{"text","fact_ids"}]}}.

The live response is validated, not silently coerced: every configured section must be present as
a list, every claim needs non-empty text and a non-empty list of fact ids that all exist in the
context, and likely_questions must have the configured count (fewer only excused by the context
itself having fewer facts). An invalid response gets exactly one corrective retry through
`llm.complete_json` that spells out what was wrong. If it's still invalid, we salvage: drop only
the claims that cite no valid fact id (a claim never survives with fact_ids == []), and raise
`LLMError` if a required section — or the whole brief — ends up with no claims at all. The number
of claims dropped this way is recorded on the brief as `dropped_claims` for the demo to surface.

Fake (llm.is_fake): deterministic claims built straight from the facts already in the context, so
this path is fully testable offline.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from pregame.contracts import BRIEF_SECTIONS, Brief, Claim, Context
from pregame.llm import LLMError

GUARDRAIL_INSTRUCTION = (
    "Use only the facts given; every claim cites fact ids; if a likely client question cannot be "
    "answered from the facts, say it needs follow-up."
)

SECTION_TITLES = {
    "what_changed": "What Changed",
    "why_it_matters": "Why It Matters",
    "likely_questions": "Likely Questions",
    "talking_points": "Talking Points",
    "watch_outs": "Watch Outs",
}


def draft_brief(ctx: Context, llm, config_label: str = "live") -> Brief:
    dropped_claims = 0
    if llm.is_fake:
        sections = _draft_fake(ctx)
        model = "fake"
    else:
        sections, dropped_claims = _draft_live(ctx, llm)
        model = llm.model_id("drafter")

    markdown = render_markdown(sections, ctx)

    brief: Brief = {
        "_id": uuid.uuid4().hex,
        "field": ctx["field"],
        "account_id": ctx["account"]["id"],
        "as_of": ctx["as_of"],
        "sections": sections,
        "markdown": markdown,
        "receipt": ctx["receipt"],
        "model": model,
        "config_label": config_label,
        "dropped_claims": dropped_claims,
    }
    return brief


# -- live path --------------------------------------------------------------------------------
def _draft_live(ctx: Context, llm) -> tuple[dict[str, list[Claim]], int]:
    system = _build_system_prompt(ctx)
    prompt = _build_user_prompt(ctx)

    data = llm.complete_json("drafter", system, prompt, max_tokens=2000)
    problems = _validate_sections(data, ctx)

    if problems:
        retry_prompt = _build_semantic_retry_prompt(ctx, problems, data)
        data = llm.complete_json("drafter", system, retry_prompt, max_tokens=2000)
        problems = _validate_sections(data, ctx)

    if not problems:
        return _sections_from_valid_data(data, ctx), 0

    # Still invalid after one corrective retry: salvage what we can rather than trust it whole.
    sections, dropped = _salvage_sections(data, ctx)
    for name in ctx["policy"]["section_order"]:
        if not sections.get(name):
            raise LLMError(
                f"drafter: section '{name}' has no usable claims after validation and one "
                "corrective retry (problems: " + "; ".join(problems) + ")"
            )
    if not any(sections.values()):
        raise LLMError(
            "drafter: brief has no valid claims after validation and one corrective retry "
            "(problems: " + "; ".join(problems) + ")"
        )

    expected_questions = min(ctx["policy"]["likely_questions"], len(ctx["facts"]))
    actual_questions = len(sections.get("likely_questions", []))
    if actual_questions != expected_questions:
        raise LLMError(
            f"drafter: section 'likely_questions' has {actual_questions} usable claims after "
            f"salvage; expected exactly {expected_questions} (problems: "
            + "; ".join(problems)
            + ")"
        )

    return sections, dropped


def _validate_sections(data: Any, ctx: Context) -> list[str]:
    """Return a list of human-readable problems with `data`; empty means valid."""
    if not isinstance(data, dict):
        return ["Top-level response is not a JSON object."]
    sections = data.get("sections")
    if not isinstance(sections, dict):
        return ["Missing or invalid top-level 'sections' object."]

    valid_ids = {f["_id"] for f in ctx["facts"]}
    policy = ctx["policy"]
    problems: list[str] = []

    for name in policy["section_order"]:
        if name not in sections:
            problems.append(f"Missing section '{name}'.")
            continue
        items = sections[name]
        if not isinstance(items, list):
            problems.append(f"Section '{name}' must be a list.")
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"Item {i} in section '{name}' is not an object.")
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                problems.append(f"Item {i} in section '{name}' has empty or missing text.")
            fact_ids = item.get("fact_ids")
            if not isinstance(fact_ids, list) or not fact_ids:
                problems.append(
                    f"Item {i} in section '{name}' has no fact_ids; every claim must cite at "
                    "least one fact id."
                )
            else:
                bad_ids = [fid for fid in fact_ids if fid not in valid_ids]
                if bad_ids:
                    problems.append(
                        f"Item {i} in section '{name}' cites unknown fact id(s): "
                        f"{', '.join(map(str, bad_ids))}."
                    )

    lq_items = sections.get("likely_questions")
    if isinstance(lq_items, list):
        expected = min(policy["likely_questions"], len(ctx["facts"]))
        if len(lq_items) != expected:
            problems.append(
                f"Section 'likely_questions' has {len(lq_items)} items; expected exactly "
                f"{expected} (min of the configured count and the number of available facts)."
            )

    return problems


def _sections_from_valid_data(data: dict, ctx: Context) -> dict[str, list[Claim]]:
    """Build the final sections dict from a response that already passed `_validate_sections`."""
    raw_sections = data["sections"]
    sections: dict[str, list[Claim]] = {}
    for name in ctx["policy"]["section_order"]:
        claims: list[Claim] = []
        for item in raw_sections.get(name, []):
            claims.append({"text": str(item["text"]).strip(), "fact_ids": list(item["fact_ids"])})
        sections[name] = claims
    return sections


def _salvage_sections(data: Any, ctx: Context) -> tuple[dict[str, list[Claim]], int]:
    """Best-effort recovery from a response that failed validation twice: drop only the claims
    that end up citing no valid fact id. Never keeps a claim with fact_ids == []."""
    valid_ids = {f["_id"] for f in ctx["facts"]}
    raw_sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(raw_sections, dict):
        raw_sections = {}

    sections: dict[str, list[Claim]] = {}
    dropped = 0
    for name in ctx["policy"]["section_order"]:
        items = raw_sections.get(name)
        claims: list[Claim] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    dropped += 1
                    continue
                text = str(item.get("text") or "").strip()
                fact_ids = [fid for fid in (item.get("fact_ids") or []) if fid in valid_ids]
                if not text or not fact_ids:
                    dropped += 1
                    continue
                claims.append({"text": text, "fact_ids": fact_ids})
        sections[name] = claims
    return sections, dropped


def _build_semantic_retry_prompt(ctx: Context, problems: list[str], previous_response: Any) -> str:
    try:
        previous_json = json.dumps(previous_response)
    except TypeError:
        previous_json = repr(previous_response)
    problems_block = "\n".join(f"- {p}" for p in problems)
    return (
        _build_user_prompt(ctx)
        + "\n\nYour previous answer was invalid for these reasons:\n"
        + problems_block
        + "\n\nYour previous answer was:\n"
        + previous_json
        + "\n\nReturn ONLY one corrected JSON object of the exact same shape, fixing every "
        "problem listed above. Every claim must cite at least one fact id that appears in the "
        "Facts list above — never invent a fact id."
    )


def _build_system_prompt(ctx: Context) -> str:
    rules = ctx["rules"]
    guardrails = [g for g in ctx["guardrails"] if g.get("enabled", True)]
    rules_block = "\n".join(f"- [{r['id']}] {r['text']}" for r in rules) or "(none)"
    guardrails_block = "\n".join(f"- [{g['id']}] {g['text']}" for g in guardrails) or "(none)"

    return (
        "You are the drafter for Pregame: you write the prep brief an account manager reads right "
        "before a client call, for an account in the "
        f"{ctx['field']} industry. Write clear, specific claims a busy person can skim.\n\n"
        "Rules (verbatim — follow exactly, do not paraphrase away from them):\n"
        f"{rules_block}\n\n"
        "Guardrails (verbatim — follow exactly):\n"
        f"{guardrails_block}\n\n"
        f"{GUARDRAIL_INSTRUCTION}\n\n"
        "Respond with ONLY one JSON object, no code fences, no commentary, of the exact shape "
        '{"sections": {"<section name>": [{"text": "...", "fact_ids": ["..."]}]}}.'
    )


def _build_user_prompt(ctx: Context) -> str:
    account = ctx["account"]
    policy = ctx["policy"]
    fact_lines = "\n".join(f"[{f['_id']}] {f['text']}" for f in ctx["facts"]) or "(no facts in context)"

    return (
        f"Account: {account.get('name')} ({account.get('id')})\n"
        f"Counterpart: {account.get('counterpart', '')}\n"
        f"Profile: {account.get('profile', '')}\n"
        f"As of: {ctx['as_of'].isoformat()}\n\n"
        "Facts:\n"
        f"{fact_lines}\n\n"
        f"Section order (produce exactly these sections, in this order): {', '.join(policy['section_order'])}\n"
        f"likely_questions: produce exactly {min(policy['likely_questions'], len(ctx['facts']))} items in that section.\n"
    )


# -- fake path --------------------------------------------------------------------------------
def _draft_fake(ctx: Context) -> dict[str, list[Claim]]:
    facts = list(ctx["facts"])
    account = ctx["account"]
    policy = ctx["policy"]
    exposures = set(account.get("exposures", []))

    facts_newest_first = sorted(facts, key=lambda f: f["valid_from"], reverse=True)
    exposed_newest_first = [f for f in facts_newest_first if f["subject"] in exposures]

    what_changed = [{"text": f["text"], "fact_ids": [f["_id"]]} for f in facts_newest_first]

    why_it_matters = [
        {"text": f"For {account.get('name')}, {f['text']}", "fact_ids": [f["_id"]]}
        for f in exposed_newest_first
    ]

    n_questions = policy["likely_questions"]
    likely_questions = [
        {"text": f"What's the latest on {f['subject']}? {f['text']}", "fact_ids": [f["_id"]]}
        for f in facts[:n_questions]
    ]

    talking_point_source = exposed_newest_first[:2] or facts_newest_first[:2]
    talking_points = [
        {"text": f"Bring up: {f['text']}", "fact_ids": [f["_id"]]} for f in talking_point_source
    ]

    watch_out_source = [f for f in facts_newest_first if f["kind"] in ("disruption", "regulation")][:2]
    if not watch_out_source:
        watch_out_source = facts_newest_first[:1]
    watch_outs = [
        {"text": f"Watch out: {f['text']}", "fact_ids": [f["_id"]]} for f in watch_out_source
    ]

    by_name = {
        "what_changed": what_changed,
        "why_it_matters": why_it_matters,
        "likely_questions": likely_questions,
        "talking_points": talking_points,
        "watch_outs": watch_outs,
    }
    return {name: by_name.get(name, []) for name in policy["section_order"]}


# -- markdown ----------------------------------------------------------------------------------
def render_markdown(sections: dict[str, list[Claim]], ctx: Context) -> str:
    account = ctx["account"]
    lines = [
        f"# Prep Brief: {account.get('name')} ({ctx['field']})",
        f"As of: {ctx['as_of'].isoformat()}",
        "",
    ]
    for name in ctx["policy"]["section_order"]:
        claims = sections.get(name, [])
        lines.append(f"## {SECTION_TITLES.get(name, name)}")
        if not claims:
            lines.append("_(none)_")
        for claim in claims:
            cites = ", ".join(claim.get("fact_ids", []))
            lines.append(f"- {claim['text']} (cites: {cites})")
        lines.append("")
    return "\n".join(lines)
