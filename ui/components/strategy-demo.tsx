'use client'

/**
 * Strategy Demo Panel — side-by-side context-management comparison.
 *
 * One prompt input at the top, two columns underneath: each runs the
 * same query under a different strategy (Tool Calling vs TypeScript
 * Code Mode). Both run in parallel through POST /api/demo/compare.
 *
 * For each result we surface latency, token usage, tool-call count,
 * the agent's final reply, and the list of tool invocations the run
 * made. The backend uses an in-memory checkpointer + a one-off
 * thread_id per call, so demo turns never leak into the user's chat
 * history.
 */

import { useEffect, useState } from 'react'
import { authFetch } from '@/lib/supabase'
import { useTheme } from '@/lib/theme'

type ToolEvent = { name: string; args_keys: string[] }
type Metrics = {
  latency_ms: number
  tool_calls?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
}
type Result = {
  strategy: string
  ok: boolean
  reply?: string
  error?: string
  metrics: Metrics
  tool_events: ToolEvent[]
}
type CompareResponse = {
  prompt: string
  model: string
  results: Result[]
}

const STRATEGY_LABEL: Record<string, string> = {
  tool_calling: 'Tool Calling',
  ts_code_mode: 'TypeScript Code Mode',
}

const SAMPLE_PROMPTS = [
  'Use the calculator to compute the squares of every integer from 1 to 20, then tell me which one is closest to 250.',
  'Compute factorials of 1 through 10 using only the calculator tool, one multiplication per call. Print each factorial.',
  'Use the calculator to evaluate (3*7)+(11*2)-(45/9)+sqrt of 144 written as 12*12, and report each intermediate value and the final total.',
]

type ModelInfo = { id: string; name: string; context_length: number }

