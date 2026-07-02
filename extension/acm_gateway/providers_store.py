"""Multi-provider routing for the gateway.

The website lets a user wire up several LLM providers (OpenAI, OpenRouter,
Anthropic, Azure, Google, Bedrock) and pick one per session. This is the gateway
port: a local registry of configured providers + credentials, and a router that
picks the right one per request and builds the exact URL / auth headers / model
to forward.

What's covered, and honestly what isn't:
  * **OpenAI-compatible** — ``openai``, ``openrouter``, ``google`` (via its
    ``/openai/`` compat path), and ``azure`` (deployment in the path,
    ``api-key`` header, ``api-version`` query). These all speak the OpenAI
    ``/chat/completions`` shape, so no response translation is needed — the
    gateway just points at the right base URL with the right auth.
  * **anthropic** — native ``/v1/messages`` surface (x-api-key auth).
  * **bedrock** — needs AWS SigV4 signing, which a plain HTTP proxy can't do.
    Configure Bedrock models *through OpenRouter* instead (slug ``openrouter``).
    Documented, not silently broken.

Routing order for a request:
  1. ``x-acm-provider`` header naming a configured slug.
  2. A ``slug/model`` (or ``slug:model``) prefix where ``slug`` is configured.
  3. The configured default provider.
  4. Env fallback (``ACM_UPSTREAM_*`` / ``ACM_ANTHROPIC_*``) — preserves the
     single-upstream behaviour when no providers.json exists.

Credentials live in ``~/.acm/providers.json`` (file mode 0600). TODO(acm): move
secrets to the OS keychain; the website uses Fernet-encrypted DB storage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from .paths import PROVIDERS_PATH as _DEFAULT_PATH
from .paths import atomic_write_text

# Default API bases for each OpenAI-compatible provider type.
_DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
}

OPENAI_LIKE = {"openai", "openrouter", "google", "custom"}
KNOWN_TYPES = OPENAI_LIKE | {"azure", "anthropic"}


@dataclass
class Target:
    """A resolved forwarding target. ``kind`` is 'openai' (forward on the
    OpenAI surface) or 'anthropic' (forward on the Messages surface)."""

    kind: str
    model: str
    slug: str
    # openai-kind:
    url: str = ""
    headers: Optional[Dict[str, str]] = None
    # anthropic-kind:
    base_url: str = ""
    api_key: Optional[str] = None


class ProviderStore:
    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self.path = path
        self._data = self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> Dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"default": None, "providers": {}}

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ── management ───────────────────────────────────────────────────────
    def list(self, *, mask: bool = True) -> Dict:
        """Configured providers (keys masked by default), plus the default."""
        out = {}
        for slug, cfg in self._data.get("providers", {}).items():
            c = dict(cfg)
            if mask and c.get("api_key"):
                k = c["api_key"]
                c["api_key"] = (k[:4] + "…" + k[-2:]) if len(k) > 7 else "••••"
            out[slug] = c
        return {"default": self._data.get("default"), "providers": out}

    def upsert(self, slug: str, cfg: Dict) -> None:
        t = (cfg.get("type") or "").lower()
        if t not in KNOWN_TYPES:
            raise ValueError(f"unknown provider type '{t}' (choose {sorted(KNOWN_TYPES)})")
        providers = self._data.setdefault("providers", {})
        providers[slug] = {k: v for k, v in cfg.items() if k != "default"}
        providers[slug]["type"] = t
        if cfg.get("default") or self._data.get("default") is None:
            self._data["default"] = slug
        self._save()

    def delete(self, slug: str) -> bool:
        providers = self._data.get("providers", {})
        if slug not in providers:
            return False
        providers.pop(slug)
        if self._data.get("default") == slug:
            self._data["default"] = next(iter(providers), None)
        self._save()
        return True

    def set_default(self, slug: str) -> bool:
        if slug not in self._data.get("providers", {}):
            return False
        self._data["default"] = slug
        self._save()
        return True

    # ── routing ──────────────────────────────────────────────────────────
    def _route(self, model: str, hint: Optional[str]) -> Tuple[Optional[str], str]:
        providers = self._data.get("providers", {})
        model = model or ""
        if hint and hint in providers:
            real = model[len(hint) + 1:] if model.startswith(hint + "/") else model
            return hint, real
        for sep in ("/", ":"):
            if sep in model:
                prefix = model.split(sep, 1)[0]
                if prefix in providers:
                    return prefix, model.split(sep, 1)[1]
        return self._data.get("default"), model

    def resolve(
        self,
        model: str,
        *,
        provider_hint: Optional[str] = None,
        env_openai: Optional[Tuple[str, Optional[str]]] = None,
        env_anthropic: Optional[Tuple[str, Optional[str]]] = None,
    ) -> Target:
        """Resolve a request to a forwarding Target. Falls back to the env
        single-upstream when nothing is configured."""
        slug, real_model = self._route(model, provider_hint)
        cfg = self._data.get("providers", {}).get(slug) if slug else None

        if cfg is None:
            # Env fallback — preserves the original single-upstream behaviour.
            base, key = env_openai or ("https://openrouter.ai/api/v1", None)
            return Target(
                kind="openai",
                model=real_model,
                slug=slug or "env",
                url=base.rstrip("/") + "/chat/completions",
                headers=_bearer(key),
            )

        t = cfg["type"]
        key = cfg.get("api_key")
        if t in OPENAI_LIKE:
            base = (cfg.get("base_url") or _DEFAULT_BASE.get(t) or _DEFAULT_BASE["openai"]).rstrip("/")
            headers = _bearer(key)
            if cfg.get("organization"):
                headers["OpenAI-Organization"] = cfg["organization"]
            return Target(kind="openai", model=real_model, slug=slug, url=base + "/chat/completions", headers=headers)
        if t == "azure":
            endpoint = (cfg.get("azure_endpoint") or "").rstrip("/")
            api_version = cfg.get("api_version") or "2024-10-21"
            url = f"{endpoint}/openai/deployments/{real_model}/chat/completions?api-version={api_version}"
            return Target(kind="openai", model=real_model, slug=slug, url=url, headers={"api-key": key or ""})
        if t == "anthropic":
            base = (cfg.get("base_url") or (env_anthropic or ("https://api.anthropic.com/v1", None))[0]).rstrip("/")
            return Target(kind="anthropic", model=real_model, slug=slug, base_url=base, api_key=key)
        # Shouldn't happen (upsert validates), but stay safe.
        base, k = env_openai or ("https://openrouter.ai/api/v1", None)
        return Target(kind="openai", model=real_model, slug=slug or "env", url=base.rstrip("/") + "/chat/completions", headers=_bearer(k))


def _bearer(key: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {key}"} if key else {}
