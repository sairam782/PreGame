"""Tests for pregame.db and pregame.versions against the mongomock `db` fixture (tests/conftest.py)."""
import copy
from datetime import datetime, timezone

import pytest

from pregame import db as db_module
from pregame import ledger as ledger_module
from pregame import versions
from pregame.defaults import DEFAULT_POLICY

SIM_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------------------------------------------

def test_is_mock_true_for_mongomock_db(db):
    assert db_module.is_mock(db) is True


def test_init_db_creates_collections_and_is_idempotent(db):
    db_module.init_db(db)
    names = set(db.list_collection_names())
    for name in (
        "facts", "events", "clock", "config_versions", "config_heads", "briefs",
        "feedback", "proposals", "eval_scenarios", "eval_runs", "ledger",
    ):
        assert name in names

    db_module.init_db(db)  # idempotent: no error, no duplication
    assert db.list_collection_names().count("ledger") == 1


def test_run_txn_under_mongomock_calls_fn_with_none_session(db):
    seen = []
    result = db_module.run_txn(db, lambda session: seen.append(session) or "ok")
    assert result == "ok"
    assert seen == [None]


def test_reset_db_drops_collections(db):
    versions.seed_configs(db, SIM_TIME)
    assert db.config_heads.count_documents({}) > 0
    db_module.reset_db(db)
    assert db.config_heads.count_documents({}) == 0
    assert db.ledger.count_documents({}) == 0


# ---------------------------------------------------------------------------------------------------------------
# versions.seed_configs
# ---------------------------------------------------------------------------------------------------------------

def test_seed_creates_ten_heads(db):
    versions.seed_configs(db, SIM_TIME)
    heads = list(db.config_heads.find({}))
    assert len(heads) == 10  # 3 fields x (policy, rules, tools) + guardrails:global
    head_ids = {h["_id"] for h in heads}
    assert head_ids == {
        "policy:insurance", "rules:insurance", "tools:insurance",
        "policy:logistics", "rules:logistics", "tools:logistics",
        "policy:energy", "rules:energy", "tools:energy",
        "guardrails:global",
    }
    assert all(h["version"] == 1 for h in heads)


def test_seed_is_idempotent(db):
    versions.seed_configs(db, SIM_TIME)
    versions_before = db.config_versions.count_documents({})
    ledger_before = db.ledger.count_documents({})

    versions.seed_configs(db, SIM_TIME)  # second call must no-op

    assert db.config_versions.count_documents({}) == versions_before
    assert db.ledger.count_documents({}) == ledger_before


# ---------------------------------------------------------------------------------------------------------------
# versions.commit / StaleVersion
# ---------------------------------------------------------------------------------------------------------------

def test_commit_moves_head_and_writes_ledger(db):
    versions.seed_configs(db, SIM_TIME)
    base = versions.head(db, "policy", "insurance")

    new_body = dict(DEFAULT_POLICY, max_facts=9)
    doc = versions.commit(
        db, "policy", "insurance", base, new_body,
        rationale="test change", sim_time=SIM_TIME,
    )

    assert doc["version"] == base + 1
    assert versions.head(db, "policy", "insurance") == base + 1
    assert versions.get_version(db, "policy", "insurance")["body"]["max_facts"] == 9

    last = ledger_module.tail(db, 1)[0]
    assert last["kind"] == "commit"
    assert last["actor"] == "gate"  # default approved_by
    assert last["payload"]["kind"] == "policy"
    assert last["payload"]["key"] == "insurance"
    assert last["payload"]["version"] == base + 1


def test_commit_with_stale_base_raises_and_changes_nothing(db):
    versions.seed_configs(db, SIM_TIME)
    real_base = versions.head(db, "policy", "insurance")
    stale_base = real_base + 5  # deliberately wrong

    versions_before = db.config_versions.count_documents({})
    ledger_before = db.ledger.count_documents({})

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", "insurance", stale_base,
            dict(DEFAULT_POLICY, max_facts=99),
            rationale="should not land", sim_time=SIM_TIME,
        )

    assert versions.head(db, "policy", "insurance") == real_base
    assert db.config_versions.count_documents({}) == versions_before
    assert db.ledger.count_documents({}) == ledger_before


