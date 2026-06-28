"""Relevance pruning — suggest which past *episodes* are unnecessary *now*.

The other techniques in :mod:`context_editing` edit context by mechanical rules
(token thresholds, recency). This one is *task-aware*: it splits the thread into
**episodes** (one user-led turn + its assistant/tool replies), figures out the
*current* task, and proposes which finished/unrelated episodes can be removed —
exactly the "feature 1.1–1.3 are done, 1.4 is unrelated, 1.5 is current" case.

Two engines sit behind one interface so we can ship both and collapse to one
later (see ``RelevancePruningCfg.mode``):

  * :class:`JudgeSuggester`  — one cheap LLM call returns KEEP/SUMMARIZE/DROP
    + a one-line reason per episode. Shipped now.
  * ``EncoderSuggester``     — (Phase 2) a local ONNX cross-encoder, Provence
    style. Not in this module yet; :class:`EnsembleSuggester` already accepts it.

This module is **pure**: it takes a ``List[BaseMessage]`` and returns
:class:`Suggestion` objects keyed by episode + the member message indices. It
*never* removes anything and never touches a LangGraph agent or the gateway
drop-list. The caller maps an accepted suggestion to concrete removals
(web: ``RemoveMessage``; gateway: drop-list fingerprints) and logs the user's
choice via :func:`record_feedback` — that one log is the dataset both the
encoder re-train and the judge DPO loop read later.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

# Reuse the engine's shared helpers so token counts + multimodal flattening
# match every other technique. Both modules are vendored together, so this
# import resolves in the backend and inside acm_engine/_vendor alike.
from context_editing import _flatten_content_for_text, _rough_tokens

VALID_LABELS = ("KEEP", "SUMMARIZE", "DROP")
VALID_SOURCES = ("encoder", "judge", "ensemble", "rule")

# Label safety order: when two engines disagree, the *safer* (more conservative)
# label wins under arbitration="safest". A missed removal is cheap; a wrong
# removal is expensive, so KEEP outranks SUMMARIZE outranks DROP.
_SAFETY_RANK = {"KEEP": 0, "SUMMARIZE": 1, "DROP": 2}


# ─── data shapes ─────────────────────────────────────────────────────────


class Episode:
    """One user-led turn and the assistant/tool messages that answered it.

    ``member_indices`` are absolute positions in the ``messages`` list passed to
    :func:`segment_into_episodes` — the caller uses them to map an accepted
    suggestion back to concrete messages (fingerprints / ids) to remove.
    """

    __slots__ = (
        "id",
        "index",
        "member_indices",
        "title",
        "text",
        "tokens",
        "is_recent",
        "protected",
    )

    def __init__(
        self,
        *,
        id: str,
        index: int,
        member_indices: List[int],
        title: str,
        text: str,
        tokens: int,
        is_recent: bool = False,
        protected: bool = False,
    ) -> None:
        self.id = id
        self.index = index
        self.member_indices = member_indices
        self.title = title
        self.text = text
        self.tokens = tokens
        self.is_recent = is_recent
        self.protected = protected

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "member_indices": list(self.member_indices),
            "title": self.title,
            "tokens": self.tokens,
            "is_recent": self.is_recent,
            "protected": self.protected,
        }


class Suggestion:
    """A per-episode recommendation the UI renders as a card."""

    __slots__ = (
        "episode_id",
        "episode_index",
        "label",
        "score",
        "reason",
        "source",
        "freed_tokens",
        "member_indices",
        "title",
    )

    def __init__(
        self,
        *,
        episode_id: str,
        episode_index: int,
        label: str,
        score: float,
        reason: str,
        source: str,
        freed_tokens: int,
        member_indices: List[int],
        title: str = "",
    ) -> None:
        self.episode_id = episode_id
        self.episode_index = episode_index
        self.label = label if label in VALID_LABELS else "KEEP"
        self.score = max(0.0, min(1.0, float(score)))
        self.reason = reason or ""
        self.source = source if source in VALID_SOURCES else "rule"
        self.freed_tokens = max(0, int(freed_tokens))
        self.member_indices = member_indices
        self.title = title

    def to_dict(self) -> Dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "episode_index": self.episode_index,
            "label": self.label,
            "score": round(self.score, 3),
            "reason": self.reason,
            "source": self.source,
            "freed_tokens": self.freed_tokens,
            "member_indices": list(self.member_indices),
            "title": self.title,
        }


# ─── segmentation ────────────────────────────────────────────────────────


def _msg_text(msg: BaseMessage) -> str:
    content = getattr(msg, "content", "") or ""
    if not isinstance(content, str):
        content = _flatten_content_for_text(content)
    return content


def _role(msg: BaseMessage) -> str:
    return getattr(msg, "type", None) or msg.__class__.__name__.lower()


def _first_line(text: str, limit: int = 80) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return (line[:limit] + "…") if len(line) > limit else line


def segment_into_episodes(messages: List[BaseMessage]) -> List[Episode]:
    """Split a thread into episodes, one per user turn.

    Leading/standalone ``SystemMessage``s are deliberately **excluded** — the
    system prompt is never a removal candidate, so it never appears in any
    episode's ``member_indices``. Everything from a ``HumanMessage`` up to (but
    not including) the next ``HumanMessage`` forms one episode.
    """
    episodes: List[Episode] = []
    cur_indices: List[int] = []
    cur_title = ""

    def _flush() -> None:
        nonlocal cur_indices, cur_title
        if not cur_indices:
            return
        idx = len(episodes)
        text = "\n".join(
            f"[{_role(messages[i])}] {_msg_text(messages[i])}" for i in cur_indices
        )
        tokens = sum(_rough_tokens(_msg_text(messages[i])) for i in cur_indices)
        episodes.append(
            Episode(
                id=f"ep{idx}",
                index=idx,
                member_indices=list(cur_indices),
                title=cur_title or "(untitled turn)",
                text=text,
                tokens=tokens,
            )
        )
        cur_indices = []
        cur_title = ""

    for i, m in enumerate(messages):
        if isinstance(m, SystemMessage):
            # Not part of any episode; never a removal candidate.
            continue
        if isinstance(m, HumanMessage):
            _flush()
            cur_title = _first_line(_msg_text(m))
        cur_indices.append(i)
    _flush()
    return episodes


def active_task(messages: List[BaseMessage]) -> str:
    """The current goal = the text of the most recent user message."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _msg_text(m).strip()
    return ""


