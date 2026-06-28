"""Context-management profile schema + built-in presets + resolver.

A *profile* bundles the user's choice of:
  - tool surface (`tool_calling` or `ts_code_mode`)
  - per-technique toggles for the six context-management techniques
    listed in CONTEXT_STRATEGIES_PLAN.md

This module owns the **shape** of that profile and the rules for
resolving "which profile applies right now" (per-session override
→ user default → global built-in default). It does **not** implement
any of the actual techniques — those land in later PRs (#3 trimming,
#4 memory, #5 summarisation, #6 sliding window, #7 sub-agent) and
each one reads the relevant sub-block off the resolved profile.

Built-in presets are stored as rows with `user_id IS NULL`. They get
seeded once at startup and re-seeded idempotently on every restart so
new presets ship automatically.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ─── valid tool surfaces (mirrors api._VALID_CONTEXT_STRATEGIES) ─────────

VALID_TOOL_SURFACES = {"tool_calling", "ts_code_mode"}


# ─── per-technique config blocks ─────────────────────────────────────────

class ToolResultTrimmingCfg(BaseModel):
    enabled: bool = False
    trigger_tokens: int = Field(default=20_000, ge=1_000, le=1_000_000)
    keep_recent: int = Field(default=4, ge=0, le=200)
    exclude_tools: List[str] = Field(default_factory=list)


class SummarizationCfg(BaseModel):
    enabled: bool = False
    trigger_tokens: int = Field(default=50_000, ge=1_000, le=2_000_000)
    keep_recent: int = Field(default=6, ge=0, le=200)
    summariser_model: Optional[str] = None
    instructions: Optional[str] = None


class MemoryCfg(BaseModel):
    enabled: bool = False
    scope: str = Field(default="thread")
    auto_view_at_start: bool = True

    @field_validator("scope")
    @classmethod
    def _scope_must_be_valid(cls, v: str) -> str:
        if v not in {"thread", "user"}:
            raise ValueError("scope must be 'thread' or 'user'")
        return v


class SubagentCfg(BaseModel):
    enabled: bool = False
    max_depth: int = Field(default=1, ge=0, le=4)
    token_budget: int = Field(default=20_000, ge=1_000, le=500_000)
    parallel_limit: int = Field(default=3, ge=1, le=10)
    inherit_memory: bool = False


class JitToolsCfg(BaseModel):
    enabled: bool = False


class SlidingWindowCfg(BaseModel):
    enabled: bool = False
    keep_recent: int = Field(default=12, ge=2, le=200)


# Valid engines for relevance pruning. "judge" = LLM-as-judge only (ships now);
# "encoder" = local ONNX cross-encoder only (Phase 2); "ensemble" = both, with
# the arbitration policy reconciling disagreements.
RELEVANCE_MODES = {"judge", "encoder", "ensemble"}
RELEVANCE_ARBITRATION = {"safest", "judge_wins", "agreement_only"}


class RelevancePruningCfg(BaseModel):
    """Task-aware removal *suggestions* (see ``backend/relevance.py``).

    Unlike the mechanical techniques, this one splits the thread into episodes,
    works out the current task, and proposes which finished/unrelated episodes
    to remove. It is **suggest-only** by default (``auto_apply=False``): the
    user confirms before anything is dropped, and every choice is logged to the
    feedback file so the judge and encoder can be improved later.
    """

    enabled: bool = False
    mode: str = "judge"
    keep_recent: int = Field(default=3, ge=0, le=50)  # episodes always kept
    drop_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    summarize_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    arbitration: str = "safest"
    judge_model: Optional[str] = None  # dedicated cheap model for the audit
    encoder_path: Optional[str] = None  # local ONNX model (Phase 2)
    auto_apply: bool = False  # always suggest-only by default
    feedback_logging: bool = True

    @field_validator("mode")
    @classmethod
    def _mode_must_be_valid(cls, v: str) -> str:
        norm = (v or "judge").strip().lower()
        if norm not in RELEVANCE_MODES:
            raise ValueError(f"mode must be one of {sorted(RELEVANCE_MODES)}")
        return norm

    @field_validator("arbitration")
    @classmethod
    def _arb_must_be_valid(cls, v: str) -> str:
        norm = (v or "safest").strip().lower()
        if norm not in RELEVANCE_ARBITRATION:
            raise ValueError(
                f"arbitration must be one of {sorted(RELEVANCE_ARBITRATION)}"
            )
        return norm


# Valid modes for the image-recall technique. These are MUTUALLY EXCLUSIVE
# (the user picks at most one), which is exactly why this is a single enum
# field rather than three independent toggles — the schema makes "can't
# select two at a time" unrepresentable.
#   off         — neither caching nor eviction (raw image method as today)
#   cache       — prompt caching only: cheaper/faster, output identical
#   evict       — image eviction only: keep last K images as pixels, replace
#                 older ones with their REFERENCES + a digest (accuracy win)
#   cache_evict — both layers combined (recommended for long visual sessions)
IMAGE_RECALL_MODES = {"off", "cache", "evict", "cache_evict"}


class ImageRecallCfg(BaseModel):
    """The three new mutually-exclusive image context-management techniques.

    Pairs with the existing visual-compression pipeline (``visual_tool``):
    once a tool output has been rasterised to a PNG, this block decides how
    that image is treated as the conversation grows.

    ``keep_recent_images`` (K) only applies to the eviction paths
    (``evict`` / ``cache_evict``): the K most-recent image-bearing tool
    results stay as real pixels; anything older is collapsed to its text
    REFERENCES block + a one-line digest so the model stops re-attending to
    a pile of stale images (the multi-image accuracy problem).
    """

    mode: str = "off"
    keep_recent_images: int = Field(default=3, ge=1, le=20)
    # Caching knobs (apply to cache / cache_evict). TTL maps to Anthropic's
    # ephemeral cache_control ttl; "5m" default, "1h" for long sessions.
    cache_ttl: str = Field(default="5m")
    # Only re-cache the settled prefix every N user turns so eviction
    # rewrites don't thrash the cache (see the cost/accuracy trade-off).
    evict_batch_turns: int = Field(default=1, ge=1, le=50)

    @field_validator("mode")
    @classmethod
    def _mode_must_be_valid(cls, v: str) -> str:
        norm = (v or "off").strip().lower()
        if norm not in IMAGE_RECALL_MODES:
            raise ValueError(
                f"mode must be one of {sorted(IMAGE_RECALL_MODES)}"
            )
        return norm

    @field_validator("cache_ttl")
    @classmethod
    def _ttl_must_be_valid(cls, v: str) -> str:
        norm = (v or "5m").strip().lower()
        if norm not in {"5m", "1h"}:
            raise ValueError("cache_ttl must be '5m' or '1h'")
        return norm

    # Convenience predicates used by the orchestrator / middleware.
    @property
    def caching_enabled(self) -> bool:
        return self.mode in {"cache", "cache_evict"}

    @property
    def eviction_enabled(self) -> bool:
        return self.mode in {"evict", "cache_evict"}


VISUAL_METHOD_MODES = {"templated", "auxiliary"}


class VisualMethodCfg(BaseModel):
    """Rasterise large tool outputs into a formatted image the model reads,
    instead of raw text — trades text tokens for a (cheaper, for big outputs)
    image. Requires a vision-capable chat model.

    ``mode``:
      * ``templated``  — hand-written layouts where they exist, else fall back
        to an auxiliary LLM that writes a formatter once and caches it.
      * ``auxiliary``  — always have a small LLM generate the formatter.
    Only outputs above ``threshold_tokens`` are rasterised; smaller ones stay
    as text (an image would cost more).
    """

    enabled: bool = False
    mode: str = "templated"
    threshold_tokens: int = Field(default=500, ge=50, le=100_000)
    only_tools: List[str] = Field(default_factory=list)
    exclude_tools: List[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _mode_must_be_valid(cls, v: str) -> str:
        norm = (v or "templated").strip().lower()
        if norm not in VISUAL_METHOD_MODES:
            raise ValueError(f"mode must be one of {sorted(VISUAL_METHOD_MODES)}")
        return norm


class ContextManagementBlock(BaseModel):
    tool_result_trimming: ToolResultTrimmingCfg = Field(default_factory=ToolResultTrimmingCfg)
    summarization: SummarizationCfg = Field(default_factory=SummarizationCfg)
    memory: MemoryCfg = Field(default_factory=MemoryCfg)
    subagent: SubagentCfg = Field(default_factory=SubagentCfg)
    jit_tools: JitToolsCfg = Field(default_factory=JitToolsCfg)
    sliding_window: SlidingWindowCfg = Field(default_factory=SlidingWindowCfg)
    image_recall: ImageRecallCfg = Field(default_factory=ImageRecallCfg)
    relevance_pruning: RelevancePruningCfg = Field(default_factory=RelevancePruningCfg)
    visual_method: VisualMethodCfg = Field(default_factory=VisualMethodCfg)


class Profile(BaseModel):
    tool_surface: str = "tool_calling"
    context_management: ContextManagementBlock = Field(default_factory=ContextManagementBlock)

    @field_validator("tool_surface")
    @classmethod
    def _surface_must_be_valid(cls, v: str) -> str:
        if v not in VALID_TOOL_SURFACES:
            raise ValueError(
                f"tool_surface must be one of {sorted(VALID_TOOL_SURFACES)}"
            )
        return v

    def fingerprint(self) -> str:
        """Stable hash-input for the agent cache key. Two profiles
        that produce the same agent should produce the same string."""
        return json.dumps(self.model_dump(), sort_keys=True)


# ─── built-in presets ────────────────────────────────────────────────────

BUILTIN_PRESETS: List[Dict[str, Any]] = [
    {
        "name": "minimal",
        "is_default": True,
        "body": Profile(tool_surface="tool_calling").model_dump(),
    },
    {
        "name": "code_mode",
        "is_default": False,
        "body": Profile(tool_surface="ts_code_mode").model_dump(),
    },
    {
        "name": "long_chat",
        "is_default": False,
        "body": Profile(
            tool_surface="tool_calling",
            context_management=ContextManagementBlock(
                tool_result_trimming=ToolResultTrimmingCfg(enabled=True),
                summarization=SummarizationCfg(enabled=True),
                jit_tools=JitToolsCfg(enabled=True),
            ),
        ).model_dump(),
    },
    {
        "name": "power_research",
        "is_default": False,
        "body": Profile(
            tool_surface="ts_code_mode",
            context_management=ContextManagementBlock(
                tool_result_trimming=ToolResultTrimmingCfg(enabled=True),
                summarization=SummarizationCfg(enabled=True),
                memory=MemoryCfg(enabled=True),
                subagent=SubagentCfg(enabled=True),
                jit_tools=JitToolsCfg(enabled=True),
            ),
        ).model_dump(),
    },
    {
        "name": "cheap_long",
        "is_default": False,
        "body": Profile(
            tool_surface="tool_calling",
            context_management=ContextManagementBlock(
                tool_result_trimming=ToolResultTrimmingCfg(enabled=True),
                sliding_window=SlidingWindowCfg(enabled=True, keep_recent=12),
                jit_tools=JitToolsCfg(enabled=True),
            ),
        ).model_dump(),
    },
    {
        "name": "visual_recall",
        "is_default": False,
        "body": Profile(
            tool_surface="tool_calling",
            context_management=ContextManagementBlock(
                # Combined image-recall: keep the last 3 images as pixels,
                # digest older ones, and prompt-cache the settled prefix.
                image_recall=ImageRecallCfg(
                    mode="cache_evict", keep_recent_images=3
                ),
                tool_result_trimming=ToolResultTrimmingCfg(enabled=True),
            ),
        ).model_dump(),
    },
    {
        "name": "auto_suggest",
        "is_default": False,
        "body": Profile(
            tool_surface="tool_calling",
            context_management=ContextManagementBlock(
                # Task-aware removal suggestions via LLM-as-judge. Suggest-only:
                # nothing is dropped without the user confirming.
                relevance_pruning=RelevancePruningCfg(enabled=True, mode="judge"),
            ),
        ).model_dump(),
    },
    {
        "name": "visual_method",
        "is_default": False,
        "body": Profile(
            tool_surface="tool_calling",
            context_management=ContextManagementBlock(
                # Rasterise large tool outputs into a formatted image the model
                # reads instead of text. Needs a vision-capable chat model.
                visual_method=VisualMethodCfg(
                    enabled=True, mode="templated", threshold_tokens=500
                ),
            ),
        ).model_dump(),
    },
]

# One-line UI descriptions; ships in the GET /context/profiles payload.
PRESET_SUMMARY: Dict[str, str] = {
    "minimal": "Today's default: classic tool calling, no extras. Best for short, surgical chats.",
    "code_mode": "TypeScript Code Mode — one program per turn instead of N round trips. Best for chained tool work.",
    "long_chat": "Tool calling + tool-result trimming + summarisation. Best for long debugging / Q&A sessions.",
    "power_research": "Code Mode + trimming + summarisation + memory + sub-agents. Best for deep research.",
    "cheap_long": "Tool calling + trimming + sliding window. Cheap fallback for tiny-context models.",
    "visual_recall": "Image-recall: keep the last 3 tool images as pixels, digest older ones, and prompt-cache the settled prefix. Best for long, image-heavy tool sessions.",
    "auto_suggest": "Task-aware cleanup: an LLM auditor splits the chat into episodes and suggests which finished/unrelated ones to remove. Suggest-only — you confirm each removal.",
    "visual_method": "Rasterise large tool outputs into a formatted image the model reads instead of text — saves tokens on big/noisy outputs. Requires a vision-capable model.",
}

DEFAULT_PRESET_NAME = "minimal"


# ─── seed at startup (idempotent) ────────────────────────────────────────


def seed_builtin_presets(conn) -> None:
    """Insert any built-in preset that isn't already present. Re-running
    is a no-op. Always updates the `body` of existing built-ins so
    adding fields to a preset doesn't strand old DBs.
    """
    for preset in BUILTIN_PRESETS:
        existing = conn.execute(
            "SELECT id FROM context_profiles WHERE user_id IS NULL AND name = %s",
            (preset["name"],),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO context_profiles (user_id, name, body, is_default)
                VALUES (NULL, %s, %s::jsonb, %s)
                """,
                (preset["name"], json.dumps(preset["body"]), preset["is_default"]),
            )
        else:
            # Refresh body + default flag so schema upgrades flow through.
            conn.execute(
                """
                UPDATE context_profiles
                   SET body = %s::jsonb, is_default = %s, updated_at = NOW()
                 WHERE id = %s
                """,
                (json.dumps(preset["body"]), preset["is_default"], existing[0]),
            )


