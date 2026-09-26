"""Environment settings for Pregame.

Loads `.env` from the repo root (python-dotenv) and exposes a cached `Settings` instance via
`settings()`. Never log or print a raw secret: `Settings.__repr__` masks the Anthropic API key and
any password embedded in the MongoDB URI.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Repo root is the parent of the `pregame` package.
_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

# Roles -> default model ids. Overridable via PREGAME_MODEL_DRAFTER / _READER / _IMPROVER.
# improver: Opus 5.5, drafter: Sonnet 5, reader: Haiku 4.5 (exact ids per the claude-api skill).
DEFAULT_MODELS = {
    "drafter": "claude-sonnet-5",
    "reader": "claude-haiku-4-5",
    "improver": "claude-opus-5-5",
}

_URI_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")


def _mask_secret(value: Optional[str]) -> Optional[str]:
    """Mask a secret for display: keep a short prefix/suffix, hide the rest."""
    if not value:
        return value
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _mask_uri(uri: str) -> str:
    """Mask the password portion of a mongodb:// or mongodb+srv:// URI, if present."""
    if not uri:
        return uri
    return _URI_PASSWORD_RE.sub(lambda m: f"{m.group(1)}***{m.group(3)}", uri)


@dataclass
class Settings:
    mongodb_uri: str
    db_name: str = "pregame"
    anthropic_api_key: Optional[str] = None
    llm_mode: str = "fake"  # "fake" | "live" | "record" | "replay"
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    cassette_path: str = "cassettes/demo.jsonl"
    k: int = 2
    # Who answers live calls: "anthropic" (API credits, needs ANTHROPIC_API_KEY) or "claude-cli" (the user's Claude
    # subscription through headless `claude -p`, no key). "openrouter" is the planned third provider.
    provider: str = "anthropic"
    cli_concurrency: int = 3

    def __repr__(self) -> str:  # never leak secrets via logs/prints/debuggers
        return (
            "Settings("
            f"mongodb_uri={_mask_uri(self.mongodb_uri)!r}, "
            f"db_name={self.db_name!r}, "
            f"anthropic_api_key={_mask_secret(self.anthropic_api_key)!r}, "
            f"llm_mode={self.llm_mode!r}, "
            f"models={self.models!r}, "
            f"cassette_path={self.cassette_path!r}, "
            f"k={self.k}, provider={self.provider!r})"
        )


def _build_settings() -> Settings:
    mongodb_uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    db_name = os.environ.get("PREGAME_DB", "pregame")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or None
    provider = os.environ.get("PREGAME_PROVIDER", "anthropic")
    default_mode = "live" if (api_key or provider == "claude-cli") else "fake"
    llm_mode = os.environ.get("PREGAME_LLM_MODE", default_mode)
    models = {
        "drafter": os.environ.get("PREGAME_MODEL_DRAFTER", DEFAULT_MODELS["drafter"]),
        "reader": os.environ.get("PREGAME_MODEL_READER", DEFAULT_MODELS["reader"]),
        "improver": os.environ.get("PREGAME_MODEL_IMPROVER", DEFAULT_MODELS["improver"]),
    }
    cassette_path = os.environ.get("PREGAME_CASSETTE", "cassettes/demo.jsonl")
    k = int(os.environ.get("PREGAME_K", "2"))
    return Settings(
        mongodb_uri=mongodb_uri,
        db_name=db_name,
        anthropic_api_key=api_key,
        llm_mode=llm_mode,
        models=models,
        cassette_path=cassette_path,
        k=k,
        provider=provider,
        cli_concurrency=int(os.environ.get("PREGAME_CLI_CONCURRENCY", "3")),
    )


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Cached process-wide Settings, read from the environment / .env."""
    return _build_settings()
