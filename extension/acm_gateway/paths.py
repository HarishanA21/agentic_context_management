"""Central home for the gateway's on-disk state and its path env vars.

Before this, every module resolved its own ``ACM_*_PATH`` from ``os.getenv`` and
wrote JSON with a bare ``write_text`` — so the env vars were undocumented sprawl
and a crash mid-write could leave a half-written (corrupt) state file. This
module is the one place that:

  * resolves the state directory (``ACM_HOME``, default ``~/.acm``) and every
    file under it, so the full set of path overrides is visible in one place;
  * offers :func:`atomic_write_text` — write to a temp file in the same dir then
    ``os.replace`` (atomic on POSIX + Windows), so a reader never sees a partial
    file and a crash leaves the previous good copy intact.

Env vars (all optional; defaults shown):

  ACM_HOME                    ~/.acm                     state directory
  ACM_DROPLIST_PATH           $ACM_HOME/dropped.json     manual drop-list
  ACM_SUMMARY_PATH            $ACM_HOME/summaries.json   injected summary notes
  ACM_CONTEXT_WINDOWS_PATH    $ACM_HOME/context_windows.json  per-chat profiles
  ACM_PROVIDERS_PATH          $ACM_HOME/providers.json   configured providers
  ACM_SAVINGS_PATH            $ACM_HOME/savings.json     freed-token ledger
  ACM_TRAINING_EXPORT_DIR     $ACM_HOME/training         trainer file output
  ACM_RELEVANCE_FEEDBACK_PATH $ACM_HOME/relevance_feedback.jsonl  (engine)
  ACM_RELEVANCE_AUDIT_PATH    $ACM_HOME/relevance_audits.jsonl    (engine)

The two ``ACM_RELEVANCE_*`` vars are read inside ``acm_engine``; they are listed
here for a complete map but resolved there.

Non-path gateway tunables (resolved in ``app.py`` / ``config.py``, listed here
so the full env surface has one reference point):

  ACM_CONTEXT_BUDGET       128000   soft token ceiling per chat; 0 disables
  ACM_CONTEXT_BUDGET_WARN  0.8      warn once a chat passes this fraction
  ACM_COST_PER_MTOK        0        $/1M tokens, for the savings dashboard
  ACM_HOST / ACM_PORT               where the gateway listens
  ACM_UPSTREAM_* / ACM_ANTHROPIC_*  upstream provider + auth (see config.py)
  ACM_JUDGE_MODEL / ACM_ENCODER_PATH  relevance-pruning models (see config.py)
  ACM_LOG_EVENTS           1        log fired technique events to stdout
"""

from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    return Path(os.getenv("ACM_HOME", str(Path.home() / ".acm")))


def _path(env: str, filename: str) -> Path:
    """A state file: explicit env override wins, else ``$ACM_HOME/filename``."""
    override = os.getenv(env)
    return Path(override) if override else _home() / filename


# Resolved once at import — the gateway reads env at startup like Settings does.
DROPLIST_PATH = _path("ACM_DROPLIST_PATH", "dropped.json")
SUMMARY_PATH = _path("ACM_SUMMARY_PATH", "summaries.json")
CONTEXT_WINDOWS_PATH = _path("ACM_CONTEXT_WINDOWS_PATH", "context_windows.json")
PROVIDERS_PATH = _path("ACM_PROVIDERS_PATH", "providers.json")
SAVINGS_PATH = _path("ACM_SAVINGS_PATH", "savings.json")
TRAINING_EXPORT_DIR = _path("ACM_TRAINING_EXPORT_DIR", "training")
# Real-usage evaluation ledger + the OpenRouter price cache that turns it into $.
USAGE_PATH = _path("ACM_USAGE_PATH", "usage.json")
PRICING_CACHE_PATH = _path("ACM_PRICING_CACHE_PATH", "pricing.json")
# The user's spend cap (daily USD budget + soft/hard enforcement).
BUDGETS_PATH = _path("ACM_BUDGETS_PATH", "budgets.json")


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: temp file in the same directory
    then ``os.replace``. A crash mid-write leaves the prior good file intact."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        # Best-effort cleanup if replace never happened (e.g. write failed).
        try:
            tmp.unlink()
        except OSError:
            pass
