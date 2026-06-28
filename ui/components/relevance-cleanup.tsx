'use client'

/**
 * Relevance Cleanup — task-aware context pruning suggestions.
 *
 * Lives inside the Context window drawer. On "Analyze" it asks the backend to
 * split the thread into episodes and label each KEEP / SUMMARIZE / DROP against
 * the current task (LLM-as-judge, a local encoder, or both — set by the active
 * profile). Suggest-only: a card acts on the model's context only when the user
 * clicks Remove (delete the episode) or Summarize (replace it with a short
 * summary). Every decision is logged for the training loop.
 */

import { useState } from 'react'
import { authFetch } from '@/lib/supabase'

type Suggestion = {
  episode_id: string
  episode_index: number
  label: 'KEEP' | 'SUMMARIZE' | 'DROP'
  score: number
  reason: string
  source: 'encoder' | 'judge' | 'ensemble' | 'rule'
  freed_tokens: number
  title: string
  member_ids: number[]
  removable_from_state: boolean
}

// One-word status derived from the judge's reason, so the list reads at a
// glance (done / error / duplicate / empty / active) instead of a sentence.
function statusWord(s: Suggestion): { w: string; cls: string } {
  const r = (s.reason || '').toLowerCase()
  if (/(fail|error|crash|exception|denied)/.test(r))
    return { w: 'error', cls: 'text-rose-300 bg-rose-500/15' }
  if (/(duplicate|redundant|repeat)/.test(r))
    return { w: 'duplicate', cls: 'text-amber-300 bg-amber-500/15' }
  if (/(empty|no-op|nothing|blank)/.test(r))
    return { w: 'empty', cls: 'text-fog-400 bg-soft/[0.08]' }
  if (/(in progress|pending|ongoing|working)/.test(r))
    return { w: 'active', cls: 'text-sky-300 bg-sky-500/15' }
  if (/(success|added|complete|done|implement|created|updated|wrote|finish)/.test(r))
    return { w: 'done', cls: 'text-emerald-300 bg-emerald-500/15' }
  return { w: s.label.toLowerCase(), cls: 'text-fog-300 bg-soft/[0.08]' }
}