export function StrategyDemoPanel() {
  const [prompt, setPrompt] = useState<string>(SAMPLE_PROMPTS[0])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [response, setResponse] = useState<CompareResponse | null>(null)
  // Model picker — fetched from /api/models on mount. Selection
  // persists in localStorage under the same `selected_model` key the
  // main chat uses, so the demo defaults to whatever the user last
  // chose elsewhere.
  const [models, setModels] = useState<ModelInfo[]>([])
  const [defaultModel, setDefaultModel] = useState<string>('')
  const [selectedModel, setSelectedModel] = useState<string>('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await authFetch('/api/models')
        if (!r.ok) return
        const data = await r.json()
        if (cancelled) return
        const list: ModelInfo[] = Array.isArray(data?.models) ? data.models : []
        setModels(list)
        setDefaultModel(data?.default || list[0]?.id || '')
        const saved =
          typeof window !== 'undefined'
            ? localStorage.getItem('selected_model') || ''
            : ''
        const initial =
          (saved && list.some((m) => m.id === saved) && saved) ||
          data?.default ||
          list[0]?.id ||
          ''
        setSelectedModel(initial)
      } catch {
        // Non-critical — the picker stays empty and the backend default wins.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  async function runComparison() {
    if (!prompt.trim() || running) return
    setRunning(true)
    setError(null)
    setResponse(null)
    try {
      const r = await authFetch('/api/demo/compare', {
        method: 'POST',
        body: JSON.stringify({
          prompt: prompt.trim(),
          strategies: ['tool_calling', 'ts_code_mode'],
          model: selectedModel || undefined,
        }),
      })
      if (!r.ok) {
        const body = await r.json().catch(() => ({}))
        setError(`${r.status} ${r.statusText} — ${body?.detail ?? 'unknown error'}`)
      } else {
        const body: CompareResponse = await r.json()
        setResponse(body)
      }
    } catch (e: any) {
      setError(e?.message ?? 'network error')
    } finally {
      setRunning(false)
    }
  }

  // Sort tool_calling before ts_code_mode for stable column order
  // regardless of how the backend ordered the response.
  const ordered: Result[] = (response?.results ?? [])
    .slice()
    .sort((a, b) => {
      const rank = (s: string) => (s === 'tool_calling' ? 0 : 1)
      return rank(a.strategy) - rank(b.strategy)
    })

  return (
    <div className="flex flex-col h-full min-h-0 bg-ink-50 text-fog-100">
      <header className="h-12 px-5 border-b border-line flex items-center justify-between shrink-0">
        <span className="text-sm text-fog-50 font-medium">Strategy Demo</span>
        <HeaderTheme />
      </header>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl px-6 py-8">
          <div className="mb-6">
            <h1 className="serif text-3xl tracking-tighter text-fog-50">
              Side-by-side strategy comparison
            </h1>
            <p className="text-sm text-fog-400 mt-1.5 max-w-3xl">
              Run the same prompt under <strong>Tool Calling</strong> and{' '}
              <strong>TypeScript Code Mode</strong> at the same time. Each
              call uses a throwaway in-memory checkpointer — nothing
              leaks into your chat history. Compare round-trip count,
              token usage, latency and the final answer.
            </p>
          </div>

          {/* Prompt box + run button */}
          <div className="surface p-4 mb-6">
            <label className="text-[11px] uppercase tracking-widest text-fog-400 mb-2 block">
              Prompt
            </label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={4}
              spellCheck={false}
              className="w-full bg-ink-200 border border-line rounded-md px-3 py-2 text-sm text-fog-100 placeholder:text-fog-500 outline-none focus:border-lineStrong font-mono leading-relaxed resize-y"
              placeholder="Type a prompt that benefits from chaining tool calls…"
            />
            <div className="flex flex-wrap items-center justify-between gap-3 mt-3">
              <div className="flex flex-wrap gap-1.5">
                {SAMPLE_PROMPTS.map((p, i) => (
                  <button
                    key={i}
                    onClick={() => setPrompt(p)}
                    disabled={running}
                    className="chip text-[11px] py-0.5 hover:bg-soft/[0.08] disabled:opacity-50"
                    title={p}
                  >
                    Sample {i + 1}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2">
                {models.length > 0 ? (
                  <div className="chip flex items-center gap-2 pr-1">
                    <span className="dot bg-emerald-400" />
                    <select
                      value={selectedModel}
                      disabled={running}
                      onChange={(e) => {
                        const v = e.target.value
                        setSelectedModel(v)
                        if (typeof window !== 'undefined') {
                          localStorage.setItem('selected_model', v)
                        }
                      }}
                      className="bg-transparent text-xs text-fog-50 outline-none max-w-[16rem] truncate disabled:opacity-50"
                      title="Model used for both strategies in the comparison"
                    >
                      {models.map((m) => (
                        <option
                          key={m.id}
                          value={m.id}
                          className="bg-ink-200 text-fog-50"
                        >
                          {m.name.replace(/\s*\(free\)\s*$/i, '')}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : (
                  <span className="chip">
                    <span className="dot bg-emerald-400" />
                    {selectedModel || defaultModel || 'default'}
                  </span>
                )}
                <button
                  onClick={runComparison}
                  disabled={running || !prompt.trim()}
                  className="px-4 py-2 rounded-md bg-accent text-ink-50 text-sm font-medium hover:bg-accent/90 disabled:opacity-50 disabled:cursor-not-allowed transition"
                >
                  {running ? 'Running both strategies…' : 'Run comparison'}
                </button>
              </div>
            </div>
            {error && (
              <div className="mt-3 text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
                {error}
              </div>
            )}
            {response && (
              <div className="mt-3 text-[11px] text-fog-500">
                Model: <span className="text-fog-300">{response.model}</span>
              </div>
            )}
          </div>

          {/* Two-column comparison */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <ResultColumn
              strategy="tool_calling"
              loading={running && !response}
              result={ordered.find((r) => r.strategy === 'tool_calling') ?? null}
              compareWith={ordered.find((r) => r.strategy === 'ts_code_mode') ?? null}
            />
            <ResultColumn
              strategy="ts_code_mode"
              loading={running && !response}
              result={ordered.find((r) => r.strategy === 'ts_code_mode') ?? null}
              compareWith={ordered.find((r) => r.strategy === 'tool_calling') ?? null}
            />
          </div>
        </div>
      </div>
    </div>
  )
}

function ResultColumn({
  strategy,
  loading,
  result,
  compareWith,
}: {
  strategy: string
  loading: boolean
  result: Result | null
  compareWith: Result | null
}) {
  const label = STRATEGY_LABEL[strategy] ?? strategy
  const accent = strategy === 'ts_code_mode' ? 'text-accent' : 'text-fog-200'

  return (
    <div className="surface p-4 flex flex-col gap-4 min-h-[260px]">
      <div className="flex items-center justify-between">
        <h2 className={`text-sm font-semibold ${accent}`}>{label}</h2>
        <StatusBadge loading={loading} result={result} />
      </div>

      {loading && !result && (
        <div className="flex-1 flex items-center justify-center text-xs text-fog-500">
          <span className="inline-flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-fog-400 animate-pulse" />
            running…
          </span>
        </div>
      )}

      {result && (
        <>
          <MetricsGrid result={result} compareWith={compareWith} />

          {result.tool_events.length > 0 && (
            <div>
              <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-1.5">
                Tool calls ({result.tool_events.length})
              </div>
              <div className="space-y-1">
                {result.tool_events.map((ev, i) => (
                  <div
                    key={i}
                    className="text-[11px] font-mono text-fog-300 bg-ink-200 border border-line rounded px-2 py-1"
                  >
                    <span className="text-fog-100">{ev.name}</span>
                    {ev.args_keys.length > 0 && (
                      <span className="text-fog-500">
                        ({ev.args_keys.join(', ')})
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-1.5">
              {result.ok ? 'Reply' : 'Error'}
            </div>
            <div
              className={`text-sm whitespace-pre-wrap font-mono leading-relaxed rounded-md p-3 border ${
                result.ok
                  ? 'bg-ink-200 border-line text-fog-100'
                  : 'bg-red-500/10 border-red-500/30 text-red-200'
              }`}
            >
              {result.ok ? result.reply || '(empty reply)' : result.error}
            </div>
          </div>
        </>
      )}

      {!loading && !result && (
        <div className="flex-1 flex items-center justify-center text-xs text-fog-500">
          Hit “Run comparison” to populate this column.
        </div>
      )}
    </div>
  )
}

function StatusBadge({
  loading,
  result,
}: {
  loading: boolean
  result: Result | null
}) {
  if (loading && !result) {
    return (
      <span className="chip text-[10px] py-0.5">
        <span className="dot bg-amber-400" />
        running
      </span>
    )
  }
  if (!result) {
    return (
      <span className="chip text-[10px] py-0.5">
        <span className="dot bg-fog-500" />
        idle
      </span>
    )
  }
  if (!result.ok) {
    return (
      <span className="chip text-[10px] py-0.5">
        <span className="dot bg-red-400" />
        error
      </span>
    )
  }
  return (
    <span className="chip text-[10px] py-0.5">
      <span className="dot bg-emerald-400" />
      done
    </span>
  )
}

function MetricsGrid({
  result,
  compareWith,
}: {
  result: Result
  compareWith: Result | null
}) {
  const m = result.metrics
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
      <MetricCell
        label="Latency"
        value={`${m.latency_ms} ms`}
        delta={deltaSuffix(m.latency_ms, compareWith?.metrics?.latency_ms, true)}
      />
      <MetricCell
        label="Tool calls"
        value={`${m.tool_calls ?? 0}`}
        delta={deltaSuffix(m.tool_calls, compareWith?.metrics?.tool_calls, true)}
      />
      <MetricCell
        label="Input tok"
        value={`${m.input_tokens ?? 0}`}
        delta={deltaSuffix(m.input_tokens, compareWith?.metrics?.input_tokens, true)}
      />
      <MetricCell
        label="Output tok"
        value={`${m.output_tokens ?? 0}`}
        delta={deltaSuffix(m.output_tokens, compareWith?.metrics?.output_tokens, true)}
      />
    </div>
  )
}

function MetricCell({
  label,
  value,
  delta,
}: {
  label: string
  value: string
  delta: { text: string; tone: 'good' | 'bad' | 'neutral' } | null
}) {
  return (
    <div className="bg-ink-200 border border-line rounded-md px-3 py-2">
      <div className="text-[10px] uppercase tracking-widest text-fog-500">
        {label}
      </div>
      <div className="text-sm font-mono text-fog-50 mt-0.5">{value}</div>
      {delta && (
        <div
          className={`text-[10px] mt-0.5 ${
            delta.tone === 'good'
              ? 'text-emerald-400'
              : delta.tone === 'bad'
                ? 'text-red-400'
                : 'text-fog-500'
          }`}
        >
          {delta.text}
        </div>
      )}
    </div>
  )
}

// For "lower is better" metrics (latency, tokens, tool calls), a smaller
// value than the comparison column is good news. We render the delta in
// percent so it reads at a glance.
function deltaSuffix(
  mine: number | undefined,
  theirs: number | undefined,
  lowerIsBetter: boolean,
): { text: string; tone: 'good' | 'bad' | 'neutral' } | null {
  if (mine == null || theirs == null) return null
  if (theirs === 0 && mine === 0) return null
  if (theirs === 0) return { text: 'baseline 0', tone: 'neutral' }
  const diff = mine - theirs
  const pct = Math.round((diff / theirs) * 100)
  if (pct === 0) return { text: 'tied', tone: 'neutral' }
  const sign = pct > 0 ? '+' : ''
  const tone: 'good' | 'bad' =
    (lowerIsBetter && pct < 0) || (!lowerIsBetter && pct > 0) ? 'good' : 'bad'
  return { text: `${sign}${pct}% vs other`, tone }
}

function HeaderTheme() {
  const [theme, setTheme] = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      className="w-7 h-7 rounded-full text-fog-300 hover:text-fog-50 hover:bg-soft/[0.08] flex items-center justify-center transition"
    >
      {dark ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      ) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      )}
    </button>
  )
}
