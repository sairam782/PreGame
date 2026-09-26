"""Tests for pregame.cli's `setup` safety check and mode banner (cmd_setup / build_parser).

`_db`/`_llm` are monkeypatched to a mongomock db and a fake LLM so nothing here touches a real
MongoDB or model -- matching the fixture-free style of the rest of the CLI (no server, no network).
"""
from __future__ import annotations

import mongomock
import pytest

from pregame import cli
from pregame.config import Settings
from pregame.llm import get_llm


def _fake_llm():
    return get_llm(Settings(mongodb_uri="mongodb://localhost:27017", llm_mode="fake", cassette_path=""))


def test_build_parser_setup_has_yes_flag():
    args = cli.build_parser().parse_args(["setup", "--yes"])
    assert args.yes is True

    args = cli.build_parser().parse_args(["setup"])
    assert args.yes is False


def test_cmd_setup_refuses_a_non_pregame_db_and_prints_the_banner(monkeypatch, capsys):
    db = mongomock.MongoClient(tz_aware=True)["not_pregame"]
    llm = _fake_llm()
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(cli, "_llm", lambda: llm)

    args = cli.build_parser().parse_args(["setup"])
    with pytest.raises(SystemExit) as exc:
        cli.cmd_setup(args)
    assert exc.value.code == 1

    out, err = capsys.readouterr()
    assert "mode=fake" in out and "db=not_pregame@" in out  # banner printed before the refusal
    assert "refused" in err.lower()
    assert db.list_collection_names() == []  # nothing was dropped or seeded


def test_cmd_setup_with_yes_flag_resets_and_seeds(monkeypatch, capsys):
    db = mongomock.MongoClient(tz_aware=True)["not_pregame"]
    llm = _fake_llm()
    monkeypatch.setattr(cli, "_db", lambda: db)
    monkeypatch.setattr(cli, "_llm", lambda: llm)

    args = cli.build_parser().parse_args(["setup", "--yes"])
    cli.cmd_setup(args)  # must not raise / exit

    out = capsys.readouterr().out
    assert "mode=fake" in out
    assert "setup complete" in out
    assert db.facts.count_documents({}) > 0