# ─── resolution: which profile applies to *this* chat turn? ─────────────


def parse_profile(body: Optional[Dict[str, Any]]) -> Profile:
    """Pydantic-validate a raw dict into a Profile. Missing keys fall
    back to defaults defined in the schema."""
    if not body:
        return Profile()
    return Profile.model_validate(body)


def _row_to_profile(row: tuple) -> Profile:
    body = row[2] if not isinstance(row[2], str) else json.loads(row[2])
    return parse_profile(body)


def resolve_profile(
    conn,
    *,
    user_id: str,
    session_id: Optional[str] = None,
    request_profile_id: Optional[str] = None,
    request_profile_body: Optional[Dict[str, Any]] = None,
    legacy_strategy: Optional[str] = None,
) -> tuple[Profile, str]:
    """Pick the active profile for this turn.

    Order:
      1. ``request_profile_body`` (one-off override, never persisted).
      2. ``request_profile_id`` (a saved profile by id).
      3. ``legacy_strategy`` ("tool_calling" / "ts_code_mode") — back-
         compat for clients that haven't migrated yet; maps to the
         matching built-in preset.
      4. session.context_profile_id, if the session has one.
      5. The user's default profile (any row with user_id = user_id
         and is_default = true).
      6. Built-in default (`minimal`).

    Returns ``(profile, display_name)``. `display_name` is something
    like "minimal" / "long_chat" / "(custom)" — handy for log lines
    and the cache key.
    """
    # 1. one-off body override
    if request_profile_body:
        return parse_profile(request_profile_body), "(custom)"

    # 2. saved profile by id (must belong to caller or be a built-in)
    if request_profile_id:
        row = conn.execute(
            """
            SELECT id, name, body
              FROM context_profiles
             WHERE id = %s AND (user_id = %s OR user_id IS NULL)
            """,
            (request_profile_id, user_id),
        ).fetchone()
        if row is not None:
            return _row_to_profile(row), row[1]

    # 3. legacy strategy string from ChatRequest.context_strategy
    if legacy_strategy:
        norm = (legacy_strategy or "").strip().lower()
        # Map the two existing surface strings to the matching preset.
        preset_for_surface = {
            "tool_calling": "minimal",
            "ts_code_mode": "code_mode",
        }.get(norm)
        if preset_for_surface:
            row = conn.execute(
                "SELECT id, name, body FROM context_profiles "
                "WHERE user_id IS NULL AND name = %s",
                (preset_for_surface,),
            ).fetchone()
            if row is not None:
                return _row_to_profile(row), row[1]

    # 4. per-session override
    if session_id:
        row = conn.execute(
            """
            SELECT cp.id, cp.name, cp.body
              FROM sessions s
              JOIN context_profiles cp ON cp.id = s.context_profile_id
             WHERE s.id = %s AND s.user_id = %s
            """,
            (session_id, user_id),
        ).fetchone()
        if row is not None:
            return _row_to_profile(row), row[1]

    # 5. user default
    row = conn.execute(
        "SELECT id, name, body FROM context_profiles "
        "WHERE user_id = %s AND is_default = true LIMIT 1",
        (user_id,),
    ).fetchone()
    if row is not None:
        return _row_to_profile(row), row[1]

    # 6. built-in default
    row = conn.execute(
        "SELECT id, name, body FROM context_profiles "
        "WHERE user_id IS NULL AND name = %s",
        (DEFAULT_PRESET_NAME,),
    ).fetchone()
    if row is not None:
        return _row_to_profile(row), row[1]

    # Last-resort: schema defaults (should never hit if seed_builtin_presets ran).
    return Profile(), DEFAULT_PRESET_NAME


