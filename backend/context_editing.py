"""Context-editing techniques (PR #3 onwards).

This module is the **only place** that rewrites a thread's message
history before `agent.ainvoke` runs. The plan locks the order:

  1. tool_result_trimming  ← mechanical, no LLM call. (PR #3, this PR)
  2. summarization          ← one extra LLM call. (PR #5, TODO)
  3. sliding_window         ← dumb fallback. (PR #6, TODO)

Each step is opt-in via the user's context-management profile (see
`backend/context_profiles.py`). When a step fires, it records a row
in `context_events` so the UI can show what happened.

The public entry point is `apply_context_edits(...)` — both /chat and
/chat/resume call it once, before `agent.ainvoke`. Future PRs add
their step inside the same function so the chat handlers don't
change again.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)


# Tools whose results are load-bearing and must never be replaced with
# a placeholder regardless of caller settings. The execute_typescript
# stdout is often the only artefact of a Code Mode turn; the memory
# tool's reads are the whole point of the technique.
_NEVER_TRIM_TOOLS: set[str] = {"execute_typescript", "memory"}

_TRIM_PLACEHOLDER = "[result cleared to save context — re-run the tool if needed]"


def _rough_tokens(text: Any) -> int:
    """Lightweight token estimator. ~4 chars per token for English text.
    Matches `api._estimate_tokens` so the per-message totals stay
    consistent across modules.
    """
    if not text:
        return 0
    return max(1, len(str(text)) // 4)


def _msg_tokens(msg: BaseMessage, estimator: Callable[[Any], int]) -> int:
    """Estimate the tokens a message occupies. Includes tool_call
    payloads on AIMessages because those serialise back into the wire
    format and count against the budget."""
    total = estimator(getattr(msg, "content", "") or "")
    # AIMessage tool_calls also serialise — small but not zero.
    for tc in getattr(msg, "tool_calls", None) or []:
        total += estimator(tc.get("name", "")) + estimator(
            str(tc.get("args", "") or "")
        )
    return total


def _count_user_turns(messages: List[BaseMessage]) -> int:
    """Number of user messages seen so far. Used to stamp the
    `turn_index` on a context_events row when we log an edit."""
    from langchain_core.messages import HumanMessage

    return sum(1 for m in messages if isinstance(m, HumanMessage))


# ─── B2: tool-result trimming ───────────────────────────────────────────


def trim_tool_results(
    messages: List[BaseMessage],
    *,
    trigger_tokens: int = 20_000,
    keep_recent: int = 4,
    exclude_tools: Optional[set[str]] = None,
    estimator: Callable[[Any], int] = _rough_tokens,
) -> Tuple[List[BaseMessage], Dict[str, Any]]:
    """Replace bodies of old ToolMessages with a placeholder when the
    running total exceeds `trigger_tokens`.

    Returns ``(replacements, info)``:
      - ``replacements``: NEW ToolMessage objects, each carrying the
        SAME `id` as the message it overwrites. LangGraph's
        ``add_messages`` reducer merges by id, so passing these to
        ``agent.update_state(config, {"messages": replacements})``
        leaves the order untouched while swapping the bodies.
      - ``info``: {trigger_tokens, total_tokens, cleared, freed_tokens,
        kept_recent}. Empty `cleared=0` when the threshold wasn't hit.

    Never trims:
      - the most recent `keep_recent` tool results (the agent usually
        needs the last few for follow-up reasoning),
      - tools listed in `exclude_tools` (auto-merged with the
        always-protected set `_NEVER_TRIM_TOOLS`),
      - messages whose content already equals the placeholder.
    """
    info: Dict[str, Any] = {
        "trigger_tokens": trigger_tokens,
        "total_tokens": 0,
        "cleared": 0,
        "freed_tokens": 0,
        "kept_recent": min(keep_recent, 0),
    }
    excluded = (exclude_tools or set()) | _NEVER_TRIM_TOOLS

    total = sum(_msg_tokens(m, estimator) for m in messages)
    info["total_tokens"] = total
    if total < trigger_tokens:
        return [], info

    # Indices of ToolMessages we're allowed to trim, oldest first.
    candidate_indices = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, ToolMessage)
        and (getattr(m, "name", "") or "") not in excluded
        and (m.content or "") != _TRIM_PLACEHOLDER
    ]
    if len(candidate_indices) <= keep_recent:
        info["kept_recent"] = len(candidate_indices)
        return [], info

    cutoff = len(candidate_indices) - keep_recent if keep_recent > 0 else len(candidate_indices)
    to_clear = candidate_indices[:cutoff]
    info["kept_recent"] = len(candidate_indices) - len(to_clear)

    replacements: List[BaseMessage] = []
    placeholder_tokens = estimator(_TRIM_PLACEHOLDER)
    for idx in to_clear:
        old = messages[idx]
        old_tokens = _msg_tokens(old, estimator)
        freed = max(0, old_tokens - placeholder_tokens)
        info["freed_tokens"] += freed
        info["cleared"] += 1
        replacements.append(
            ToolMessage(
                content=_TRIM_PLACEHOLDER,
                tool_call_id=getattr(old, "tool_call_id", "") or "",
                name=getattr(old, "name", None),
                id=getattr(old, "id", None),  # same id ⇒ in-place replace
            )
        )
    return replacements, info


# ─── B1: summarisation ──────────────────────────────────────────────────


_DEFAULT_SUMMARY_SYSTEM = (
    "You are a conversation compactor. You receive an excerpt of a chat "
    "between a user and an AI agent (with tool calls + results) and must "
    "summarise it into a short paragraph that the agent can read to "
    "continue the conversation.\n\n"
    "Preserve, in priority order:\n"
    "  1. Open tasks, unresolved bugs, and the user's most recent intent.\n"
    "  2. Architectural / design decisions and any commitments made.\n"
    "  3. File paths and identifiers the agent has already touched.\n"
    "  4. Concrete results the agent has produced (function names, "
    "     totals, key findings).\n"
    "Drop verbose tool outputs, repeated retries, and intermediate "
    "reasoning. Aim for ≤ 400 tokens.\n\n"
    "Wrap your summary in <summary></summary>. Do not add commentary "
    "outside the tags."
)


def _render_transcript(messages: List[BaseMessage]) -> str:
    """Compact human-readable rendering of the messages to summarise.
    Keeps the model's job easy: one line per message, role tag + content
    (or "[tool call] name=..." for assistant tool-call stubs)."""
    lines: List[str] = []
    for m in messages:
        role = getattr(m, "type", None) or type(m).__name__.lower()
        content = (getattr(m, "content", "") or "")
        if not isinstance(content, str):
            content = str(content)
        # Short prefix per role, content truncated so giant tool outputs
        # don't make the summariser do unnecessary work.
        if len(content) > 1200:
            content = content[:1200] + " […truncated…]"
        if getattr(m, "tool_calls", None):
            tcs = ", ".join(
                f"{tc.get('name','?')}({list((tc.get('args') or {}).keys())})"
                for tc in m.tool_calls
            )
            lines.append(f"[{role}] {content} [tool_calls: {tcs}]")
        else:
            tool_name = getattr(m, "name", "") or ""
            tag = f"{role}:{tool_name}" if tool_name else role
            lines.append(f"[{tag}] {content}")
    return "\n".join(lines)


def summarise_old_messages(
    messages: List[BaseMessage],
    *,
    trigger_tokens: int = 50_000,
    keep_recent: int = 6,
    summariser_model,
    instructions: Optional[str] = None,
    estimator: Callable[[Any], int] = _rough_tokens,
) -> Tuple[List[BaseMessage], Optional[SystemMessage], Dict[str, Any]]:
    """Summarise the prefix of the message list when the running token
    total exceeds `trigger_tokens`.

    Returns ``(removes, summary_msg, info)``:
      - ``removes``: list of RemoveMessage(id=...) for the messages to drop.
      - ``summary_msg``: a SystemMessage carrying the LLM-generated summary,
        or None when nothing fired (under threshold / no removable prefix
        / LLM call failed).
      - ``info``: stats for logging — tokens before/after, summary token
        count, message count compacted, any error.

    Designed to be paired with ``agent.update_state(config, {"messages":
    removes + [summary_msg]})`` — LangGraph's reducer applies the
    RemoveMessage IDs first, then appends the new summary.
    """
    info: Dict[str, Any] = {
        "trigger_tokens": trigger_tokens,
        "total_tokens": 0,
        "compacted": 0,
        "summary_tokens": 0,
        "freed_tokens": 0,
    }
    total = sum(_msg_tokens(m, estimator) for m in messages)
    info["total_tokens"] = total
    if total < trigger_tokens:
        return [], None, info
    if len(messages) <= keep_recent:
        return [], None, info

    cutoff = len(messages) - keep_recent
    prefix = [m for m in messages[:cutoff] if getattr(m, "id", None)]
    if not prefix:
        # Nothing has a stable id, so we can't remove anything safely.
        return [], None, info

    # Skip if the prefix is already mostly a previous summary
    # (don't recursively summarise tiny prefixes).
    prefix_tokens = sum(_msg_tokens(m, estimator) for m in prefix)
    if prefix_tokens < max(2_000, trigger_tokens // 10):
        return [], None, info

    system_text = _DEFAULT_SUMMARY_SYSTEM
    if instructions:
        system_text = system_text + "\n\nExtra instructions from the user:\n" + instructions
    transcript = _render_transcript(prefix)
    try:
        resp = summariser_model.invoke(
            [
                SystemMessage(content=system_text),
                HumanMessage(content=f"Summarise the following transcript:\n\n{transcript}"),
            ]
        )
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
        return [], None, info

    summary_text = resp.content if isinstance(resp.content, str) else str(resp.content)
    if not summary_text.strip():
        info["error"] = "summariser returned empty content"
        return [], None, info

    # Wrap defensively — the model is asked to add tags but might forget.
    if "<summary>" not in summary_text:
        summary_text = f"<summary>{summary_text.strip()}</summary>"

    summary_msg = SystemMessage(
        content=(
            "[Earlier conversation has been compacted to save context.]\n"
            f"{summary_text}"
        )
    )
    removes = [RemoveMessage(id=m.id) for m in prefix if getattr(m, "id", None)]
    info["compacted"] = len(removes)
    info["summary_tokens"] = estimator(summary_msg.content)
    info["freed_tokens"] = max(0, prefix_tokens - info["summary_tokens"])
    return removes, summary_msg, info


# ─── B6: sliding window ─────────────────────────────────────────────────


def sliding_window_trim(
    messages: List[BaseMessage],
    *,
    keep_recent: int = 12,
) -> Tuple[List[RemoveMessage], Dict[str, Any]]:
    """Drop the middle of a long conversation, keep the system messages
    + the most recent ``keep_recent`` messages.

    No LLM call, no summarisation, no nuance — pure chop. Designed as
    the safety net of last resort behind trimming + summarisation, or
    as the only edit a tiny-context model can afford.
    """
    info: Dict[str, Any] = {"keep_recent": keep_recent, "dropped": 0}
    if len(messages) <= keep_recent:
        return [], info
    # Always preserve any leading SystemMessage(s) — that's the actual
    # system prompt / a prior compaction summary.
    head: List[BaseMessage] = []
    body_start = 0
    for i, m in enumerate(messages):
        if isinstance(m, SystemMessage):
            head.append(m)
            body_start = i + 1
        else:
            break
    body = messages[body_start:]
    if len(body) <= keep_recent:
        return [], info
    # Drop the front of the body, keep the tail of length keep_recent.
    cutoff = len(body) - keep_recent
    to_drop = [m for m in body[:cutoff] if getattr(m, "id", None)]
    if not to_drop:
        return [], info
    info["dropped"] = len(to_drop)
    return [RemoveMessage(id=m.id) for m in to_drop], info


# ─── orchestrator (single entry point for the chat handlers) ────────────


def apply_context_edits(
    agent,
    config: Dict[str, Any],
    profile,  # context_profiles.Profile
    *,
    chat_model=None,
    record_event: Optional[
        Callable[[str, int, int, Optional[Dict[str, Any]]], None]
    ] = None,
    estimator: Callable[[Any], int] = _rough_tokens,
) -> List[Dict[str, Any]]:
    """Run every enabled context-editing step in the fixed order and
    push the resulting message rewrites back to the LangGraph state.

    Returns a list of edit-event dicts (one per step that actually
    fired) so the caller can log them, render UI markers, etc.

    `record_event(edit_type, turn_index, freed_tokens, details)` is
    called once per fired step. The chat handler wires it to
    `_record_context_event` so the UI's applied-edits list updates.

    Subsequent PRs slot in here:
      - PR #5 (summarization) — after trim, before sliding_window.
      - PR #6 (sliding_window) — last, as the dumb safety net.
    """
    fired: List[Dict[str, Any]] = []
    cm = getattr(profile, "context_management", None)
    if cm is None:
        return fired

    state = agent.get_state(config)
    messages: List[BaseMessage] = list(
        (state.values or {}).get("messages", []) or []
    )
    if not messages:
        return fired

    turn_index = _count_user_turns(messages)

    # 1. tool_result_trimming (PR #3)
    trim_cfg = getattr(cm, "tool_result_trimming", None)
    if trim_cfg is not None and getattr(trim_cfg, "enabled", False):
        replacements, info = trim_tool_results(
            messages,
            trigger_tokens=trim_cfg.trigger_tokens,
            keep_recent=trim_cfg.keep_recent,
            exclude_tools=set(trim_cfg.exclude_tools or []),
            estimator=estimator,
        )
        if replacements:
            try:
                agent.update_state(config, {"messages": replacements})
            except Exception as e:
                # Never let an edit failure block the chat turn —
                # log and move on.
                print(
                    f"[context_editing] update_state failed: {e!r}",
                    flush=True,
                )
            else:
                # Update local messages list so later steps in this same
                # orchestrator call see the trimmed view, not the raw one.
                by_id = {getattr(r, "id", None): r for r in replacements}
                messages = [
                    by_id.get(getattr(m, "id", None), m) for m in messages
                ]
                event = {
                    "type": "tool_result_trimming",
                    "turn": turn_index,
                    "freed_tokens": info["freed_tokens"],
                    "details": {
                        "cleared": info["cleared"],
                        "total_tokens_before": info["total_tokens"],
                        "trigger_tokens": info["trigger_tokens"],
                        "kept_recent": info["kept_recent"],
                    },
                }
                fired.append(event)
                if record_event is not None:
                    try:
                        record_event(
                            event["type"],
                            event["turn"],
                            event["freed_tokens"],
                            event["details"],
                        )
                    except Exception as e:
                        print(
                            f"[context_editing] record_event failed: {e!r}",
                            flush=True,
                        )

    # 2. summarization (PR #5)
    sum_cfg = getattr(cm, "summarization", None)
    if sum_cfg is not None and getattr(sum_cfg, "enabled", False):
        # Pick the summariser: a profile-specified slug wins, else the
        # active chat model. The slug path lets a user shrink to a cheap
        # fast model purely for compaction work.
        summariser = None
        slug = getattr(sum_cfg, "summariser_model", None) or None
        if slug:
            try:
                from api import _build_model  # lazy

                summariser = _build_model(slug)
            except Exception as e:
                print(
                    f"[context_editing] summariser slug {slug!r} failed: {e!r} — "
                    f"falling back to chat model",
                    flush=True,
                )
        if summariser is None:
            summariser = chat_model
        if summariser is None:
            # Can't run summarisation without a model; skip the step
            # cleanly so the rest of the orchestrator keeps working.
            pass
        else:
            removes, summary_msg, info = summarise_old_messages(
                messages,
                trigger_tokens=sum_cfg.trigger_tokens,
                keep_recent=sum_cfg.keep_recent,
                summariser_model=summariser,
                instructions=getattr(sum_cfg, "instructions", None),
                estimator=estimator,
            )
            if summary_msg is not None:
                try:
                    agent.update_state(
                        config, {"messages": removes + [summary_msg]}
                    )
                except Exception as e:
                    print(
                        f"[context_editing] summarisation update_state failed: {e!r}",
                        flush=True,
                    )
                else:
                    # Drop the summarised prefix from the local view and
                    # tack the summary onto the front so subsequent
                    # steps (sliding_window) see the rewritten state.
                    removed_ids = {r.id for r in removes}
                    messages = [summary_msg] + [
                        m for m in messages if getattr(m, "id", None) not in removed_ids
                    ]
                    event = {
                        "type": "summarization",
                        "turn": turn_index,
                        "freed_tokens": info["freed_tokens"],
                        "details": {
                            "compacted": info["compacted"],
                            "summary_tokens": info["summary_tokens"],
                            "total_tokens_before": info["total_tokens"],
                            "trigger_tokens": info["trigger_tokens"],
                            "summariser": slug or "chat-model",
                        },
                    }
                    fired.append(event)
                    if record_event is not None:
                        try:
                            record_event(
                                event["type"],
                                event["turn"],
                                event["freed_tokens"],
                                event["details"],
                            )
                        except Exception as e:
                            print(
                                f"[context_editing] record_event failed: {e!r}",
                                flush=True,
                            )

    # 3. sliding_window (PR #6) — dumb safety net behind 1 and 2
    sw_cfg = getattr(cm, "sliding_window", None)
    if sw_cfg is not None and getattr(sw_cfg, "enabled", False):
        removes, info = sliding_window_trim(
            messages, keep_recent=sw_cfg.keep_recent
        )
        if removes:
            try:
                agent.update_state(config, {"messages": removes})
            except Exception as e:
                print(
                    f"[context_editing] sliding_window update_state failed: {e!r}",
                    flush=True,
                )
            else:
                removed_ids = {r.id for r in removes}
                # Re-estimate freed tokens from the local view BEFORE
                # we drop the records; sliding window has no LLM call
                # so the saving is just the sum of removed bodies.
                freed = sum(
                    _msg_tokens(m, estimator)
                    for m in messages
                    if getattr(m, "id", None) in removed_ids
                )
                messages = [
                    m for m in messages if getattr(m, "id", None) not in removed_ids
                ]
                event = {
                    "type": "sliding_window",
                    "turn": turn_index,
                    "freed_tokens": freed,
                    "details": {
                        "dropped": info["dropped"],
                        "keep_recent": info["keep_recent"],
                    },
                }
                fired.append(event)
                if record_event is not None:
                    try:
                        record_event(
                            event["type"],
                            event["turn"],
                            event["freed_tokens"],
                            event["details"],
                        )
                    except Exception as e:
                        print(
                            f"[context_editing] record_event failed: {e!r}",
                            flush=True,
                        )

    return fired
