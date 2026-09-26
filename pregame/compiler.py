"""Pure context compiler: FieldConfig + Account + fact history + as_of -> Context + Receipt.

No I/O, no database access. Deterministic given its inputs, per INTERFACES.md:

    drop facts after as_of; keep only the newest per (subject, relation) (count the rest as
    excluded_superseded); keep sources whose tool is on; keep include_kinds; keep within
    recency_days (account facts exempt); rank exposures first when prefer_exposed, then newest;
    cap at max_facts; build the Receipt.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from pregame.contracts import Account, Context, Fact, FieldConfig, Receipt


def compile_context(
    cfg: FieldConfig, account: Account, facts: list[Fact], as_of: datetime
) -> Context:
    policy = cfg["policy"]
    tools = cfg["tools"]

    # 1. drop facts after as_of
    current = [f for f in facts if f["valid_from"] <= as_of]

    # 2. keep only the newest per (subject, relation); count the rest as excluded_superseded
    survivors, excluded_superseded = _drop_superseded(current)

    # 3. keep sources whose tool is on
    survivors = [f for f in survivors if tools.get(f["source"], False)]

    # 4. keep include_kinds (kind "account" is kept whenever account_notes is on)
    include_kinds = set(policy["include_kinds"])
    survivors = [f for f in survivors if _kind_ok(f, include_kinds, tools)]

    # 5. keep within recency_days of as_of (account facts exempt)
    recency_days = policy["recency_days"]
    survivors = [f for f in survivors if _recent_ok(f, as_of, recency_days)]

    # 6. rank exposures first when prefer_exposed, then newest first
    exposures = set(account.get("exposures", []))
    survivors.sort(key=lambda f: f["valid_from"], reverse=True)  # newest first (stable)
    if policy["prefer_exposed"]:
        survivors.sort(key=lambda f: f["subject"] not in exposures)  # exposed first (stable)

    # 7. cap at max_facts
    kept = survivors[: policy["max_facts"]]

    # 8. receipt
    receipt: Receipt = {
        "field": cfg["field"],
        "account_id": account["id"],
        "as_of": as_of,
        "versions": dict(cfg["versions"]),
        "config_hash": _config_hash(cfg),
        "fact_ids": [f["_id"] for f in kept],
        "excluded_superseded": excluded_superseded,
        "context_tokens": _estimate_tokens(account, as_of, kept),
    }

    return {
        "field": cfg["field"],
        "account": account,
        "as_of": as_of,
        "facts": kept,
        "rules": cfg["rules"],
        "guardrails": cfg["guardrails"],
        "policy": policy,
        "receipt": receipt,
    }


def _drop_superseded(facts: list[Fact]) -> tuple[list[Fact], int]:
    """Keep only the newest fact per (subject, relation); return (survivors, excluded_count)."""
    latest: dict[tuple[str, str], Fact] = {}
    excluded = 0
    for f in facts:
        key = (f["subject"], f["relation"])
        existing = latest.get(key)
        if existing is None:
            latest[key] = f
            continue
        # newer wins; ties broken by _id for a deterministic result. The loser is superseded.
        if (f["valid_from"], f["_id"]) > (existing["valid_from"], existing["_id"]):
            latest[key] = f
        excluded += 1
    return list(latest.values()), excluded


def _kind_ok(f: Fact, include_kinds: set[str], tools: dict) -> bool:
    if f["kind"] == "account":
        return bool(tools.get("account_notes", False))
    return f["kind"] in include_kinds


def _recent_ok(f: Fact, as_of: datetime, recency_days: int) -> bool:
    if f["kind"] == "account":
        return True
    return (as_of - f["valid_from"]) <= timedelta(days=recency_days)


def _estimate_tokens(account: Account, as_of: datetime, facts: list[Fact]) -> int:
    """Rough context size: chars/4 of a plain-text rendering of the account + facts."""
    lines = [account.get("profile", ""), as_of.isoformat()]
    lines.extend(f"[{f['_id']}] {f['text']}" for f in facts)
    rendered = "\n".join(lines)
    return max(0, round(len(rendered) / 4))


def _config_hash(cfg: FieldConfig) -> str:
    """sha256 of the resolved config's bodies. Prefers pregame.versions.config_hash (the
    canonical implementation, owned by the db agent); falls back to a local hash of the same
    four bodies if that module isn't importable yet."""
    try:
        from pregame.versions import config_hash as _versions_config_hash
    except ImportError:
        return _fallback_config_hash(cfg)
    return _versions_config_hash(cfg)


def _fallback_config_hash(cfg: FieldConfig) -> str:
    body = {
        "policy": cfg.get("policy"),
        "rules": cfg.get("rules"),
        "tools": cfg.get("tools"),
        "guardrails": cfg.get("guardrails"),
    }
    payload = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
