"""Tests for pregame.db and pregame.versions against the mongomock `db` fixture (tests/conftest.py)."""
import copy
from datetime import datetime, timezone

import pytest

from pregame import db as db_module
from pregame import ledger as ledger_module
from pregame import versions
from pregame.contracts import FIELDS
from pregame.defaults import DEFAULT_POLICY

SIM_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)

# The field these tests exercise most of the time. Derived from pregame.contracts.FIELDS (not
# hard-coded) so a field-id rename there (e.g. the insurance/logistics/energy -> financial-advisor
# rename) doesn't require touching every test -- only the tests that care about ALL fields
# (test_seed_creates_ten_heads) reference FIELDS directly.
FIELD = FIELDS[0]


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
    expected = {f"{kind}:{field}" for field in FIELDS for kind in ("policy", "rules", "tools")}
    expected.add("guardrails:global")
    assert head_ids == expected
    assert all(h["version"] == 1 for h in heads)


def test_seed_is_idempotent(db):
    versions.seed_configs(db, SIM_TIME)
    versions_before = db.config_versions.count_documents({})
    ledger_before = db.ledger.count_documents({})

    versions.seed_configs(db, SIM_TIME)  # second call must no-op

    assert db.config_versions.count_documents({}) == versions_before
    assert db.ledger.count_documents({}) == ledger_before


def test_seed_configs_raises_on_partial_seed(db):
    """Regression for CHECK_HDY-28 (b1c11c1): a database with only one of the ten expected heads
    (e.g. an interrupted or non-transactional prior seed attempt) must not be silently treated
    as fully seeded -- it should refuse loudly instead of leaving nine fields unconfigured
    forever."""
    db.config_heads.insert_one({"_id": f"policy:{FIELD}", "version": 1})
    db.config_versions.insert_one({
        "_id": f"policy:{FIELD}@v1", "kind": "policy", "key": FIELD, "version": 1,
        "body": dict(DEFAULT_POLICY), "rationale": "seed", "proposal_id": None,
        "approval_hash": None, "approved_by": None, "supersedes": None, "restores": None,
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })

    with pytest.raises(versions.SeedInconsistent):
        versions.seed_configs(db, SIM_TIME)

    # Refusing loudly, not "helpfully" seeding the other nine keys on top of the partial state.
    assert db.config_heads.count_documents({}) == 1
    assert db.config_versions.count_documents({}) == 1


def test_seed_configs_accepts_head_advanced_by_a_legitimate_commit(db):
    """Regression for CHECK_HDY-28 (1cbab1d): after seeding, a legitimate commit legitimately
    advances a head past v1 (e.g. the demo turns a tool on). Re-running seed_configs afterwards
    must still recognize the database as fully seeded -- not treat the advanced head as
    "partial" -- since v1 still exists (insert-only) and the head still points at a real,
    consistent version doc."""
    versions.seed_configs(db, SIM_TIME)
    versions.commit(
        db, "tools", FIELD, versions.head(db, "tools", FIELD),
        {"market_feed": True, "account_notes": True, "analyst_notes": True},
        rationale="turn on analyst_notes", sim_time=SIM_TIME,
    )
    assert versions.head(db, "tools", FIELD) == 2

    versions_before = db.config_versions.count_documents({})
    ledger_before = db.ledger.count_documents({})

    versions.seed_configs(db, SIM_TIME)  # must no-op, not raise SeedInconsistent

    assert db.config_versions.count_documents({}) == versions_before
    assert db.ledger.count_documents({}) == ledger_before
    assert versions.head(db, "tools", FIELD) == 2  # unchanged


def test_seed_configs_raises_when_head_points_at_missing_version(db):
    """Regression for CHECK_HDY-28 (1cbab1d): a head advanced to v2 with no v2 document behind it
    (a corrupted/inconsistent state, unlike a legitimate commit) must be treated as
    inconsistent, not accepted as "fully seeded"."""
    versions.seed_configs(db, SIM_TIME)
    db.config_heads.update_one({"_id": f"policy:{FIELD}"}, {"$set": {"version": 2}})

    with pytest.raises(versions.SeedInconsistent):
        versions.seed_configs(db, SIM_TIME)


def test_seed_configs_raises_when_seed_ledger_entry_missing(db):
    """Regression for CHECK_HDY-28 (1cbab1d): all ten heads and v1 version docs present and
    correct, but the 'seed' ledger receipt is missing (e.g. it was pruned, or the mongomock seed
    failed after writing config docs but before the ledger append). Must raise, not no-op."""
    versions.seed_configs(db, SIM_TIME)
    assert db.ledger.count_documents({"kind": "seed"}) == 1

    db.ledger.delete_many({"kind": "seed"})

    with pytest.raises(versions.SeedInconsistent):
        versions.seed_configs(db, SIM_TIME)


