"""Runtime settings for the gateway.

Two kinds of config:
  * **Environment** — where the real model lives + where we listen. Held in
    :class:`Settings`, read once at startup.
  * **Profile** — which techniques are on. That's the website's ``Profile``
    schema, loaded from a JSON file so a user (or an IDE settings panel) can
    edit it without touching code. Reloaded on every request so edits take
    effect live.

The profile file path resolution order:
  1. ``ACM_CONFIG`` env var, if set.
  2. ``extension/config/acm.config.json`` (the user's copy).
  3. ``extension/config/acm.config.example.json`` (the shipped default).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from acm_engine import Profile, parse_profile

_EXT_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_DIR = _EXT_ROOT / "config"


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    upstream_base_url: str
    upstream_api_key: Optional[str]
    # Anthropic-native upstream (for the /v1/messages surface — Claude Code).
    anthropic_base_url: str
    anthropic_api_key: Optional[str]
    anthropic_version: str
    # How the Anthropic surface authenticates upstream:
    #   * "auto"        — if the IDE sends its own OAuth bearer (a Claude
    #                     subscription session) and no Anthropic provider is
    #                     explicitly selected, forward that bearer (bills the
    #                     subscription); otherwise inject our x-api-key.
    #   * "passthrough" — always forward the client's bearer (subscription-only).
    #   * "api_key"     — always inject x-api-key (the original behaviour).
    anthropic_auth_mode: str
    # Dedicated cheap/fast model for the relevance auditor (LLM-as-judge). Kept
    # separate from the chat model so audits stay cheap and don't compete with
    # the user's main model. A profile's ``judge_model`` overrides this per use.
    judge_model: str
    # Optional local cross-encoder for relevance scoring (Provence-style ONNX or
    # a sentence-transformers model). Empty -> the encoder falls back to its
    # dependency-free lexical backend, so ensemble mode still works.
    encoder_path: str
    config_path: Path
    log_events: bool

    @classmethod
    def from_env(cls) -> "Settings":
        # Load a .env so a plain `acm-gateway` picks up keys without a manual
        # export. Search from the cwd upward, then the repo root next to
        # extension/. Real env vars win (override=False). No-op if python-dotenv
        # is missing or no .env exists.
        try:
            from dotenv import find_dotenv, load_dotenv

            found = find_dotenv(usecwd=True)
            if found:
                load_dotenv(found)
            repo_env = _EXT_ROOT.parent / ".env"
            if repo_env.is_file():
                load_dotenv(repo_env)
        except Exception:
            pass
        return cls(
            host=os.getenv("ACM_HOST", "127.0.0.1"),
            port=int(os.getenv("ACM_PORT", "8807")),
            # Default upstream is OpenRouter — matches the website's zero-config
            # path. Override for OpenAI/Anthropic/Azure/etc.
            upstream_base_url=os.getenv(
                "ACM_UPSTREAM_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            upstream_api_key=os.getenv("ACM_UPSTREAM_API_KEY")
            or os.getenv("OPENROUTER_API_KEY"),
            anthropic_base_url=os.getenv(
                "ACM_ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"
            ).rstrip("/"),
            anthropic_api_key=os.getenv("ACM_ANTHROPIC_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY"),
            anthropic_version=os.getenv("ACM_ANTHROPIC_VERSION", "2023-06-01"),
            anthropic_auth_mode=os.getenv(
                "ACM_ANTHROPIC_AUTH_MODE", "auto"
            ).strip().lower()
            or "auto",
            judge_model=os.getenv("ACM_JUDGE_MODEL", "openai/gpt-4o-mini"),
            encoder_path=os.getenv("ACM_ENCODER_PATH", ""),
            config_path=_resolve_config_path(),
            log_events=os.getenv("ACM_LOG_EVENTS", "1") not in {"0", "false", "no"},
        )


def user_config_path() -> Path:
    """The writable user config copy. ``set_profile`` always writes here, even
    when the gateway is currently reading the shipped example."""
    env = os.getenv("ACM_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return _CONFIG_DIR / "acm.config.json"


def active_config_path() -> Path:
    """The config path to *read* right now — re-resolved per call so edits and
    ``set_profile`` writes take effect live. Prefers the user copy; falls back
    to the shipped example."""
    user_copy = user_config_path()
    if user_copy.is_file():
        return user_copy
    return _CONFIG_DIR / "acm.config.example.json"


def _resolve_config_path() -> Path:
    return active_config_path()


def load_visual_cfg(path: Path) -> dict:
    """Read the gateway-specific ``visual_method`` block from the raw config.

    It lives alongside the web ``Profile`` keys but isn't part of the Profile
    schema (the website applies the visual method via a separate axis), so we
    read it straight from the JSON. Shape:
        {"enabled": bool, "trigger_tokens": int,
         "only_tools": [str], "exclude_tools": [str]}
    """
    default = {
        "enabled": False,
        "trigger_tokens": 500,
        "only_tools": [],
        "exclude_tools": [],
    }
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    block = raw.get("visual_method") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return default
    return {**default, **block}


def load_profile(path: Path) -> Profile:
    """Read the JSON config file and validate it into a ``Profile``. Falls back
    to the schema defaults if the file is missing or malformed so a bad edit
    degrades to 'no techniques' instead of a 500."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Profile()
    except json.JSONDecodeError as e:
        print(f"[acm-gateway] bad config {path}: {e} — using defaults", flush=True)
        return Profile()
    # Drop comment keys before validation.
    if isinstance(raw, dict):
        raw = {k: v for k, v in raw.items() if not k.startswith("_")}
    try:
        return parse_profile(raw)
    except Exception as e:  # pydantic ValidationError etc.
        print(f"[acm-gateway] invalid profile {path}: {e} — using defaults", flush=True)
        return Profile()
