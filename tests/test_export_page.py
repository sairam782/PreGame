"""Tests for scripts/export_page.py: the static-snapshot exporter for the live page.

Seeds a mongomock database with `loop.setup` + `loop.run_demo` (the fake LLM, no network, no
model calls -- see tests/test_loop.py) so the shape matches a real stage database, then runs the
exporter against it and checks the output HTML embeds the state/briefs and carries no mongo
connection string.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from pregame import loop
from pregame.config import Settings
from pregame.llm import get_llm

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_page.py"


def _load_export_page():
    """Import scripts/export_page.py by path (scripts/ isn't a package)."""
    spec = importlib.util.spec_from_file_location("export_page", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export_page = _load_export_page()


def make_fake_llm():
    settings = Settings(
        mongodb_uri="mongodb://localhost:27017",
        anthropic_api_key=None,
        llm_mode="fake",
        models={"drafter": "fake", "reader": "fake", "improver": "fake"},
        cassette_path="",
    )
    llm = get_llm(settings)
    assert llm.is_fake
    return llm


@pytest.fixture
def llm():
    return make_fake_llm()


@pytest.fixture
def seeded_db(db, llm):
    """A mongomock db run through the same scripted demo the stage database was built from."""
    loop.run_demo(db, llm, yes=True)
    return db


def test_collect_snapshot_has_state_and_referenced_briefs(seeded_db):
    snapshot = export_page.collect_snapshot(seeded_db)

    assert "state" in snapshot and "briefs_by_id" in snapshot
    state = snapshot["state"]
    assert isinstance(state, dict)
    assert "ledger_verified" in state
    assert "proposals" in state
    assert state["proposals"], "expected at least one proposal from run_demo"

    referenced_ids = {
        b["_id"]
        for field_briefs in (state.get("briefs") or {}).values()
        for b in field_briefs
    }
    assert referenced_ids, "expected run_demo to have written at least one brief"
    assert referenced_ids <= snapshot["briefs_by_id"].keys()
    for brief_id in referenced_ids:
        assert snapshot["briefs_by_id"][brief_id]["_id"] == brief_id

    # JSON-serialisable end to end (no leftover datetimes/ObjectIds).
    json.dumps(snapshot)


def test_scrub_drops_credential_shaped_keys_and_redacts_mongo_uris():
    dirty = {
        "mongodb_uri": "mongodb+srv://alex:hunter2@cluster0.mongodb.net/pregame_demo",
        "nested": {"password": "hunter2", "host": "cluster0.mongodb.net", "fine": "keep me"},
        "note": "see mongodb://user:pw@localhost:27017/db for the connection",
        "list": [{"api_key": "sk-secret"}, "plain value"],
    }
    clean = export_page._scrub(dirty)
    blob = json.dumps(clean)
    assert "mongodb_uri" not in clean
    assert "password" not in clean["nested"]
    assert "host" not in clean["nested"]
    assert clean["nested"]["fine"] == "keep me"
    assert "hunter2" not in blob
    assert "sk-secret" not in blob
    assert "mongodb://" not in blob and "mongodb+srv://" not in blob
    assert "[redacted]" in clean["note"]


def test_export_page_writes_self_contained_html(seeded_db, monkeypatch, tmp_path):
    """Run the exporter end to end (CLI-level function), against the mongomock db, no network."""
    import pregame.db as pregame_db

    monkeypatch.setattr(pregame_db, "get_db", lambda *a, **k: seeded_db)

    out_path = tmp_path / "index.html"
    written = export_page.export_page("pregame_test", out_path)

    assert written == out_path
    assert out_path.exists()
    html_text = out_path.read_text(encoding="utf-8")

    # No secret ever reaches disk.
    assert "mongodb://" not in html_text
    assert "mongodb+srv://" not in html_text
    assert re.search(r"mongodb", html_text, re.IGNORECASE) is None

    # The banner is present and mentions the database + the repo.
    assert "database pregame_test" in html_text
    assert "https://github.com/sairam782/PreGame" in html_text

    # The embedded snapshot round-trips and matches what the live page would have fetched.
    match = re.search(
        r'<script id="pregame-snapshot-data" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    assert match, "expected an embedded snapshot <script> tag"
    embedded = json.loads(match.group(1))
    assert "state" in embedded and "briefs_by_id" in embedded
    assert embedded["state"]["proposals"]

    # The original page's own rendering script survives untouched.
    assert "function renderProposals()" in html_text
    assert "window.fetch = function" in html_text

    # Self-contained: index.html itself uses no external stylesheet/font links, and the exporter
    # must not introduce any -- the only external URL allowed is the plain repo link in the banner.
    static_original = (REPO_ROOT / "pregame" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "<link" not in static_original  # nothing external for the exporter to have to account for
    external_urls = re.findall(r'(?:href|src)=["\'](https?://[^"\']+)', html_text)
    assert external_urls == []


def test_export_page_embeds_cabinet_run(seeded_db, monkeypatch, tmp_path):
    """The exporter embeds the latest cabinet_runs document and the shim can answer /api/cabinet from it."""
    from datetime import datetime, timezone

    import pregame.db as pregame_db
    from pregame.cabinet import RUNS_COLLECTION

    seeded_db[RUNS_COLLECTION].insert_one({
        "_id": "CR-1", "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "scores": {"harness": {"faults_total": 0}},
    })
    monkeypatch.setattr(pregame_db, "get_db", lambda *a, **k: seeded_db)

    out_path = tmp_path / "index.html"
    export_page.export_page("pregame_test", out_path)
    html_text = out_path.read_text(encoding="utf-8")

    match = re.search(
        r'<script id="pregame-snapshot-data" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    embedded = json.loads(match.group(1))
    assert embedded["cabinet"]["_id"] == "CR-1"
    assert embedded["cabinet"]["scores"]["harness"]["faults_total"] == 0
    assert "/api/cabinet" in html_text  # the fetch shim answers /api/cabinet from the embedded snapshot


def test_export_page_cabinet_is_empty_dict_when_no_runs(seeded_db, monkeypatch, tmp_path):
    """No cabinet_runs document at all -> the embedded snapshot's cabinet field is {}."""
    import pregame.db as pregame_db

    monkeypatch.setattr(pregame_db, "get_db", lambda *a, **k: seeded_db)

    out_path = tmp_path / "index.html"
    export_page.export_page("pregame_test", out_path)
    html_text = out_path.read_text(encoding="utf-8")

    match = re.search(
        r'<script id="pregame-snapshot-data" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    embedded = json.loads(match.group(1))
    assert embedded["cabinet"] == {}


def test_export_page_is_read_only(seeded_db, monkeypatch, tmp_path):
    """The exporter must never reset/reinitialise the database it reads."""
    import pregame.db as pregame_db

    monkeypatch.setattr(pregame_db, "get_db", lambda *a, **k: seeded_db)
    monkeypatch.setattr(pregame_db, "reset_db", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("export_page must never call reset_db")
    ))
    monkeypatch.setattr(pregame_db, "init_db", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("export_page must never call init_db")
    ))

    before = seeded_db.proposals.count_documents({})
    export_page.export_page("pregame_test", tmp_path / "index.html")
    after = seeded_db.proposals.count_documents({})
    assert before == after