def _mark_protection(episodes: List[Episode], keep_recent: int) -> None:
    """Flag the active + most-recent episodes so they're never suggested for
    removal. The last episode is the current task; the previous ``keep_recent``
    are kept for immediate coherence."""
    n = len(episodes)
    for ep in episodes:
        ep.is_recent = ep.index >= n - max(0, keep_recent) - 1
        ep.protected = ep.index == n - 1  # the active task itself
    if episodes:
        episodes[-1].protected = True


# ─── the judge (LLM) ─────────────────────────────────────────────────────


JUDGE_SYSTEM = (
    "You are a context auditor for a coding/agent chat. The conversation is "
    "split into numbered EPISODES (one user request + the replies to it). The "
    "user is CURRENTLY working on a specific task. For each episode decide "
    "whether it is still needed for the current work.\n\n"
    "Labels:\n"
    "  KEEP      — still relevant, or load-bearing background (a decision, a "
    "constraint, an identifier still in use).\n"
    "  SUMMARIZE — finished but worth a one-line trace; safe to compress.\n"
    "  DROP      — finished AND unrelated to the current task (e.g. a solved "
    "bug on different files), safe to remove entirely.\n\n"
    "Rules:\n"
    "  - When unsure, prefer KEEP over SUMMARIZE over DROP. A wrong DROP is "
    "much worse than a missed one.\n"
    "  - Only DROP an episode that is clearly closed and off-topic.\n"
    "  - Give a short, concrete reason (name the files/topic).\n\n"
    "Return ONLY a JSON array, no prose:\n"
    '[{"episode_id":"ep0","label":"DROP","reason":"...","confidence":0.0-1.0}]'
)


def _render_episodes_for_judge(episodes: List[Episode], snippet_chars: int = 600) -> str:
    lines: List[str] = []
    for ep in episodes:
        body = ep.text.strip().replace("\n", " ")
        if len(body) > snippet_chars:
            body = body[:snippet_chars] + " […]"
        lines.append(f'episode_id={ep.id} | ~{ep.tokens} tok | "{ep.title}"\n  {body}')
    return "\n\n".join(lines)


