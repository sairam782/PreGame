"""Tests against the real MongoDB Atlas cluster: the things mongomock cannot prove.

Skipped unless PREGAME_ATLAS_TESTS=1 (they need MONGODB_URI in .env and network). They use their own scratch database,
`pregame_atlas_test`, and drop its collections afterwards; they never touch the demo databases. From the Codex tests
audit (audits/parallel-1315/tests-codex.md): transactions, validators, index definitions and BSON datetimes are only
exercised on Atlas.

    PREGAME_ATLAS_TESTS=1 .venv/Scripts/python -m pytest -q tests/test_atlas.py
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from pymongo.errors import DuplicateKeyError, WriteError

pytestmark = pytest.mark.skipif(os.environ.get("PREGAME_ATLAS_TESTS") != "1",
                                reason="Atlas tests run only with PREGAME_ATLAS_TESTS=1")

SCRATCH_DB = "pregame_atlas_test"
SIM = datetime(2026, 4, 1, tzinfo=timezone.utc)


@pytest.fixture
def adb():
    from pregame import db as dbmod

    database = dbmod.get_db().client[SCRATCH_DB]
    assert not dbmod.is_mock(database)
    dbmod.reset_db(database)
    dbmod.init_db(database)
    yield database
    dbmod.reset_db(database)


def test_unique_indexes_exist_and_reject_duplicates(adb):
    keys = {name: [tuple(k) for k in spec["key"]] for name, spec in adb.eval_runs.index_information().items()
            if spec.get("unique")}
    assert [("config_hash", 1), ("scenario_id", 1), ("run", 1)] in keys.values()
    assert any(spec.get("unique") and [tuple(k) for k in spec["key"]] == [("idem_key", 1)]
               for spec in adb.proposals.index_information().values())

    row = {"config_hash": "h", "scenario_id": "s", "run": 1}
    adb.eval_runs.insert_one(dict(row, _id="a"))
    with pytest.raises(DuplicateKeyError):
        adb.eval_runs.insert_one(dict(row, _id="b"))

    from pregame import gate, ledger

    filed = gate.file_proposal(adb, {"field": "retirement", "kind": "policy", "key": "retirement", "base_version": 1,
                                     "body": {"max_facts": 7}, "diff": ["max_facts 6 -> 7"], "rationale": "test",
                                     "evidence": [], "filed_by": "improver", "created_sim": SIM})
    twin = dict(adb.proposals.find_one({"_id": filed["_id"]}), _id="prop-twin")        # same idem_key
    with pytest.raises(DuplicateKeyError):
        adb.proposals.insert_one(twin)

    first = adb.ledger.find_one({"seq": 1})
    assert first is not None
    with pytest.raises(DuplicateKeyError):                                             # the chain cannot fork
        adb.ledger.insert_one(dict(first, _id=10_000))              # a new _id, the same seq


def test_validators_reject_malformed_documents(adb):
    for name in ("proposals", "config_versions", "ledger"):
        with pytest.raises(WriteError) as exc:
            adb[name].insert_one({"_id": f"malformed-{name}", "junk": True})
        assert exc.value.code == 121, name          # DocumentValidationFailure


def test_ledger_times_round_trip_and_the_chain_verifies(adb):
    from pregame import ledger

    odd_zone = timezone(timedelta(hours=-4))
    when = datetime(2026, 4, 1, 9, 30, 15, 123456, tzinfo=odd_zone)       # microseconds, not UTC
    ledger.append(adb, "seed", "test", {"at": when, "note": "round trip"}, when)
    ledger.append(adb, "event", "test", {"n": 2}, when + timedelta(seconds=1))
    stored = adb.ledger.find_one({"seq": 1})
    assert stored["sim_time"].tzinfo is not None
    assert stored["sim_time"].utcoffset() == timedelta(0)
    assert stored["sim_time"].microsecond == 123000                       # BSON keeps milliseconds
    ok, n, msg = ledger.verify(adb)
    assert ok and n == 2, msg


def test_a_failed_commit_leaves_nothing_behind(adb, monkeypatch):
    from pregame import ledger, versions

    versions.seed_configs(adb, SIM)
    head = adb.config_heads.find_one({"_id": {"$regex": "^policy:"}})
    kind, key, base = "policy", head["_id"].split(":", 1)[1], head["version"]
    before = (adb.config_heads.find_one({"_id": head["_id"]}), adb.config_versions.count_documents({}),
              adb.ledger.count_documents({}))

    def boom(*args, **kwargs):              # fails after the head bump and the version insert
        raise RuntimeError("injected failure inside the fence")

    monkeypatch.setattr(ledger, "append", boom)
    body = dict(versions.resolve_field_config(adb, key)["policy"], max_facts=7)
    with pytest.raises(RuntimeError):
        versions.commit(adb, kind, key, base, body, rationale="test", sim_time=SIM)
    after = (adb.config_heads.find_one({"_id": head["_id"]}), adb.config_versions.count_documents({}),
             adb.ledger.count_documents({}))
    assert after == before


def test_two_racing_commits_from_one_base_produce_exactly_one_version(adb):
    from pregame import versions

    versions.seed_configs(adb, SIM)
    head = adb.config_heads.find_one({"_id": {"$regex": "^policy:"}})
    key, base = head["_id"].split(":", 1)[1], head["version"]
    policy = versions.resolve_field_config(adb, key)["policy"]
    versions_before = adb.config_versions.count_documents({})
    results: list = []
    start = threading.Barrier(2)

    def attempt(max_facts: int) -> None:
        start.wait()
        try:
            versions.commit(adb, "policy", key, base, dict(policy, max_facts=max_facts), rationale="race",
                            sim_time=SIM)
            results.append("won")
        except versions.StaleVersion:
            results.append("stale")

    threads = [threading.Thread(target=attempt, args=(n,)) for n in (7, 8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == ["stale", "won"]
    assert adb.config_heads.find_one({"_id": head["_id"]})["version"] == base + 1
    assert adb.ledger.count_documents({"kind": "commit"}) == 1
    assert adb.config_versions.count_documents({}) == versions_before + 1
    assert adb.config_versions.count_documents({"kind": "policy", "key": key, "version": base + 1}) == 1
