"""Brief drafter: Context -> Brief, either via the live drafter model or a deterministic fake path.

Live: the system prompt pins the drafter's job plus the rules and guardrails VERBATIM (never
summarised), and the fixed instruction to use only the given facts, cite fact ids on every claim,
and flag any likely question the facts can't answer as needing follow-up. The user prompt carries
the account profile, as_of, the facts as "[fact_id] text" lines, the section order and the
likely_questions count. The model must return JSON {"sections": {section: [{"text","fact_ids"}]}}.

Fake (llm.is_fake): deterministic claims built straight from the facts already in the context, so
this path is fully testable offline.
"""
from __future__ import annotations

import uuid
from typing import Any

from pregame.contracts import BRIEF_SECTIONS, Brief, Claim, Context

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
    if llm.is_fake:
        sections = _draft_fake(ctx)
        model = "fake"
    else:
        sections = _draft_live(ctx, llm)
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
    }
    return brief


# -- live path --------------------------------------------------------------------------------
def _draft_live(ctx: Context, llm) -> dict[str, list[Claim]]:
    system = _build_system_prompt(ctx)
    prompt = _build_user_prompt(ctx)
    data = llm.complete_json("drafter", system, prompt, max_tokens=2000)

    raw_sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(raw_sections, dict):
        raw_sections = {}

    valid_ids = {f["_id"] for f in ctx["facts"]}
    sections: dict[str, list[Claim]] = {}
    for name in ctx["policy"]["section_order"]:
        claims: list[Claim] = []
        for item in raw_sections.get(name) or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            fact_ids = [fid for fid in (item.get("fact_ids") or []) if fid in valid_ids]
            claims.append({"text": text, "fact_ids": fact_ids})
        sections[name] = claims
    return sections


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
        f"likely_questions: produce exactly {policy['likely_questions']} items in that section.\n"
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