_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def parse_judge_json(text: str) -> Dict[str, Dict[str, Any]]:
    """Pull ``{episode_id: {label, reason, confidence}}`` out of the model's
    reply, tolerating ```json fences and leading/trailing prose."""
    if not text:
        return {}
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("\n") + 1 :] if "\n" in raw else raw
    m = _JSON_ARRAY_RE.search(raw)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(arr, list):
        return {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("episode_id") or "").strip()
        if not eid:
            continue
        label = str(item.get("label") or "KEEP").strip().upper()
        if label not in VALID_LABELS:
            label = "KEEP"
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out[eid] = {
            "label": label,
            "reason": str(item.get("reason") or "").strip(),
            "confidence": max(0.0, min(1.0, conf)),
        }
    return out


class SuggesterClient(Protocol):
    """Minimal chat client the judge needs: one call, returns ``.content``.
    The gateway's ``SummariserClient`` / ``AnthropicSummariser`` already match."""

    def invoke(self, messages: List[BaseMessage]) -> Any: ...


class Suggester(Protocol):
    def suggest(
        self, episodes: List[Episode], task: str
    ) -> List[Suggestion]: ...


_FEWSHOT_PATH = Path(
    os.getenv("ACM_JUDGE_FEWSHOT_PATH", str(Path.home() / ".acm" / "judge_fewshot.json"))
)


