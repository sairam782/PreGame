"""Tests for pregame.llm (record/replay round-trip, JSON extraction, retry) and the Settings repr.

No network calls: the Anthropic client is monkeypatched with an in-process stub.
"""
from __future__ import annotations

import io
import json
import threading

import anthropic
import pytest

from pregame.config import DEFAULT_MODELS, Settings
from pregame.config import settings as config_settings
from pregame.llm import LLM, LLMError, _cassette_key, _extract_json_object, _ProgressReporter, get_llm


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


# ---------------------------------------------------------------------------------------------
# progress indicator (no output during long live/record runs otherwise)
# ---------------------------------------------------------------------------------------------
def test_fake_and_replay_modes_have_no_progress_reporter(tmp_path):
    fake = LLM(Settings(mongodb_uri="mongodb://localhost:27017", llm_mode="fake"))
    assert fake._progress is None

    cassette = tmp_path / "empty.jsonl"
    cassette.write_text("", encoding="utf-8")
    replay = LLM(make_settings(llm_mode="replay", cassette_path=str(cassette)))
    assert replay._progress is None


def test_live_and_record_modes_have_a_progress_reporter(tmp_path):
    live = LLM(make_settings(llm_mode="live"))
    assert live._progress is not None

    record = LLM(make_settings(llm_mode="record", cassette_path=str(tmp_path / "c.jsonl")))
    assert record._progress is not None


def test_live_calls_tick_the_progress_counter_per_role(monkeypatch):
    stub = _StubClient(['{"a": 1}', '{"b": 2}', '{"c": 3}'])
    _patch_anthropic(monkeypatch, stub)
    llm = LLM(make_settings(llm_mode="live"))

    llm.complete_json("drafter", "sys", "p1")
    llm.complete_json("drafter", "sys", "p2")
    llm.complete_json("reader", "sys", "p3")

    assert llm._progress.counts["drafter"] == 2
    assert llm._progress.counts["reader"] == 1


def test_progress_never_touches_prompt_or_cassette_key(monkeypatch, tmp_path):
    """The progress indicator is pure stderr side-effect: it must not change the cassette key or
    the prompt/system text a call sends, so record/replay round-trips stay byte-for-byte identical
    whether or not progress reporting is happening."""
    cassette_path = str(tmp_path / "demo.jsonl")
    stub = _StubClient(['{"foo": 1}'])
    _patch_anthropic(monkeypatch, stub)

    recorder = LLM(make_settings(llm_mode="record", cassette_path=cassette_path))
    recorder.complete_json("drafter", "sys-prompt", "user-prompt")
    sent = stub.messages.calls[0]
    assert sent["system"] == "sys-prompt"
    assert sent["messages"][0]["content"] == "user-prompt"

    lines = [json.loads(l) for l in open(cassette_path, encoding="utf-8") if l.strip()]
    model = recorder.model_id("drafter")
    assert lines[0]["key"] == _cassette_key("drafter", model, "sys-prompt", "user-prompt")


def test_progress_reporter_plain_mode_writes_periodic_lines_not_every_call():
    stream = io.StringIO()
    reporter = _ProgressReporter(stream=stream, plain_every=2)
    assert reporter._tty is False  # StringIO reports not-a-tty

    reporter.tick("drafter")  # total=1 -> emitted (first call)
    reporter.tick("drafter")  # total=2 -> emitted (every 2nd)
    reporter.tick("drafter")  # total=3 -> not emitted

    output = stream.getvalue()
    assert output.count("\n") == 2
    assert "drafter=1" in output
    assert "drafter=2" in output
    assert "\r" not in output


def test_progress_reporter_tty_mode_uses_carriage_return_in_place():
    class _FakeTty(io.StringIO):
        def isatty(self):
            return True

    stream = _FakeTty()
    reporter = _ProgressReporter(stream=stream)
    assert reporter._tty is True

    reporter.tick("reader")
    reporter.tick("reader")

    output = stream.getvalue()
    assert output.count("\r") == 2
    assert "\n" not in output
    assert "reader=2" in output


def test_progress_reporter_is_thread_safe():
    stream = io.StringIO()
    reporter = _ProgressReporter(stream=stream, plain_every=1000)
    n_threads, n_ticks = 8, 50

    def worker():
        for _ in range(n_ticks):
            reporter.tick("drafter")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert reporter.counts["drafter"] == n_threads * n_ticks


def test_replay_serves_a_repeated_prompt_its_recorded_answers_in_order(tmp_path, monkeypatch):
    """The gate asks the drafter the same prompt k times (k runs per held-out meeting) and the live model answers
    differently each time; replay must hand back those k answers, not the last one k times."""
    cassette_path = str(tmp_path / "demo.jsonl")
    stub = _StubClient(['{"run": 1}', '{"run": 2}'])
    _patch_anthropic(monkeypatch, stub)
    recorder = LLM(make_settings(llm_mode="record", cassette_path=cassette_path))
    assert [recorder.complete_json("drafter", "sys", "same prompt") for _ in range(2)] == [{"run": 1}, {"run": 2}]

    replayer = LLM(make_settings(llm_mode="replay", cassette_path=cassette_path))
    served = [replayer.complete_json("drafter", "sys", "same prompt") for _ in range(3)]
    assert served == [{"run": 1}, {"run": 2}, {"run": 2}]      # recorded order, then the last answer repeats
