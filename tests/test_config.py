"""Tests for the operator-facing banner helpers in pregame.config: uri_host and mode_banner.

These back the "no mode banner" fix: every command that calls a model or writes prints mode,
provider, the three model ids and the target database + host -- never the connection string,
username or password.
"""
from __future__ import annotations

from pregame.config import Settings, mode_banner, uri_host


# ---------------------------------------------------------------------------------------------
# uri_host
# ---------------------------------------------------------------------------------------------
def test_uri_host_strips_credentials_and_path():
    uri = "mongodb+srv://myuser:SuperSecretPass1@cluster0.mongodb.net/pregame?retryWrites=true"
    host = uri_host(uri)
    assert host == "cluster0.mongodb.net"
    assert "myuser" not in host
    assert "SuperSecretPass1" not in host
    assert "pregame" not in host


def test_uri_host_plain_uri_with_no_credentials():
    assert uri_host("mongodb://localhost:27017") == "localhost:27017"


def test_uri_host_multi_host_replica_set_with_credentials():
    uri = "mongodb://user:pass@host1:27017,host2:27017,host3:27017/pregame?replicaSet=rs0"
    host = uri_host(uri)
    assert host == "host1:27017,host2:27017,host3:27017"
    assert "user" not in host
    assert "pass" not in host


def test_uri_host_empty_string():
    assert uri_host("") == ""


# ---------------------------------------------------------------------------------------------
# mode_banner
# ---------------------------------------------------------------------------------------------
def test_mode_banner_contains_mode_provider_models_and_db_no_credentials():
    settings = Settings(
        mongodb_uri="mongodb+srv://myuser:SuperSecretPass1@cluster0.mongodb.net/?retryWrites=true",
        db_name="pregame",
        llm_mode="live",
        provider="anthropic",
        models={"drafter": "claude-sonnet-5", "reader": "claude-haiku-4-5", "improver": "claude-opus-5-5"},
    )
    line = mode_banner("pregame", settings)

    assert "\n" not in line  # one line
    assert "mode=live" in line
    assert "provider=anthropic" in line
    assert "claude-sonnet-5" in line
    assert "claude-haiku-4-5" in line
    assert "claude-opus-5-5" in line
    assert "cluster0.mongodb.net" in line
    assert "db=pregame@" in line

    # never the connection string, username or password
    assert "myuser" not in line
    assert "SuperSecretPass1" not in line
    assert "mongodb+srv://" not in line
    assert settings.mongodb_uri not in line


def test_mode_banner_fake_mode_localhost():
    settings = Settings(mongodb_uri="mongodb://localhost:27017", db_name="pregame_test", llm_mode="fake")
    line = mode_banner("pregame_test", settings)
    assert line == (
        "mode=fake provider=anthropic drafter=claude-sonnet-5 reader=claude-haiku-4-5 "
        "improver=claude-opus-5-5 db=pregame_test@localhost:27017"
    )
