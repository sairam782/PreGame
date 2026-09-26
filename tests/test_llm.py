"""Tests for pregame.llm (record/replay round-trip, JSON extraction, retry) and the Settings repr.

No network calls: the Anthropic client is monkeypatched with an in-process stub.
"""
from __future__ import annotations

import json

import anthropic
import pytest

from pregame.config import DEFAULT_MODELS, Settings
from pregame.config import settings as config_settings
from pregame.llm import LLM, LLMError, _cassette_key, _extract_json_object, get_llm


# ---------------------------------------------------------------------------------------------
# stub Anthropic client (no network)
# ---------------------------------------------------------------------------------------------
class _StubUsage:
    def __init__(self, input_tokens=10, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _StubTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _StubResponse:
    def __init__(self, text):
        self.content = [_StubTextBlock(text)]
        self.usage = _StubUsage()


class _StubMessages:
    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self._texts.pop(0)
        return _StubResponse(text)


class _StubClient:
    def __init__(self, texts):
        self.messages = _StubMessages(texts)


def _patch_anthropic(monkeypatch, stub_client):
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: stub_client)


def make_settings(**overrides) -> Settings:
    base = dict(
        mongodb_uri="mongodb://localhost:27017",
        anthropic_api_key="fake-key",
        llm_mode="live",
    )
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------------------------
# Settings repr
# ---------------------------------------------------------------------------------------------
def test_settings_repr_masks_secrets():
    s = Settings(
        mongodb_uri="mongodb+srv://myuser:SuperSecretPass1@cluster0.mongodb.net/?retryWrites=true",
        anthropic_api_key="sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    )
    r = repr(s)
    assert "SuperSecretPass1" not in r
    assert "sk-ant-api03-XXXXXXXXXXXXXXXXXXXXXXXXXXXX" not in r
    assert "myuser" in r
    assert "mongodb+srv://" in r


def test_settings_repr_handles_no_password_and_no_key():
    s = Settings(mongodb_uri="mongodb://localhost:27017", anthropic_api_key=None)
    r = repr(s)
    assert "mongodb://localhost:27017" in r
    assert "anthropic_api_key=None" in r


def test_settings_env_overrides(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://example:27017")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("PREGAME_MODEL_DRAFTER", "claude-sonnet-5")
    monkeypatch.delenv("PREGAME_LLM_MODE", raising=False)
    config_settings.cache_clear()
    try:
        s = config_settings()
        assert s.mongodb_uri == "mongodb://example:27017"
        assert s.llm_mode == "live"  # default flips to live once a key is present
        assert s.models["drafter"] == "claude-sonnet-5"
    finally:
        config_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# model_id / defaults
# ---------------------------------------------------------------------------------------------
def test_default_model_ids_per_role():
    s = Settings(mongodb_uri="mongodb://localhost:27017")
    assert s.models["drafter"] == "claude-sonnet-5"
    assert s.models["reader"] == "claude-haiku-4-5"
    assert s.models["improver"] == "claude-opus-5-5"
    llm = LLM(s)  # llm_mode defaults to "fake" -> no client is constructed
    assert llm.is_fake is True
    assert llm.model_id("drafter") == "claude-sonnet-5"
    assert llm.model_id("reader") == "claude-haiku-4-5"
    assert llm.model_id("improver") == "claude-opus-5-5"


def test_fake_mode_complete_json_raises():
    s = Settings(mongodb_uri="mongodb://localhost:27017", llm_mode="fake")
    llm = LLM(s)
    with pytest.raises(LLMError):
        llm.complete_json("drafter", "system", "prompt")


def test_get_llm_uses_cached_settings(monkeypatch):
    monkeypatch.setenv("PREGAME_LLM_MODE", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_settings.cache_clear()
    try:
        llm = get_llm()
        assert llm.is_fake is True
    finally:
        config_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------------------------
class TestExtractJsonObject:
    def test_plain_json(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_json_code_fence_with_language_tag(self):
        text = 'Sure, here you go:\n```json\n{"a": 1, "b": [1, 2, 3]}\n```\nHope that helps!'
        assert _extract_json_object(text) == {"a": 1, "b": [1, 2, 3]}

    def test_code_fence_without_language_tag(self):
        text = '```\n{"z": 3}\n```'
        assert _extract_json_object(text) == {"z": 3}

    def test_leading_and_trailing_commentary_no_fence(self):
        text = 'Here is the JSON: {"x": "y"} — let me know if you need more.'
        assert _extract_json_object(text) == {"x": "y"}

    def test_no_json_present(self):
        assert _extract_json_object("no json here at all") is None

    def test_empty_and_none(self):
        assert _extract_json_object("") is None
        assert _extract_json_object(None) is None


# ---------------------------------------------------------------------------------------------
# record / replay round-trip
# ---------------------------------------------------------------------------------------------
def test_record_then_replay_round_trip(tmp_path, monkeypatch):
    cassette_path = str(tmp_path / "demo.jsonl")
    stub = _StubClient(['{"foo": 1}'])
    _patch_anthropic(monkeypatch, stub)

    record_settings = make_settings(llm_mode="record", cassette_path=cassette_path)
    recorder = LLM(record_settings)

    result = recorder.complete_json("drafter", "sys-prompt", "user-prompt")
    assert result == {"foo": 1}
    assert len(stub.messages.calls) == 1
    assert recorder.usage["drafter"]["calls"] == 1
    assert recorder.usage["drafter"]["input_tokens"] == 10
    assert recorder.usage["drafter"]["output_tokens"] == 20

    # cassette has exactly one line, keyed correctly
    lines = [json.loads(l) for l in open(cassette_path, encoding="utf-8") if l.strip()]
    assert len(lines) == 1
    model = recorder.model_id("drafter")
    expected_key = _cassette_key("drafter", model, "sys-prompt", "user-prompt")
    assert lines[0]["key"] == expected_key
    assert lines[0]["role"] == "drafter"
    assert lines[0]["model"] == model
    assert lines[0]["response"] == {"foo": 1}

    # replay: same settings/models, no client calls expected
    replay_settings = make_settings(llm_mode="replay", cassette_path=cassette_path)
    replayer = LLM(replay_settings)
    replay_result = replayer.complete_json("drafter", "sys-prompt", "user-prompt")
    assert replay_result == {"foo": 1}
    assert len(stub.messages.calls) == 1  # unchanged: replay never touches the client

    # a miss raises LLMError naming the role
    with pytest.raises(LLMError, match="drafter"):
        replayer.complete_json("drafter", "sys-prompt", "a different prompt")


def test_replay_without_cassette_file_raises(tmp_path):
    missing_path = str(tmp_path / "nope.jsonl")
    s = make_settings(llm_mode="replay", cassette_path=missing_path)
    llm = LLM(s)
    with pytest.raises(LLMError, match="reader"):
        llm.complete_json("reader", "sys", "prompt")


# ---------------------------------------------------------------------------------------------
# invalid JSON -> one retry -> success or LLMError
# ---------------------------------------------------------------------------------------------
def test_invalid_json_retries_once_then_succeeds(monkeypatch):
    stub = _StubClient(["not json at all", '{"ok": true}'])
    _patch_anthropic(monkeypatch, stub)
    llm = LLM(make_settings(llm_mode="live"))

    result = llm.complete_json("reader", "sys", "prompt")
    assert result == {"ok": True}
    assert len(stub.messages.calls) == 2
    assert llm.usage["reader"]["calls"] == 2
    # the retry call includes a corrective follow-up message
    retry_messages = stub.messages.calls[1]["messages"]
    assert retry_messages[0]["content"] == "prompt"
    assert retry_messages[1]["content"] == "not json at all"
    assert "valid JSON" in retry_messages[2]["content"]


def test_invalid_json_after_retry_raises(monkeypatch):
    stub = _StubClient(["still not json", "nope, still not json either"])
    _patch_anthropic(monkeypatch, stub)
    llm = LLM(make_settings(llm_mode="live"))

    with pytest.raises(LLMError, match="reader"):
        llm.complete_json("reader", "sys", "prompt")
    assert len(stub.messages.calls) == 2
    assert llm.usage["reader"]["calls"] == 2