export function RelevanceCleanupSection({
  sessionId,
  threadId,
  model,
  onApplied,
}: {
  sessionId: string | null
  threadId: string | null
  model?: string
  onApplied: () => void
}) {
  const [sugs, setSugs] = useState<Suggestion[] | null>(null)
  const [info, setInfo] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [working, setWorking] = useState<string | null>(null) // episode_id in-flight
  const [error, setError] = useState<string | null>(null)

  const base = `/api/sessions/${sessionId}/threads/${threadId}/relevance`
  const qs = model ? `?model=${encodeURIComponent(model)}` : ''

  async function analyze() {
    if (!sessionId || !threadId) return
    setBusy(true)
    setError(null)
    setSugs(null)
    setInfo(null)
    try {
      const r = await authFetch(`${base}/suggest${qs}`, { method: 'POST' })
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
      const data = await r.json()
      setSugs(data.suggestions || [])
      setInfo(data.info || null)
    } catch (e: any) {
      setError(e?.message ?? 'Analyze failed')
    } finally {
      setBusy(false)
    }
  }

  // Drop the acted-on card from the local list so the rest stay visible
  // (no full re-Analyze) and refresh the meter/messages via a soft refresh.
  function settle(episodeId: string) {
    setSugs((prev) => (prev || []).filter((x) => x.episode_id !== episodeId))
    onApplied()
  }

  async function remove(s: Suggestion) {
    setWorking(s.episode_id)
    setError(null)
    try {
      const r = await authFetch(`${base}/apply`, {
        method: 'POST',
        body: JSON.stringify({
          message_ids: s.member_ids,
          episode_id: s.episode_id,
          label: s.label,
          score: s.score,
          source: s.source,
          title: s.title,
          freed_tokens: s.freed_tokens,
          model,
        }),
      })
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
      settle(s.episode_id)
    } catch (e: any) {
      setError(e?.message ?? 'Remove failed')
    } finally {
      setWorking(null)
    }
  }

  async function summarize(s: Suggestion) {
    setWorking(s.episode_id)
    setError(null)
    try {
      const r = await authFetch(`${base}/summarize`, {
        method: 'POST',
        body: JSON.stringify({
          message_ids: s.member_ids,
          episode_id: s.episode_id,
          title: s.title,
          score: s.score,
          source: s.source,
          freed_tokens: s.freed_tokens,
          model,
        }),
      })
      if (!r.ok) throw new Error((await r.text()) || `HTTP ${r.status}`)
      settle(s.episode_id)
    } catch (e: any) {
      setError(e?.message ?? 'Summarize failed')
    } finally {
      setWorking(null)
    }
  }

  async function keep(s: Suggestion) {
    settle(s.episode_id)
    try {
      await authFetch(`${base}/feedback`, {
        method: 'POST',
        body: JSON.stringify({
          episode_id: s.episode_id,
          title: s.title,
          shown_label: s.label,
          user_action: 'reject',
          final_label: 'KEEP',
          score: s.score,
          source: s.source,
          tokens: s.freed_tokens,
        }),
      })
    } catch {
      /* feedback logging is best-effort */
    }
  }

  const actionable = (sugs || []).filter((s) => s.label !== 'KEEP')

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] uppercase tracking-widest text-fog-500">
          Relevance cleanup
        </div>
        <button
          onClick={analyze}
          disabled={busy || !sessionId || !threadId}
          className="text-[11px] px-2 py-0.5 rounded text-fog-200 bg-soft/[0.06] hover:bg-soft/[0.12] disabled:opacity-40"
        >
          {busy ? 'Analyzing…' : 'Analyze'}
        </button>
      </div>

      {error && <div className="text-red-400 text-xs mb-2">{error}</div>}

      {!sugs && !busy && !error && (
        <p className="text-fog-500 text-xs">
          Split this chat into episodes and suggest which finished or unrelated
          ones to remove or summarize. Nothing changes until you confirm.
        </p>
      )}

      {info && actionable.length > 0 && (
        <div className="text-fog-400 text-[11px] mb-2">
          {actionable.length} section{actionable.length === 1 ? '' : 's'} · ~
          {(info.potential_freed_tokens || 0).toLocaleString()} tokens recoverable
        </div>
      )}

      <div className="space-y-1">
        {actionable.map((s) => {
          const st = statusWord(s)
          const isWorking = working === s.episode_id
          const canSummarize = s.label === 'SUMMARIZE'
          return (
            <div
              key={s.episode_id}
              className={`border border-line rounded-md px-2.5 py-1.5 bg-ink-300/40 ${
                isWorking ? 'opacity-50' : ''
              }`}
            >
              <div className="flex items-center gap-2">
                <span
                  className={`text-[9.5px] font-semibold uppercase tracking-wide px-1 py-0.5 rounded shrink-0 ${st.cls}`}
                >
                  {st.w}
                </span>
                <span
                  className="text-fog-100 text-[12px] truncate flex-1"
                  title={`${s.title}\n${s.reason}`}
                >
                  {s.title}
                </span>
                <span className="text-fog-500 text-[10px] shrink-0 tabular-nums">
                  ~{s.freed_tokens.toLocaleString()} tok
                </span>
              </div>
              <div className="flex items-center gap-1 mt-1">
                {canSummarize && (
                  <button
                    onClick={() => summarize(s)}
                    disabled={isWorking}
                    className="text-[11px] px-2 py-0.5 rounded bg-yellow-500/15 text-yellow-300 hover:bg-yellow-500/25 disabled:opacity-50"
                  >
                    Summarize
                  </button>
                )}
                <button
                  onClick={() => remove(s)}
                  disabled={isWorking}
                  className="text-[11px] px-2 py-0.5 rounded bg-red-500/15 text-red-300 hover:bg-red-500/25 disabled:opacity-50"
                >
                  Remove
                </button>
                <button
                  onClick={() => keep(s)}
                  disabled={isWorking}
                  className="text-[11px] px-2 py-0.5 rounded text-fog-300 hover:bg-soft/[0.08] disabled:opacity-50"
                >
                  Keep
                </button>
                <span className="ml-auto text-fog-500 text-[10px] font-mono">
                  {s.source} · {s.member_ids.length} msg
                </span>
              </div>
            </div>
          )
        })}
      </div>

      {sugs && actionable.length === 0 && !error && (
        <p className="text-fog-500 text-xs">
          Nothing to clean up — every section looks relevant to the current task.
        </p>
      )}
    </section>
  )
}