def test_seed_configs_survives_transaction_retry_without_duplicate_ledger_keys(db, monkeypatch):
    """Regression for CHECK_HDY-28 (b1c11c1): with_transaction may invoke its callback more than
    once on Atlas (an earlier attempt's writes are discarded when the transaction retries). The
    seed ledger's "seeded" list must reflect exactly the 10 expected keys once, not double up
    across retries. Simulated with a stub run_txn that calls the callback twice, wiping the
    database in between to stand in for the first (discarded) attempt's aborted writes."""

    def fake_run_txn(db_arg, fn):
        fn(None)                      # first attempt: writes, as if about to be retried
        db_module.reset_db(db_arg)    # simulate the transaction aborting: discard those writes
        return fn(None)               # second attempt: the one that "really" commits

    monkeypatch.setattr(versions, "run_txn", fake_run_txn)

    versions.seed_configs(db, SIM_TIME)

    assert db.config_heads.count_documents({}) == 10
    assert db.ledger.count_documents({}) == 1  # exactly one seed entry, not one per attempt

    seeded = ledger_module.tail(db, 1)[0]["payload"]["seeded"]
    assert len(seeded) == 10
    assert len(set(seeded)) == 10  # no duplicates from the retried attempt


# ---------------------------------------------------------------------------------------------------------------
# versions.commit / StaleVersion
# ---------------------------------------------------------------------------------------------------------------

def test_commit_moves_head_and_writes_ledger(db):
    versions.seed_configs(db, SIM_TIME)
    base = versions.head(db, "policy", FIELD)

    new_body = dict(DEFAULT_POLICY, max_facts=9)
    doc = versions.commit(
        db, "policy", FIELD, base, new_body,
        rationale="test change", sim_time=SIM_TIME,
    )

    assert doc["version"] == base + 1
    assert versions.head(db, "policy", FIELD) == base + 1
    assert versions.get_version(db, "policy", FIELD)["body"]["max_facts"] == 9

    last = ledger_module.tail(db, 1)[0]
    assert last["kind"] == "commit"
    assert last["actor"] == "gate"  # default approved_by
    assert last["payload"]["kind"] == "policy"
    assert last["payload"]["key"] == FIELD
    assert last["payload"]["version"] == base + 1


def test_commit_with_stale_base_raises_and_changes_nothing(db):
    versions.seed_configs(db, SIM_TIME)
    real_base = versions.head(db, "policy", FIELD)
    stale_base = real_base + 5  # deliberately wrong

    versions_before = db.config_versions.count_documents({})
    ledger_before = db.ledger.count_documents({})

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", FIELD, stale_base,
            dict(DEFAULT_POLICY, max_facts=99),
            rationale="should not land", sim_time=SIM_TIME,
        )

    assert versions.head(db, "policy", FIELD) == real_base
    assert db.config_versions.count_documents({}) == versions_before
    assert db.ledger.count_documents({}) == ledger_before


def test_commit_with_proposal_id_flips_status(db):
    versions.seed_configs(db, SIM_TIME)
    db.proposals.insert_one({
        "_id": "prop-1", "field": FIELD, "kind": "policy", "key": FIELD,
        "base_version": 1, "body": {}, "rationale": "r", "filed_by": "improver",
        "idem_key": "x", "status": "evaluating",
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })
    base = versions.head(db, "policy", FIELD)
    versions.commit(
        db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=7),
        rationale="from proposal", sim_time=SIM_TIME, proposal_id="prop-1",
    )
    prop = db.proposals.find_one({"_id": "prop-1"})
    assert prop["status"] == "committed"


def _snapshot(db):
    """All rows from every collection commit() can touch, for before/after equality checks."""
    return {
        "heads": list(db.config_heads.find({})),
        "versions": list(db.config_versions.find({})),
        "proposals": list(db.proposals.find({})),
        "ledger": list(db.ledger.find({})),
    }


def test_commit_with_orphan_target_version_raises_and_changes_nothing(db):
    """Regression for CHECK_HDY-28 blocking finding 1: a pre-existing (orphan) version doc at
    base_version + 1, with the head still at base_version, must not advance the head. Before the
    fix, commit() bumped the head first and only discovered the collision on the version insert,
    leaving the head bumped under mongomock (no rollback) despite raising StaleVersion."""
    versions.seed_configs(db, SIM_TIME)
    base = versions.head(db, "policy", FIELD)

    orphan_id = f"policy:{FIELD}@v{base + 1}"
    db.config_versions.insert_one({
        "_id": orphan_id, "kind": "policy", "key": FIELD, "version": base + 1,
        "body": {"orphan": True}, "rationale": "orphan, not a real commit", "proposal_id": None,
        "approval_hash": None, "approved_by": None, "supersedes": base, "restores": None,
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })

    before = _snapshot(db)

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=42),
            rationale="should be blocked by the orphan collision", sim_time=SIM_TIME,
        )

    assert versions.head(db, "policy", FIELD) == base
    assert _snapshot(db) == before