def list_profiles_for_user(conn, user_id: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id::text, user_id::text, name, body, is_default, created_at, updated_at
          FROM context_profiles
         WHERE user_id IS NULL OR user_id = %s
         ORDER BY user_id NULLS FIRST, name
        """,
        (user_id,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        body = r[3] if not isinstance(r[3], str) else json.loads(r[3])
        out.append(
            {
                "id": r[0],
                "user_id": r[1],
                "name": r[2],
                "body": body,
                "is_default": bool(r[4]),
                "built_in": r[1] is None,
                "summary": PRESET_SUMMARY.get(r[2]) if r[1] is None else None,
                "created_at": r[5].isoformat() if r[5] else None,
                "updated_at": r[6].isoformat() if r[6] else None,
            }
        )
    return out


def get_profile_by_id(
    conn, user_id: str, profile_id: str
) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id::text, user_id::text, name, body, is_default, created_at, updated_at
          FROM context_profiles
         WHERE id = %s AND (user_id = %s OR user_id IS NULL)
        """,
        (profile_id, user_id),
    ).fetchone()
    if row is None:
        return None
    body = row[3] if not isinstance(row[3], str) else json.loads(row[3])
    return {
        "id": row[0],
        "user_id": row[1],
        "name": row[2],
        "body": body,
        "is_default": bool(row[4]),
        "built_in": row[1] is None,
        "summary": PRESET_SUMMARY.get(row[2]) if row[1] is None else None,
        "created_at": row[5].isoformat() if row[5] else None,
        "updated_at": row[6].isoformat() if row[6] else None,
    }
