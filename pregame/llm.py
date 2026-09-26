"""The LLM interface: live, fake, record and replay modes.

`complete_json` always returns the parsed JSON object the model wrote (never raw text). In fake
mode it raises `LLMError` — each caller (drafter, oracle's reader, improver) implements its own
deterministic fake path when `llm.is_fake` is true, per INTERFACES.md.

record mode makes a real call and appends {key, role, model, response} to a cassette JSONL file,
keyed by sha256(role + model + system + prompt). replay mode never touches the network — it serves
recorded responses by that same key and raises LLMError (naming the role) on a miss.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from collections import defaultdict
from typing import Optional

import anthropic

from pregame.config import Settings
from pregame.config import settings as _get_settings


class LLMError(Exception):
    """Raised on any LLM failure: fake-mode call, invalid JSON after retry, or a replay miss."""


class _ProgressReporter:
    """A lightweight, thread-safe per-role call counter for live/record calls, written to stderr.

    A cold live evaluation is ~70 model calls and can run for minutes with nothing else printed;
    without this the terminal looks hung. Writes only to stderr (never stdout, never anything that
    reaches a prompt or the cassette key): a running total, updated in place with `\\r` when stderr
    is a TTY, or a plain line every few calls otherwise (piped output, CI logs).
    """

    def __init__(self, stream=None, plain_every: int = 5):
        self._stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self.counts: dict = defaultdict(int)
        self._tty = bool(getattr(self._stream, "isatty", lambda: False)())
        self._plain_every = max(1, plain_every)

    def tick(self, role: str) -> None:
        with self._lock:
            self.counts[role] += 1
            total = sum(self.counts.values())
            summary = " ".join(f"{r}={n}" for r, n in sorted(self.counts.items()))
            line = f"model calls: {summary} (total {total})"
            try:
                if self._tty:
                    self._stream.write(f"\r{line}")
                    self._stream.flush()
                elif total == 1 or total % self._plain_every == 0:
                    self._stream.write(f"{line}\n")
                    self._stream.flush()
            except Exception:
                pass  # progress output is best-effort; never let it break a real call


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_object(text: Optional[str]) -> Optional[dict]:
    """Pull the first JSON *object* out of `text`, tolerating markdown code fences and any
    leading/trailing commentary. Returns None if no valid JSON object can be found."""
    if not text:
        return None
    text = text.strip()

    candidates = []
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())
    candidates.append(text)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                obj, _end = decoder.raw_decode(candidate, start)
            except json.JSONDecodeError:
                start = candidate.find("{", start + 1)
                continue
            if isinstance(obj, dict):
                return obj
            start = candidate.find("{", start + 1)
    return None


def _cassette_key(role: str, model: str, system: str, prompt: str) -> str:
    payload = f"{role}{model}{system}{prompt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_RETRY_MESSAGE = (
    "That was not a single valid JSON object. Reply again with ONLY one valid JSON object — "
    "no code fences, no commentary, no leading or trailing text."
)


class LLM:
    """Thread-safe wrapper over the Anthropic Messages API with fake/record/replay support."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self.settings = settings  # public: lets callers (banners) report the mode/models actually in use
        self.is_fake = settings.llm_mode == "fake"
        self._mode = settings.llm_mode
        self._client: Optional[anthropic.Anthropic] = None
        self._usage_lock = threading.Lock()
        self._cassette_lock = threading.Lock()
        self.usage: dict = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        self._replay_cache: Optional[dict] = None
        self._replay_served: dict = {}
        # Progress feedback is only meaningful (and only ever ticked) for live/record calls: fake mode
        # raises before any call, replay serves from the cassette without calling out.
        self._progress: Optional[_ProgressReporter] = (
            _ProgressReporter() if settings.llm_mode in ("live", "record") else None
        )

        self._provider = getattr(settings, "provider", "anthropic")
        self._cli_slots = threading.BoundedSemaphore(max(1, getattr(settings, "cli_concurrency", 3)))
        if self._mode in ("live", "record") and self._provider == "anthropic":
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        if self._mode in ("live", "record") and self._provider not in ("anthropic", "claude-cli"):
            raise LLMError(f"unknown provider {self._provider!r} (anthropic | claude-cli; openrouter is planned)")
        if self._mode == "replay":
            self._replay_cache = self._load_cassette()
        if self._mode == "record":
            # Replay serves the FIRST recorded answer for a prompt, so recording on top of an older run would replay
            # the old answers: refuse unless the caller opts in to appending.
            path = settings.cassette_path
            if (path and os.path.exists(path) and os.path.getsize(path) > 0
                    and os.environ.get("PREGAME_CASSETTE_APPEND") != "1"):
                raise LLMError(f"record: cassette {path} already holds a recording; move it aside or set "
                               "PREGAME_CASSETTE_APPEND=1 to add to it")

    # -- public interface -------------------------------------------------------------------
    def model_id(self, role: str) -> str:
        return self._settings.models[role]

    def complete_json(self, role: str, system: str, prompt: str, max_tokens: int = 2000) -> dict:
        if self.is_fake:
            raise LLMError(
                f"complete_json called in fake mode for role '{role}'; the caller must use its "
                "own deterministic fake path when llm.is_fake is true"
            )

        model = self.model_id(role)

        if self._mode == "replay":
            key = _cassette_key(role, model, system, prompt)
            rows = self._replay_cache.get(key) if self._replay_cache else None
            if not rows:
                raise LLMError(
                    f"replay: no cassette entry for role '{role}' (model={model}, key={key})"
                )
            # The same prompt can be asked more than once (the gate scores each held-out meeting k times), and the
            # recording holds one answer per ask: serve them in recorded order so replay sees the same set of answers
            # the live run saw, not the last one k times. Past the recorded count, repeat the last answer.
            with self._cassette_lock:
                n = self._replay_served.get(key, 0)
                self._replay_served[key] = n + 1
            return rows[min(n, len(rows) - 1)]["response"]

        # live or record
        if self._progress is not None:
            self._progress.tick(role)
        text = self._call(role, model, system, prompt, max_tokens)
        parsed = _extract_json_object(text)
        if parsed is None:
            text = self._call_retry(role, model, system, prompt, text, max_tokens)
            parsed = _extract_json_object(text)
            if parsed is None:
                raise LLMError(
                    f"model did not return valid JSON for role '{role}' (model={model}) after retry"
                )

        if self._mode == "record":
            self._append_cassette(role, model, system, prompt, parsed)

        return parsed

    # -- cassette -----------------------------------------------------------------------------
    def _load_cassette(self) -> dict:
        """key -> every recorded row for that key, in recording order."""
        cache: dict = {}
        path = self._settings.cassette_path
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    cache.setdefault(row["key"], []).append(row)
        return cache

    def _append_cassette(self, role: str, model: str, system: str, prompt: str, response: dict) -> None:
        key = _cassette_key(role, model, system, prompt)
        row = {"key": key, "role": role, "model": model, "response": response}
        path = self._settings.cassette_path
        with self._cassette_lock:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    # -- live calls ---------------------------------------------------------------------------
    def _call(self, role: str, model: str, system: str, prompt: str, max_tokens: int) -> str:
        if self._provider == "claude-cli":
            return self._call_cli(role, model, system, prompt)
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        self._record_usage(role, response)
        return _response_text(response)

    def _call_retry(
        self, role: str, model: str, system: str, prompt: str, bad_text: str, max_tokens: int
    ) -> str:
        if self._provider == "claude-cli":
            retry_prompt = f"{prompt}\n\nYour previous reply was:\n{bad_text or ''}\n\n{_RETRY_MESSAGE}"
            return self._call_cli(role, model, system, retry_prompt)
        response = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": bad_text or ""},
                {"role": "user", "content": _RETRY_MESSAGE},
            ],
        )
        self._record_usage(role, response)
        return _response_text(response)

    def _call_cli(self, role: str, model: str, system: str, prompt: str) -> str:
        """One headless `claude -p` call on the user's Claude subscription (no API key, nothing metered).

        Mechanics follow the Dispatch engine's proven claude lane: the prompt goes in on stdin, the system prompt from
        a file, tools off, one turn, an empty temporary working folder (so no project files load), CLAUDECODE stripped
        so it can run inside a Claude Code session, and the Windows .cmd shim routed through the shell.
        A semaphore caps parallel calls to stay inside the plan's rate limits.
        """
        import shutil
        import subprocess
        import tempfile

        workdir = tempfile.mkdtemp(prefix="pregame-cli-")
        try:
            sys_path = os.path.join(workdir, "system.txt")
            with open(sys_path, "w", encoding="utf-8") as f:
                f.write(system)
            cmd = ["claude", "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                   "--max-turns", "1", "--model", model, "--system-prompt-file", sys_path]
            # A clean environment, as a plain terminal would have: drop every CLAUDE*/ANTHROPIC* variable. Launched
            # from inside Claude Code (or its desktop app) the child would otherwise inherit the host's session
            # plumbing (a redirected ANTHROPIC_BASE_URL, "host refreshes my login" flags) and fail to authenticate;
            # dropping ANTHROPIC_API_KEY also keeps it on the subscription.
            env = {k: v for k, v in os.environ.items()
                   if not (k.upper().startswith("CLAUDE") or k.upper().startswith("ANTHROPIC"))}
            kw = dict(env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=300, cwd=workdir, input=prompt)
            with self._cli_slots:
                if os.name == "nt":
                    proc = subprocess.run(subprocess.list2cmdline(cmd), shell=True, **kw)
                else:
                    proc = subprocess.run(cmd, **kw)
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out for role '{role}'") from exc
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        if proc.returncode != 0 or not data:
            detail = str(data.get("result") or proc.stderr or proc.stdout or "")[:200]
            hint = " Sign in once: run `claude` in a terminal and complete the login." if "authentic" in detail.lower() else ""
            raise LLMError(f"claude CLI failed for role '{role}' (rc={proc.returncode}): {detail}{hint}")
        usage = data.get("usage") or {}
        with self._usage_lock:
            u = self.usage[role]
            u["calls"] += 1
            u["input_tokens"] += usage.get("input_tokens", 0) or 0
            u["output_tokens"] += usage.get("output_tokens", 0) or 0
        if data.get("is_error"):
            raise LLMError(f"claude CLI reported an error for role '{role}': {str(data.get('result'))[:200]}")
        return data.get("result", "") or ""

    def _record_usage(self, role: str, response) -> None:
        usage = getattr(response, "usage", None)
        with self._usage_lock:
            u = self.usage[role]
            u["calls"] += 1
            if usage is not None:
                u["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                u["output_tokens"] += getattr(usage, "output_tokens", 0) or 0


def _response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def get_llm(settings: Optional[Settings] = None) -> LLM:
    if settings is None:
        settings = _get_settings()
    return LLM(settings)