def _load_fewshot(limit: int = 5) -> str:
    """Few-shot block for the judge prompt, built by ``train_judge.py`` from the
    hardest user corrections. Empty string when the file is absent. This is the
    zero-training (Tier-1) improvement path — yesterday's mistakes teach today's
    judge without touching weights."""
    try:
        data = json.loads(_FEWSHOT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    items = data.get("examples", data) if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        return ""
    lines: List[str] = []
    for ex in items[:limit]:
        if not isinstance(ex, dict):
            continue
        task = str(ex.get("task", ""))[:200]
        ep = str(ex.get("episode_text", ex.get("title", "")))[:300]
        label = str(ex.get("label", "")).upper()
        reason = str(ex.get("reason", ""))[:160]
        lines.append(
            f'- task: "{task}"\n  episode: "{ep}"\n  correct: {label} — {reason}'
        )
    if not lines:
        return ""
    return (
        "\nLearn from these past corrections (the user overrode the model here):\n"
        + "\n".join(lines)
        + "\n"
    )


class JudgeSuggester:
    """LLM-as-judge engine: label every candidate episode in one call."""

    def __init__(self, client: SuggesterClient, *, use_fewshot: bool = True) -> None:
        self.client = client
        self.use_fewshot = use_fewshot

    def suggest(self, episodes: List[Episode], task: str) -> List[Suggestion]:
        if not episodes:
            return []
        fewshot = _load_fewshot() if self.use_fewshot else ""
        user = (
            f"The user is CURRENTLY working on:\n{task or '(unknown)'}\n"
            f"{fewshot}\n"
            f"Episodes (oldest first):\n\n{_render_episodes_for_judge(episodes)}"
        )
        try:
            resp = self.client.invoke(
                [
                    SystemMessage(content=JUDGE_SYSTEM),
                    HumanMessage(content=user),
                ]
            )
        except Exception as e:  # never let a judge hiccup break the caller
            return [_keep(ep, "judge", f"judge unavailable: {type(e).__name__}") for ep in episodes]
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        labels = parse_judge_json(content)
        out: List[Suggestion] = []
        for ep in episodes:
            verdict = labels.get(ep.id)
            if verdict is None:
                out.append(_keep(ep, "judge", "no verdict — kept by default"))
                continue
            out.append(
                Suggestion(
                    episode_id=ep.id,
                    episode_index=ep.index,
                    label=verdict["label"],
                    score=verdict["confidence"],
                    reason=verdict["reason"],
                    source="judge",
                    freed_tokens=_freed(ep, verdict["label"]),
                    member_indices=ep.member_indices,
                    title=ep.title,
                )
            )
        return out


def _keep(ep: Episode, source: str, reason: str) -> Suggestion:
    return Suggestion(
        episode_id=ep.id,
        episode_index=ep.index,
        label="KEEP",
        score=1.0,
        reason=reason,
        source=source,
        freed_tokens=0,
        member_indices=ep.member_indices,
        title=ep.title,
    )


def _freed(ep: Episode, label: str) -> int:
    if label == "DROP":
        return ep.tokens
    if label == "SUMMARIZE":
        return int(ep.tokens * 0.7)  # rough: a summary keeps ~30%
    return 0


# ─── ensemble (judge now; encoder seam for Phase 2) ──────────────────────


class EnsembleSuggester:
    """Combine the judge and (later) the encoder under one arbitration policy.

    Phase 1 ships with ``encoder=None`` so this just returns the judge's verdict.
    When the encoder lands it scores every episode cheaply/locally and this class
    reconciles the two:
      * ``safest``        — the more conservative label wins on disagreement.
      * ``judge_wins``    — trust the judge's label, keep the encoder score.
      * ``agreement_only``— only surface DROP/SUMMARIZE when both agree.
    """

    def __init__(
        self,
        judge: Optional[Suggester] = None,
        encoder: Optional[Suggester] = None,
        *,
        arbitration: str = "safest",
    ) -> None:
        self.judge = judge
        self.encoder = encoder
        self.arbitration = arbitration if arbitration in {
            "safest",
            "judge_wins",
            "agreement_only",
        } else "safest"

    def suggest(self, episodes: List[Episode], task: str) -> List[Suggestion]:
        if not episodes:
            return []
        judge_out = {s.episode_id: s for s in self.judge.suggest(episodes, task)} if self.judge else {}
        enc_out = {s.episode_id: s for s in self.encoder.suggest(episodes, task)} if self.encoder else {}

        if not enc_out:  # Phase 1 path
            return [judge_out.get(ep.id, _keep(ep, "judge", "")) for ep in episodes]
        if not judge_out:
            return [enc_out.get(ep.id, _keep(ep, "encoder", "")) for ep in episodes]

        merged: List[Suggestion] = []
        for ep in episodes:
            j = judge_out.get(ep.id)
            e = enc_out.get(ep.id)
            merged.append(self._arbitrate(ep, j, e))
        return merged

    def _arbitrate(
        self, ep: Episode, j: Optional[Suggestion], e: Optional[Suggestion]
    ) -> Suggestion:
        if j is None:
            return e or _keep(ep, "ensemble", "")
        if e is None:
            return j
        if j.label == e.label:
            return Suggestion(
                episode_id=ep.id, episode_index=ep.index, label=j.label,
                score=max(j.score, e.score), reason=j.reason or e.reason,
                source="ensemble", freed_tokens=_freed(ep, j.label),
                member_indices=ep.member_indices, title=ep.title,
            )
        if self.arbitration == "judge_wins":
            win = j
        elif self.arbitration == "agreement_only":
            win = _keep(ep, "ensemble", f"engines disagreed (judge={j.label}, encoder={e.label})")
        else:  # safest
            win = j if _SAFETY_RANK[j.label] <= _SAFETY_RANK[e.label] else e
        return Suggestion(
            episode_id=ep.id, episode_index=ep.index, label=win.label,
            score=win.score, reason=win.reason or f"(judge={j.label}/encoder={e.label})",
            source="ensemble", freed_tokens=_freed(ep, win.label),
            member_indices=ep.member_indices, title=ep.title,
        )


# ─── top-level entry point ───────────────────────────────────────────────


def suggest_removals(
    messages: List[BaseMessage],
    *,
    keep_recent: int = 3,
    mode: str = "judge",
    arbitration: str = "safest",
    judge_client: Optional[SuggesterClient] = None,
    encoder: Optional[Suggester] = None,
    return_episodes: bool = False,
) -> Tuple[List[Suggestion], Dict[str, Any]]:
    """Segment ``messages`` and return one :class:`Suggestion` per episode.

    Protected (the active task) and recent episodes are forced to KEEP by rule
    and never sent to an engine. Everything older is judged. Returns
    ``(suggestions, info)`` — or ``(suggestions, info, episodes)`` when
    ``return_episodes`` is set, so the caller can log audit rows (task +
    episode text) for the training loop without re-segmenting.
    """
    info: Dict[str, Any] = {
        "episodes": 0,
        "candidates": 0,
        "drop": 0,
        "summarize": 0,
        "potential_freed_tokens": 0,
        "mode": mode,
    }
    episodes = segment_into_episodes(messages)
    info["episodes"] = len(episodes)
    if not episodes:
        return ([], info, []) if return_episodes else ([], info)
    _mark_protection(episodes, keep_recent)

    candidates = [ep for ep in episodes if not ep.protected and not ep.is_recent]
    info["candidates"] = len(candidates)

    judged: Dict[str, Suggestion] = {}
    if candidates and (judge_client is not None or encoder is not None):
        judge = JudgeSuggester(judge_client) if judge_client is not None else None
        engine: Suggester = EnsembleSuggester(judge, encoder, arbitration=arbitration)
        for s in engine.suggest(candidates, active_task(messages)):
            judged[s.episode_id] = s

    out: List[Suggestion] = []
    for ep in episodes:
        if ep.protected:
            out.append(_keep(ep, "rule", "current task — always kept"))
        elif ep.is_recent:
            out.append(_keep(ep, "rule", "recent turn — kept for coherence"))
        else:
            out.append(judged.get(ep.id, _keep(ep, "rule", "not analyzed")))
    for s in out:
        if s.label == "DROP":
            info["drop"] += 1
        elif s.label == "SUMMARIZE":
            info["summarize"] += 1
        info["potential_freed_tokens"] += s.freed_tokens
    return (out, info, episodes) if return_episodes else (out, info)


# ─── feedback log (the dataset both improvement loops read) ──────────────


_DEFAULT_FEEDBACK_PATH = Path(
    os.getenv("ACM_RELEVANCE_FEEDBACK_PATH", str(Path.home() / ".acm" / "relevance_feedback.jsonl"))
)


def record_feedback(record: Dict[str, Any], *, path: Optional[Path] = None) -> Path:
    """Append one feedback row as JSONL. This is the single source of truth the
    encoder re-train and the judge DPO loop both consume later.

    A row should carry at least: ``conv``, ``episode_id``, ``shown_label``,
    ``user_action`` (accept_drop | reject | edit_to_X | ignore), ``final_label``,
    plus the episode features (title, tokens, score, source) and a timestamp.
    """
    p = path or _DEFAULT_FEEDBACK_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), **record}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return p


