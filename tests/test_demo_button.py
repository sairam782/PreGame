"""The "Run it live" button: POST /api/demo/run + GET /api/demo/status, with subprocess patched (no real run)."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import pregame.web.app as webapp

LOCAL = ("127.0.0.1", 50000)


class FakeProc:
    """Stands in for subprocess.Popen: yields canned stdout lines, exits with `code` once `gate` is set."""

    def __init__(self, lines, code=0, gate=None):
        self.stdout = iter([line + "\n" for line in lines])
        self.code = code
        self.gate = gate
        self.killed = False

    def wait(self, timeout=None):
        if self.gate is not None and not self.gate.wait(timeout=10):
            raise AssertionError("test gate never released")
        return self.code

    def kill(self):
        self.killed = True


@pytest.fixture(autouse=True)
def fresh_demo_state(monkeypatch):
    webapp._demo.update(running=False, step=None, started_at=None, steps=[])
    monkeypatch.setattr(webapp, "_server_db_name", lambda: "pregame_stage")
    yield
    webapp._demo.update(running=False, step=None, started_at=None, steps=[])


def _wait_idle(client, limit=5.0):
    deadline = time.time() + limit
    while time.time() < deadline:
        st = client.get("/api/demo/status").json()
        if not st["running"]:
            return st
        time.sleep(0.02)
    raise AssertionError("demo run never finished")


def test_post_starts_run_and_status_reports_steps(monkeypatch):
    calls = []

    def fake_popen(cmd, cwd=None, env=None, **kw):
        calls.append((cmd, env))
        if "demo" in cmd:
            return FakeProc(["resetting pregame_stage", "loop rebuilt"])
        return FakeProc(["mode banner", "brief b-123  (retirement/acct-1)", "  versions: {}", "", "# Retirement brief"])

    monkeypatch.setattr(webapp.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PREGAME_CLAUDE_DIR", "C:/fake/claude/bin")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://nope")
    client = TestClient(webapp.app, client=LOCAL)

    res = client.post("/api/demo/run")
    assert res.status_code == 200 and res.json()["db"] == "pregame_stage"
    st = _wait_idle(client)
    assert [s["state"] for s in st["steps"]] == ["done", "done"]
    assert st["steps"][0]["lines"][-1] == "loop rebuilt"
    assert st["steps"][1]["brief_head"][0].startswith("brief b-123")
    assert st["started_at"] is not None and all(s["seconds"] is not None for s in st["steps"])

    (cmd1, env1), (cmd2, env2) = calls
    assert cmd1[1:] == ["-m", "pregame.cli", "demo"] and env1["PREGAME_LLM_MODE"] == "replay"
    assert cmd2[-2:] == ["brief", "retirement"] and env2["PREGAME_LLM_MODE"] == "live"
    assert env2["PREGAME_PROVIDER"] == "claude-cli" and env2["PREGAME_DB"] == "pregame_stage"
    assert env2["PATH"].startswith("C:/fake/claude/bin")
    assert not any(k.upper().startswith(("CLAUDE", "ANTHROPIC")) for k in env2)


def test_second_post_while_running_is_409(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda cmd, **kw: FakeProc(["working"], gate=gate))
    client = TestClient(webapp.app, client=LOCAL)
    try:
        assert client.post("/api/demo/run").status_code == 200
        assert client.get("/api/demo/status").json()["running"] is True
        assert client.post("/api/demo/run").status_code == 409
    finally:
        gate.set()
    _wait_idle(client)


def test_live_failure_falls_back_to_recording(monkeypatch):
    def fake_popen(cmd, env=None, **kw):
        if "demo" in cmd:
            return FakeProc(["ok"])
        if env["PREGAME_LLM_MODE"] == "live":
            return FakeProc(["claude said no"], code=1)
        assert env["PREGAME_CASSETTE"] == "cassettes/room-brief.jsonl"
        return FakeProc(["brief b-9  (retirement/acct-1)", "# recorded"])

    monkeypatch.setattr(webapp.subprocess, "Popen", fake_popen)
    client = TestClient(webapp.app, client=LOCAL)
    assert client.post("/api/demo/run").status_code == 200
    st = _wait_idle(client)
    assert st["steps"][1]["state"] == "fallback"
    assert "recorded fallback" in st["steps"][1]["note"]


def test_non_local_client_is_refused(monkeypatch):
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    client = TestClient(webapp.app, client=("203.0.113.7", 50000))
    assert client.post("/api/demo/run").status_code == 403
    assert client.get("/api/demo/status").json()["running"] is False


def test_non_pregame_db_is_refused(monkeypatch):
    monkeypatch.setattr(webapp, "_server_db_name", lambda: "production")
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda *a, **k: pytest.fail("must not start"))
    client = TestClient(webapp.app, client=LOCAL)
    assert client.post("/api/demo/run").status_code == 400


def test_uri_redaction():
    line = "\x1b[1mconnecting to mongodb+srv://alex:s3cret@cluster0.abcde.mongodb.net/?retryWrites=true now\x1b[0m"
    out = webapp.redact(line)
    assert "s3cret" not in out and "alex:" not in out and "\x1b" not in out
    assert out == "connecting to mongodb://[redacted] now"
    assert webapp.redact("mongodb://u:p@localhost:27017") == "mongodb://[redacted]"