def test_commit_with_missing_proposal_raises_and_changes_nothing(db):
    versions.seed_configs(db, SIM_TIME)
    base = versions.head(db, "policy", FIELD)
    before = _snapshot(db)

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=5),
            rationale="no such proposal", sim_time=SIM_TIME,
            proposal_id="prop-does-not-exist",
        )

    assert versions.head(db, "policy", FIELD) == base
    assert _snapshot(db) == before


def test_commit_with_ineligible_proposal_status_raises_and_changes_nothing(db):
    versions.seed_configs(db, SIM_TIME)
    db.proposals.insert_one({
        "_id": "prop-rejected", "field": FIELD, "kind": "policy", "key": FIELD,
        "base_version": 1, "body": {}, "rationale": "r", "filed_by": "improver",
        "idem_key": "y", "status": "rejected",
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })
    base = versions.head(db, "policy", FIELD)
    before = _snapshot(db)

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=5),
            rationale="proposal already rejected", sim_time=SIM_TIME,
            proposal_id="prop-rejected",
        )

    assert versions.head(db, "policy", FIELD) == base
    assert _snapshot(db) == before
    assert db.proposals.find_one({"_id": "prop-rejected"})["status"] == "rejected"


def test_commit_proposal_update_losing_its_race_rolls_back_everything(db, monkeypatch):
    """Regression for CHECK_HDY-28 (5853a1a): the pre-check sees an eligible proposal (so the
    version doc and ledger entry DO get written this invocation), but the final conditional
    proposal update reports matched_count == 0 -- e.g. a concurrent commit claimed the proposal
    after our pre-check passed. Before this fix, the mongomock compensation restored only
    config_heads, leaving an orphaned config_versions doc and ledger entry behind."""
    versions.seed_configs(db, SIM_TIME)
    db.proposals.insert_one({
        "_id": "prop-race", "field": FIELD, "kind": "policy", "key": FIELD,
        "base_version": 1, "body": {}, "rationale": "r", "filed_by": "improver",
        "idem_key": "z", "status": "evaluating",
        "created_sim": SIM_TIME, "created_at": SIM_TIME,
    })
    base = versions.head(db, "policy", FIELD)
    before = _snapshot(db)

    class _FakeResult:
        matched_count = 0

    def _fake_update_one(*args, **kwargs):
        # Simulate the update losing its race: report no match without touching the document.
        return _FakeResult()

    monkeypatch.setattr(db.proposals, "update_one", _fake_update_one)

    with pytest.raises(versions.StaleVersion):
        versions.commit(
            db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=11),
            rationale="proposal update loses its race", sim_time=SIM_TIME,
            proposal_id="prop-race",
        )

    assert versions.head(db, "policy", FIELD) == base
    assert _snapshot(db) == before


def _all_heads(db):
    return {f"{kind}:{key}": versions.head(db, kind, key)
            for kind, key in (("policy", FIELD), ("rules", FIELD), ("tools", FIELD), ("guardrails", "global"))}


def test_commit_with_expected_heads_all_current_commits(db):
    versions.seed_configs(db, SIM_TIME)
    doc = versions.commit(
        db, "policy", FIELD, 1, dict(DEFAULT_POLICY, max_facts=9),
        rationale="evaluated on these four heads", sim_time=SIM_TIME, expected_heads=_all_heads(db),
    )
    assert doc["version"] == 2 and versions.head(db, "policy", FIELD) == 2


def test_commit_when_another_surface_moved_since_evaluation_raises_and_changes_nothing(db):
    """The gate evaluated a policy change on tools v1; tools moved to v2 before the commit. That combination was
    never evaluated, so the fence refuses it inside the same transaction as the write."""
    versions.seed_configs(db, SIM_TIME)
    evaluated = _all_heads(db)
    versions.commit(
        db, "tools", FIELD, 1, {"market_feed": True, "account_notes": True, "analyst_notes": True},
        rationale="a tools change lands meanwhile", sim_time=SIM_TIME,
    )
    before = _snapshot(db)

    with pytest.raises(versions.StaleVersion, match=f"tools:{FIELD} is at v2, not v1"):
        versions.commit(
            db, "policy", FIELD, 1, dict(DEFAULT_POLICY, max_facts=9),
            rationale="evaluated on tools v1", sim_time=SIM_TIME, expected_heads=evaluated,
        )

    assert versions.head(db, "policy", FIELD) == 1
    assert _snapshot(db) == before