def load_feedback(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the feedback log back (for the future training scripts)."""
    return _load_jsonl(path or _DEFAULT_FEEDBACK_PATH)


# ─── audit log (features captured at suggest time) ───────────────────────
# The feedback log only knows the user's *action*; the model's *features*
# (the task + the episode text it judged) are captured here, at suggest time,
# where the full text is available. The trainers join the two on episode_id.


_DEFAULT_AUDIT_PATH = Path(
    os.getenv("ACM_RELEVANCE_AUDIT_PATH", str(Path.home() / ".acm" / "relevance_audits.jsonl"))
)


def build_audit_rows(
    episodes: List[Episode],
    suggestions: List[Suggestion],
    *,
    task: str,
    conv: str,
    surface: str,
    task_chars: int = 1200,
    text_chars: int = 2400,
) -> List[Dict[str, Any]]:
    """One audit row per *analyzed* (non-rule) suggestion, carrying the features
    a trainer needs: the current task and the episode's text, plus the model's
    own label/score so we can measure how often the user overrode it."""
    by_id = {e.id: e for e in episodes}
    rows: List[Dict[str, Any]] = []
    for s in suggestions:
        if s.source == "rule":
            continue  # protected/recent — not a learnable decision
        ep = by_id.get(s.episode_id)
        if ep is None:
            continue
        rows.append(
            {
                "surface": surface,
                "conv": conv,
                "episode_id": s.episode_id,
                "task": (task or "")[:task_chars],
                "episode_text": ep.text[:text_chars],
                "title": s.title,
                "model_label": s.label,
                "score": s.score,
                "source": s.source,
                "tokens": ep.tokens,
            }
        )
    return rows


def record_audit(rows: List[Dict[str, Any]], *, path: Optional[Path] = None) -> Optional[Path]:
    """Append audit rows as JSONL. No-op for an empty list."""
    if not rows:
        return None
    p = path or _DEFAULT_AUDIT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"ts": time.time(), **r}, ensure_ascii=False) + "\n")
    return p


def load_audits(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return _load_jsonl(path or _DEFAULT_AUDIT_PATH)


def _load_jsonl(p: Path) -> List[Dict[str, Any]]:
    if not p.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