def test_commit_with_proposal_id_flips_status(db):
    versions.seed_configs(db, SIM_TIME)
    db.proposals.insert_one({
        "_id": "prop-1", "field": "insurance", "kind": "policy", "key": "insurance",
        "base_version": 1, "body": {}, "rationale": "r", "filed_by": "improver",
        "idem_key": "x", "status": "evaluating",
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })
    base = versions.head(db, "policy", "insurance")
    versions.commit(
        db, "policy", "insurance", base, dict(DEFAULT_POLICY, max_facts=7),
        rationale="from proposal", sim_time=SIM_TIME, proposal_id="prop-1",
    )
    prop = db.proposals.find_one({"_id": "prop-1"})
    assert prop["status"] == "committed"


# ---------------------------------------------------------------------------------------------------------------
# versions.rollback
# ---------------------------------------------------------------------------------------------------------------

def test_rollback_creates_v3_carrying_v1_body(db):
    versions.seed_configs(db, SIM_TIME)
    v1_body = versions.get_version(db, "policy", "insurance", 1)["body"]

    base = versions.head(db, "policy", "insurance")
    versions.commit(
        db, "policy", "insurance", base, dict(DEFAULT_POLICY, max_facts=20),
        rationale="bump to v2", sim_time=SIM_TIME,
    )
    assert versions.head(db, "policy", "insurance") == 2

    rolled = versions.rollback(db, "policy", "insurance", 1, "owner:alex", SIM_TIME)

    assert rolled["version"] == 3
    assert rolled["restores"] == 1
    assert rolled["body"] == v1_body
    assert rolled["approved_by"] == "owner:alex"
    assert versions.head(db, "policy", "insurance") == 3

    last = ledger_module.tail(db, 1)[0]
    assert last["kind"] == "rollback"


# ---------------------------------------------------------------------------------------------------------------
# ledger.verify
# ---------------------------------------------------------------------------------------------------------------

def test_ledger_verify_passes_then_fails_after_tamper(db):
    versions.seed_configs(db, SIM_TIME)
    versions.commit(
        db, "tools", "insurance", versions.head(db, "tools", "insurance"),
        {"market_feed": True, "account_notes": True, "analyst_notes": True},
        rationale="turn on analyst_notes", sim_time=SIM_TIME,
    )

    ok, checked, problem = ledger_module.verify(db)
    assert ok is True
    assert checked == db.ledger.count_documents({})
    assert problem == ""

    first = db.ledger.find_one({"seq": 1})
    db.ledger.update_one({"_id": first["_id"]}, {"$set": {"payload.seeded": ["tampered!"]}})

    ok2, checked2, problem2 = ledger_module.verify(db)
    assert ok2 is False
    assert problem2 != ""


# ---------------------------------------------------------------------------------------------------------------
# versions.config_hash / with_change (pure)
# ---------------------------------------------------------------------------------------------------------------

def test_config_hash_ignores_version_numbers(db):
    versions.seed_configs(db, SIM_TIME)
    cfg_before = versions.resolve_field_config(db, "insurance")
    hash_before = versions.config_hash(cfg_before)

    base = versions.head(db, "policy", "insurance")
    versions.commit(
        db, "policy", "insurance", base, dict(cfg_before["policy"]),
        rationale="no-op body, version bump only", sim_time=SIM_TIME,
    )

    cfg_after = versions.resolve_field_config(db, "insurance")
    hash_after = versions.config_hash(cfg_after)

    assert cfg_before["versions"] != cfg_after["versions"]
    assert hash_before == hash_after


def test_with_change_does_not_mutate_input(db):
    versions.seed_configs(db, SIM_TIME)
    cfg = versions.resolve_field_config(db, "insurance")
    original = copy.deepcopy(cfg)

    new_tools = dict(cfg["tools"], analyst_notes=True)
    changed = versions.with_change(cfg, "tools", new_tools)

    assert cfg == original
    assert changed["tools"]["analyst_notes"] is True
    assert cfg["tools"]["analyst_notes"] is False
    assert changed is not cfg