# ---------------------------------------------------------------------------------------------------------------
# versions.rollback
# ---------------------------------------------------------------------------------------------------------------

def test_rollback_creates_v3_carrying_v1_body(db):
    versions.seed_configs(db, SIM_TIME)
    v1_body = versions.get_version(db, "policy", FIELD, 1)["body"]

    base = versions.head(db, "policy", FIELD)
    versions.commit(
        db, "policy", FIELD, base, dict(DEFAULT_POLICY, max_facts=20),
        rationale="bump to v2", sim_time=SIM_TIME,
    )
    assert versions.head(db, "policy", FIELD) == 2

    rolled = versions.rollback(db, "policy", FIELD, 1, "owner:alex", SIM_TIME)

    assert rolled["version"] == 3
    assert rolled["restores"] == 1
    assert rolled["body"] == v1_body
    assert rolled["approved_by"] == "owner:alex"
    assert versions.head(db, "policy", FIELD) == 3

    last = ledger_module.tail(db, 1)[0]
    assert last["kind"] == "rollback"


# ---------------------------------------------------------------------------------------------------------------
# ledger.verify
# ---------------------------------------------------------------------------------------------------------------

def test_ledger_verify_passes_then_fails_after_tamper(db):
    versions.seed_configs(db, SIM_TIME)
    versions.commit(
        db, "tools", FIELD, versions.head(db, "tools", FIELD),
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


def test_ledger_verify_rejects_id_seq_mismatch(db):
    """Regression for CHECK_HDY-28 finding 3: a structurally-valid entry (correct hash, correct
    seq field) stored under the wrong _id must fail verification. Before the fix, verify() only
    checked hash/seq-ordering/prev_hash and would happily accept this as (True, 1, "")."""
    versions.seed_configs(db, SIM_TIME)
    assert db.ledger.count_documents({}) == 1  # just the "seed" entry, seq == 1

    entry = db.ledger.find_one({"seq": 1})
    db.ledger.delete_one({"_id": entry["_id"]})
    tampered = dict(entry)
    tampered["_id"] = 99  # seq (and hash) left untouched -- only the storage key changes
    db.ledger.insert_one(tampered)

    ok, checked, problem = ledger_module.verify(db)
    assert ok is False
    assert problem != ""
    assert "_id" in problem


# ---------------------------------------------------------------------------------------------------------------
# versions.config_hash / with_change (pure)
# ---------------------------------------------------------------------------------------------------------------

def test_config_hash_ignores_version_numbers(db):
    versions.seed_configs(db, SIM_TIME)
    cfg_before = versions.resolve_field_config(db, FIELD)
    hash_before = versions.config_hash(cfg_before)

    base = versions.head(db, "policy", FIELD)
    versions.commit(
        db, "policy", FIELD, base, dict(cfg_before["policy"]),
        rationale="no-op body, version bump only", sim_time=SIM_TIME,
    )

    cfg_after = versions.resolve_field_config(db, FIELD)
    hash_after = versions.config_hash(cfg_after)

    assert cfg_before["versions"] != cfg_after["versions"]
    assert hash_before == hash_after


def test_with_change_does_not_mutate_input(db):
    versions.seed_configs(db, SIM_TIME)
    cfg = versions.resolve_field_config(db, FIELD)
    original = copy.deepcopy(cfg)

    new_tools = dict(cfg["tools"], analyst_notes=True)
    changed = versions.with_change(cfg, "tools", new_tools)

    assert cfg == original
    assert changed["tools"]["analyst_notes"] is True
    assert cfg["tools"]["analyst_notes"] is False
    assert changed is not cfg


def test_seed_configs_raises_when_head_target_claims_another_version(db):
    """Codex HDY-28 (6527cae): a head at v1 whose target document claims version 99 is inconsistent."""
    db_module.init_db(db)
    versions.seed_configs(db, SIM_TIME)
    head_id = f"policy:{FIELD}"
    db.config_versions.update_one({"_id": f"{head_id}@v1"}, {"$set": {"version": 99}})
    with pytest.raises(versions.SeedInconsistent):
        versions.seed_configs(db, SIM_TIME)


def test_ledger_verifies_times_appended_in_another_zone(db):
    """A driver reads dates back in UTC; the ledger must hash the UTC form or verify() fails after the round trip."""
    from datetime import timedelta

    from pregame import ledger

    when = datetime(2026, 4, 1, 9, 30, 15, 123456, tzinfo=timezone(timedelta(hours=-4)))
    ledger.append(db, "seed", "test", {"note": "zone"}, when)
    ok, n, msg = ledger.verify(db)
    assert ok and n == 1, msg
