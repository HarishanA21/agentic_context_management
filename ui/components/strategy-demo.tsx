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

// Models surfaced at the top of the picker (kept in sync with the backend's
// promoted/pinned list and the chat picker's PRIMARY_MODEL_IDS).
const PRIMARY_MODEL_IDS = [
  'google/gemini-3.1-flash-lite',
  'minimax/minimax-m3',
  'stepfun/step-3.7-flash',
]

type ToolEvent = { name: string; args_keys: string[] }
type Metrics = {
  latency_ms: number
  tool_calls?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  // PR #5 — image-block visibility. The provider's input_tokens
  // already includes the billed image cost; these counts just prove
  // the compressed columns actually sent images.
  image_blocks?: number
  image_messages?: number
  // Image-recall caching: prompt tokens served from / written to the
  // provider cache across this column's model calls.
  cache_read_tokens?: number
  cache_write_tokens?: number
  cache_hit_ratio?: number | null
}
type CriterionResult = {
  score: number
  baseline_answer?: string
  candidate_answer?: string
  match?: boolean
  exact_values?: string[]
  mutated_values?: string[]
  covered?: string[]
  missing?: string[]
  hallucinated_claims?: string[]
}
type Accuracy = {
  score: number
  reasoning?: string
  criteria?: {
    c1_answer_correctness?: CriterionResult
    c2_value_fidelity?: CriterionResult
    c3_completeness?: CriterionResult
    c4_hallucination?: CriterionResult
  }
}
type Result = {
  index?: number
  strategy: string
  label?: string
  ok: boolean
  reply?: string
  error?: string
  metrics: Metrics
  tool_events: ToolEvent[]
  accuracy?: Accuracy
  context_window?: Array<[string, string]>
}
type CompressionRatios = {
  column_2_vs_1?: number | null
  column_4_vs_3?: number | null
  raw_cache_vs_image?: number | null
  raw_evict_vs_image?: number | null
  raw_cache_evict_vs_image?: number | null
  ts_cache_vs_image?: number | null
  ts_evict_vs_image?: number | null
  ts_cache_evict_vs_image?: number | null
}
type CompareResponse = {
  prompt: string
  model: string
  results: Result[]
  tab?: string                                   // PR #3
  judge_model?: string                           // PR #3
  compression_ratios?: CompressionRatios         // PR #3
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

// Prompts that exercise large tool outputs — best for showing off
// Tab 2's compression numbers.
const VISUAL_SAMPLE_PROMPTS = [
  'Read README.md and PROJECT.md in full, then write a paragraph summary that cites at least one heading from each.',
  'List every project file, read all .md files, and produce a ranked table by word count with the first heading per file.',
  'Use run_shell to capture the current `git log --oneline -20` output, then summarise the three biggest themes in the recent history.',
]

type TabId = 'current_methods' | 'visual_compression'

type ModelInfo = { id: string; name: string; context_length: number }

export function StrategyDemoPanel() {
  const [tab, setTab] = useState<TabId>('current_methods')
  const [prompt, setPrompt] = useState<string>(SAMPLE_PROMPTS[0])
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [response, setResponse] = useState<CompareResponse | null>(null)
  const samplePrompts = tab === 'visual_compression' ? VISUAL_SAMPLE_PROMPTS : SAMPLE_PROMPTS
  // Model picker — fetched from /api/models on mount. Selection
  // persists in localStorage under the same `selected_model` key the
  // main chat uses, so the demo defaults to whatever the user last
  // chose elsewhere.
  const [models, setModels] = useState<ModelInfo[]>([])
  const [defaultModel, setDefaultModel] = useState<string>('')
  const [selectedModel, setSelectedModel] = useState<string>('')
  const [activeModal, setActiveModal] = useState<{ type: 'context' | 'judge'; colIdx: number } | null>(null)
  // Tab 2 runs its 10 columns in batches of 2 (to dodge provider rate
  // limits). Columns land here by index as each batch returns, so the UI
  // fills in progressively. `batchProgress` drives the "X / 10" hint.
  const VISUAL_COLUMN_COUNT = 10
  const [visualCols, setVisualCols] = useState<(Result | null)[]>([])
  const [batchProgress, setBatchProgress] = useState<{ done: number; total: number } | null>(null)

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
      if (tab === 'visual_compression') {
        await runVisualStream()
      } else {
        const r = await authFetch('/api/demo/compare', {
          method: 'POST',
          body: JSON.stringify({
            prompt: prompt.trim(),
            strategies: ['tool_calling', 'ts_code_mode'],
            model: selectedModel || undefined,
          }),
        })
        if (!r.ok) {
          const b = await r.json().catch(() => ({}))
          setError(`${r.status} ${r.statusText} — ${b?.detail ?? 'unknown error'}`)
        } else {
          setResponse(await r.json())
        }
      }
    } catch (e: any) {
      setError(e?.message ?? 'network error')
    } finally {
      setRunning(false)
      setBatchProgress(null)
    }
  }

  // Tab 2: stream the 10 columns over SSE. The backend runs up to 4 in
  // parallel and pushes one `column` frame per column the instant its run +
  // judge finish, so the grid fills in live (in completion order). Column 0
  // is the ground-truth baseline judged at 100; the rest are scored against
  // it server-side.
  async function runVisualStream() {
    const cols: (Result | null)[] = Array(VISUAL_COLUMN_COUNT).fill(null)
    setVisualCols(cols.slice())
    setBatchProgress({ done: 0, total: VISUAL_COLUMN_COUNT })

    const r = await authFetch('/api/demo/compare/stream', {
      method: 'POST',
      body: JSON.stringify({
        prompt: prompt.trim(),
        model: selectedModel || undefined,
        tab: 'visual_compression',
      }),
    })
    if (!r.ok || !r.body) {
      const b = await r.json().catch(() => ({}))
      setError(`${r.status} ${r.statusText} — ${b?.detail ?? 'stream failed'}`)
      return
    }

    const reader = r.body.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let done = 0

    const handle = (evt: any) => {
      if (evt?.type === 'meta') {
        // Carries prompt/model/judge_model for the footer + total/concurrency.
        setResponse({
          prompt: evt.prompt,
          model: evt.model,
          judge_model: evt.judge_model,
          results: [],
        })
        setBatchProgress({ done: 0, total: evt.total ?? VISUAL_COLUMN_COUNT })
      } else if (evt?.type === 'column' && evt.result) {
        const res: Result = evt.result
        const idx = res.index ?? -1
        if (idx >= 0 && idx < VISUAL_COLUMN_COUNT) {
          cols[idx] = res
          setVisualCols(cols.slice()) // live, per-column render
          done += 1
          setBatchProgress({ done, total: VISUAL_COLUMN_COUNT })
        }
      }
    }

    // Parse the SSE byte stream: frames are separated by a blank line, each
    // carrying a single `data: {json}` line.
    while (true) {
      const { value, done: streamDone } = await reader.read()
      if (streamDone) break
      buf += decoder.decode(value, { stream: true })
      const frames = buf.split('\n\n')
      buf = frames.pop() ?? ''
      for (const frame of frames) {
        const line = frame.split('\n').find((l) => l.startsWith('data:'))
        if (!line) continue
        try {
          handle(JSON.parse(line.slice(5).trim()))
        } catch {
          // ignore keepalives / malformed frames
        }
      }
    }
  }

  // Tab 1 ordering: tool_calling left, ts_code_mode right. (Tab 2 renders
  // straight off `visualCols`, which is already index-aligned.)
  const ordered: Result[] = (() => {
    const r = response?.results ?? []
    return r.slice().sort((a, b) => {
      const rank = (s: string) => (s === 'tool_calling' ? 0 : 1)
      return rank(a.strategy) - rank(b.strategy)
    })
  })()

  function switchTab(next: TabId) {
    if (next === tab) return
    setTab(next)
    setResponse(null)
    setVisualCols([])
    setBatchProgress(null)
    setError(null)
    setPrompt((cur) => {
      const samples = next === 'visual_compression' ? VISUAL_SAMPLE_PROMPTS : SAMPLE_PROMPTS
      // If the current prompt is one of the *other* tab's samples,
      // swap to the matching sample for the new tab so the user lands
      // on a sensible default. Otherwise keep their custom prompt.
      if (SAMPLE_PROMPTS.includes(cur) || VISUAL_SAMPLE_PROMPTS.includes(cur)) {
        return samples[0]
      }
      return cur
    })
  }

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
            {tab === 'current_methods' ? (
              <p className="text-sm text-fog-400 mt-1.5 max-w-3xl">
                Run the same prompt under <strong>Tool Calling</strong> and{' '}
                <strong>TypeScript Code Mode</strong> at the same time. Each
                call uses a throwaway in-memory checkpointer — nothing
                leaks into your chat history. Compare round-trip count,
                token usage, latency and the final answer.
              </p>
            ) : (
              <p className="text-sm text-fog-400 mt-1.5 max-w-3xl">
                Four columns, same prompt, same model. Columns 2 and 4
                swap each tool's raw-text return for a compressed
                2-column image + a text REFERENCES block (research
                paper 21_ENG_009, <code>image_format_2col_index</code>
                method). The two compressed columns should hit{' '}
                <strong>≥ 80 %</strong> of baseline accuracy while
                cutting tool tokens by <strong>60-80 %</strong>.
                Recommended model: <strong>Gemini 2.5 Flash</strong>.
                Accuracy is judged by GPT-4o.
              </p>
            )}
          </div>

          {/* Tab strip */}
          <div className="flex items-center gap-1 mb-4 border-b border-line">
            <TabButton
              active={tab === 'current_methods'}
              label="Current methods"
              onClick={() => switchTab('current_methods')}
            />
            <TabButton
              active={tab === 'visual_compression'}
              label="Visual compression bench"
              onClick={() => switchTab('visual_compression')}
            />
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
                {samplePrompts.map((p, i) => (
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
                      {(() => {
                        const primary = PRIMARY_MODEL_IDS
                          .map((id) => models.find((m) => m.id === id))
                          .filter(Boolean) as ModelInfo[]
                        const rest = models.filter((m) => !PRIMARY_MODEL_IDS.includes(m.id))
                        const opt = (m: ModelInfo) => (
                          <option key={m.id} value={m.id} className="bg-ink-200 text-fog-50">
                            {m.name.replace(/\s*\(free\)\s*$/i, '')}
                          </option>
                        )
                        return (
                          <>
                            {primary.length > 0 && (
                              <optgroup label="Primary models">{primary.map(opt)}</optgroup>
                            )}
                            {rest.length > 0 && (
                              <optgroup label="Other models">{rest.map(opt)}</optgroup>
                            )}
                          </>
                        )
                      })()}
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
                  {running
                    ? tab === 'visual_compression'
                      ? 'Streaming 10 columns…'
                      : 'Running both strategies…'
                    : 'Run comparison'}
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

          {tab === 'current_methods' ? (
            /* Tab 1: 2-column compare (unchanged). */
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
          ) : (
            /* Tab 2: 10-column visual bench. Cols 0-3 are the baseline
               methods; cols 4-9 apply the 3 image-recall techniques to the
               raw-image (vs col 1) and TS+image (vs col 3) methods. */
            <>
              {batchProgress && (
                <div className="mb-3 flex items-center gap-2 text-[11px] text-fog-400">
                  <div className="flex-1 h-1.5 rounded-full bg-soft/[0.06] overflow-hidden">
                    <div
                      className="h-full bg-accent transition-all"
                      style={{ width: `${(batchProgress.done / batchProgress.total) * 100}%` }}
                    />
                  </div>
                  <span className="font-mono">
                    {batchProgress.done} / {batchProgress.total} columns
                    {batchProgress.done < batchProgress.total ? ' · streaming, 4 in parallel' : ' · done'}
                  </span>
                </div>
              )}
              {(() => {
                // Compression ratios computed client-side from the columns
                // gathered so far (each image-recall column vs the image
                // method it builds on). Null until both columns are present.
                const inTok = (i: number) => visualCols[i]?.metrics?.input_tokens ?? 0
                const ratio = (after: number, before: number): number | null => {
                  const b = inTok(before)
                  if (!b || !visualCols[after]) return null
                  return Math.max(0, b - inTok(after)) / b
                }
                // index → compare index (for token delta + savings badge).
                const cmp: Record<number, number | null> = {
                  0: null, 1: 0, 2: null, 3: 2,
                  4: 1, 5: 1, 6: 1, 7: 3, 8: 3, 9: 3,
                }
                const renderCol = (idx: number) => {
                  const r = visualCols[idx] ?? null
                  const compareIdx = cmp[idx]
                  const compareWith = compareIdx != null ? visualCols[compareIdx] ?? null : null
                  return (
                    <ResultColumn
                      key={idx}
                      strategy={r?.strategy ?? ''}
                      label={r?.label ?? `Column ${idx + 1}`}
                      loading={running && !r}
                      result={r}
                      compareWith={compareWith}
                      compressionRatio={compareIdx != null ? ratio(idx, compareIdx) ?? undefined : undefined}
                      showAccuracy
                      onContextWindow={r ? () => setActiveModal({ type: 'context', colIdx: idx }) : undefined}
                      onJudgeResult={r?.accuracy ? () => setActiveModal({ type: 'judge', colIdx: idx }) : undefined}
                    />
                  )
                }
                return (
                  <div className="flex flex-col gap-4">
                    <div>
                      <div className="text-xs font-semibold text-fog-200 mb-2">Baseline methods</div>
                      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
                        {[0, 1, 2, 3].map(renderCol)}
                      </div>
                    </div>
                    <div>
                      <div className="text-xs font-semibold text-fog-200 mb-2">
                        Image-recall techniques · raw-image (vs col 2) &amp; TS+image (vs col 4)
                      </div>
                      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                        {[4, 5, 6, 7, 8, 9].map(renderCol)}
                      </div>
                    </div>
                  </div>
                )
              })()}
              {response?.judge_model && (
                <div className="mt-3 text-[11px] text-fog-500">
                  Accuracy judged by{' '}
                  <span className="text-fog-300">{response.judge_model}</span>{' '}
                  — Column 1 is the baseline (100 / 100). Cache/evict columns
                  reuse the image method they sit under.
                </div>
              )}

              {activeModal && visualCols[activeModal.colIdx] && (
                activeModal.type === 'context' ? (
                  <ContextWindowModal
                    label={visualCols[activeModal.colIdx]!.label ?? `Column ${activeModal.colIdx + 1}`}
                    contextWindow={visualCols[activeModal.colIdx]!.context_window ?? []}
                    onClose={() => setActiveModal(null)}
                  />
                ) : (
                  <JudgeResultModal
                    label={visualCols[activeModal.colIdx]!.label ?? `Column ${activeModal.colIdx + 1}`}
                    accuracy={visualCols[activeModal.colIdx]!.accuracy!}
                    onClose={() => setActiveModal(null)}
                  />
                )
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function TabButton({
  active,
  label,
  onClick,
}: {
  active: boolean
  label: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-sm border-b-2 -mb-px transition ${
        active
          ? 'border-accent text-fog-50'
          : 'border-transparent text-fog-400 hover:text-fog-200'
      }`}
    >
      {label}
    </button>
  )
}

function ResultColumn({
  strategy,
  label,
  loading,
  result,
  compareWith,
  compressionRatio,
  showAccuracy,
  onContextWindow,
  onJudgeResult,
}: {
  strategy: string
  label?: string
  loading: boolean
  result: Result | null
  compareWith: Result | null
  compressionRatio?: number
  showAccuracy?: boolean
  onContextWindow?: () => void
  onJudgeResult?: () => void
}) {
  const displayLabel = label ?? STRATEGY_LABEL[strategy] ?? strategy
  // Mark the "+ Image" columns with the accent colour so eyes find them.
  const isCompressedCol = (label ?? '').toLowerCase().includes('+ image')
  const accent =
    isCompressedCol || strategy === 'ts_code_mode' ? 'text-accent' : 'text-fog-200'

  return (
    <div className="surface p-4 flex flex-col gap-4 min-h-[260px]">
      <div className="flex items-center justify-between gap-2">
        <h2 className={`text-sm font-semibold ${accent}`}>{displayLabel}</h2>
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

          {/* PR #3 — extra rows for Tab 2 */}
          {compressionRatio != null && (
            <div className="text-[11px] flex items-baseline justify-between gap-2 bg-emerald-500/8 border border-emerald-500/25 rounded-md px-3 py-2">
              <span className="text-fog-400 uppercase tracking-widest">
                Compression vs baseline
              </span>
              <span className="text-emerald-300 font-mono">
                ↓ {(compressionRatio * 100).toFixed(1)} % input tokens
              </span>
            </div>
          )}

          {/* PR #5 — image-block visibility. Shown only when at least
              one image actually crossed the wire, so the baseline /
              text-only columns stay quiet. */}
          {(result.metrics.image_blocks ?? 0) > 0 && (
            <div className="text-[11px] flex items-baseline justify-between gap-2 text-fog-400">
              <span className="uppercase tracking-widest">Images sent</span>
              <span className="font-mono text-fog-200">
                🖼️ {result.metrics.image_blocks} block
                {(result.metrics.image_blocks ?? 0) === 1 ? '' : 's'} in{' '}
                {result.metrics.image_messages} message
                {(result.metrics.image_messages ?? 0) === 1 ? '' : 's'}
              </span>
            </div>
          )}

          {/* Image-recall caching — shown only when the provider reported
              cache activity (cache columns on cache-capable providers). */}
          {((result.metrics.cache_read_tokens ?? 0) > 0 ||
            (result.metrics.cache_write_tokens ?? 0) > 0) && (
            <div className="text-[11px] flex items-baseline justify-between gap-2 bg-sky-500/8 border border-sky-500/25 rounded-md px-3 py-2">
              <span className="text-fog-400 uppercase tracking-widest">Cache</span>
              <span className="font-mono text-sky-300">
                ⚡ {(result.metrics.cache_read_tokens ?? 0).toLocaleString()} read
                {(result.metrics.cache_write_tokens ?? 0) > 0 &&
                  ` · ${(result.metrics.cache_write_tokens ?? 0).toLocaleString()} write`}
                {result.metrics.cache_hit_ratio != null &&
                  ` · ${(result.metrics.cache_hit_ratio * 100).toFixed(0)}% hit`}
              </span>
            </div>
          )}

          {showAccuracy && result.accuracy && (
            <AccuracyRow accuracy={result.accuracy} />
          )}

          {(onContextWindow || onJudgeResult) && (
            <div className="flex gap-2">
              {onContextWindow && (
                <button
                  onClick={onContextWindow}
                  className="flex-1 text-[11px] py-1.5 rounded-md border border-line text-fog-300 hover:text-fog-50 hover:border-lineStrong hover:bg-soft/[0.06] transition"
                >
                  Context Window
                </button>
              )}
              {onJudgeResult && (
                <button
                  onClick={onJudgeResult}
                  className="flex-1 text-[11px] py-1.5 rounded-md border border-line text-fog-300 hover:text-fog-50 hover:border-lineStrong hover:bg-soft/[0.06] transition"
                >
                  Judge Result
                </button>
              )}
            </div>
          )}

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

function AccuracyRow({ accuracy }: { accuracy: Accuracy }) {
  const score = Math.max(-1, Math.min(100, accuracy.score))
  if (score < 0) {
    return (
      <div className="text-[11px] bg-ink-200 border border-line rounded-md px-3 py-2 text-fog-400">
        <span className="uppercase tracking-widest">Accuracy</span>{' '}
        <span className="text-red-300 font-mono">unavailable</span>
        {accuracy.reasoning && (
          <span className="ml-1 text-fog-500">— {accuracy.reasoning}</span>
        )}
      </div>
    )
  }
  const tone =
    score >= 80
      ? 'bg-emerald-500'
      : score >= 60
        ? 'bg-amber-500'
        : 'bg-red-500'
  return (
    <div className="bg-ink-200 border border-line rounded-md px-3 py-2 space-y-1">
      <div className="text-[11px] flex items-baseline justify-between gap-2">
        <span className="uppercase tracking-widest text-fog-400">Accuracy</span>
        <span className="text-fog-100 font-mono">{score} / 100</span>
      </div>
      <div className="h-1.5 rounded-full bg-soft/[0.08] overflow-hidden">
        <div
          className={`h-full ${tone} transition-all`}
          style={{ width: `${score}%` }}
        />
      </div>
      {accuracy.reasoning && (
        <div className="text-[10px] text-fog-500 leading-snug">
          {accuracy.reasoning}
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

function Modal({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: React.ReactNode
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="relative bg-ink-100 border border-line rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col">
        <div className="flex items-center justify-between px-5 py-3 border-b border-line shrink-0">
          <span className="text-sm font-semibold text-fog-100">{title}</span>
          <button
            onClick={onClose}
            className="w-6 h-6 flex items-center justify-center rounded text-fog-400 hover:text-fog-50 hover:bg-soft/[0.08] transition text-base leading-none"
          >
            ×
          </button>
        </div>
        <div className="overflow-y-auto flex-1 p-5">{children}</div>
      </div>
    </div>
  )
}

function ContextWindowModal({
  label,
  contextWindow,
  onClose,
}: {
  label: string
  contextWindow: Array<[string, string]>
  onClose: () => void
}) {
  const isImageMethod = label.toLowerCase().includes('+ image')
  return (
    <Modal title={`Context Window — ${label}`} onClose={onClose}>
      {isImageMethod && (
        <div className="mb-4 text-[11px] bg-amber-500/10 border border-amber-500/30 rounded-md px-3 py-2 text-amber-300">
          This method converts tool outputs to images before sending to the LLM.
          The text below is the raw content before image rendering.
        </div>
      )}
      {contextWindow.length === 0 ? (
        <p className="text-sm text-fog-500">No tool outputs were recorded for this run.</p>
      ) : (
        <div className="space-y-4">
          {contextWindow.map(([toolName, text], i) => (
            <div key={i}>
              <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-1.5">
                Tool: <span className="text-fog-300 normal-case font-mono">{toolName}</span>
              </div>
              <pre className="text-xs font-mono text-fog-200 bg-ink-200 border border-line rounded-md p-3 whitespace-pre-wrap break-all leading-relaxed overflow-x-auto">
                {text || '(empty output)'}
              </pre>
            </div>
          ))}
        </div>
      )}
    </Modal>
  )
}

const CRITERIA_META: Record<string, { label: string; description: string; primary?: boolean }> = {
  c1_answer_correctness: {
    label: 'C1 — Answer Correctness',
    description: 'Does the final answer match the baseline?',
  },
  c2_value_fidelity: {
    label: 'C2 — Value Fidelity',
    description: 'Are exact numbers / strings reproduced without mutation?',
    primary: true,
  },
  c3_completeness: {
    label: 'C3 — Completeness',
    description: 'Were all sub-tasks from the baseline addressed?',
  },
  c4_hallucination: {
    label: 'C4 — Hallucination',
    description: 'Did the candidate add claims not in the baseline? (25 = none)',
    primary: true,
  },
}

function JudgeResultModal({
  label,
  accuracy,
  onClose,
}: {
  label: string
  accuracy: Accuracy
  onClose: () => void
}) {
  const score = Math.max(-1, Math.min(100, accuracy.score))
  const tone = score >= 80 ? 'text-emerald-400' : score >= 60 ? 'text-amber-400' : 'text-red-400'
  const bar = score >= 80 ? 'bg-emerald-500' : score >= 60 ? 'bg-amber-500' : 'bg-red-500'

  return (
    <Modal title={`Judge Result — ${label}`} onClose={onClose}>
      <div className="space-y-5">
        {/* Overall score */}
        <div className="bg-ink-200 border border-line rounded-md px-4 py-3 space-y-2">
          <div className="flex items-baseline justify-between">
            <span className="text-[11px] uppercase tracking-widest text-fog-400">Overall Score</span>
            <span className={`text-lg font-mono font-semibold ${tone}`}>{score < 0 ? 'N/A' : `${score} / 100`}</span>
          </div>
          {score >= 0 && (
            <div className="h-2 rounded-full bg-soft/[0.08] overflow-hidden">
              <div className={`h-full ${bar} transition-all`} style={{ width: `${score}%` }} />
            </div>
          )}
          {accuracy.reasoning && (
            <p className="text-xs text-fog-400 leading-snug">{accuracy.reasoning}</p>
          )}
        </div>

        {/* Per-criterion breakdown */}
        {accuracy.criteria && Object.keys(accuracy.criteria).length > 0 ? (
          <div className="space-y-3">
            <div className="text-[11px] uppercase tracking-widest text-fog-500">Criteria Breakdown</div>
            {(Object.entries(accuracy.criteria) as [string, CriterionResult][]).map(([key, c]) => {
              const meta = CRITERIA_META[key]
              const criterionTone = c.score >= 20 ? 'text-emerald-400' : c.score >= 13 ? 'text-amber-400' : 'text-red-400'
              const criterionBar = c.score >= 20 ? 'bg-emerald-500' : c.score >= 13 ? 'bg-amber-500' : 'bg-red-500'
              return (
                <div key={key} className="bg-ink-200 border border-line rounded-md px-3 py-2.5 space-y-2">
                  <div className="flex items-baseline justify-between gap-2">
                    <div>
                      <span className={`text-[11px] font-medium ${meta?.primary ? 'text-fog-100' : 'text-fog-300'}`}>
                        {meta?.label ?? key}
                      </span>
                      {meta?.primary && (
                        <span className="ml-1.5 text-[9px] uppercase tracking-wider text-accent">primary</span>
                      )}
                      {meta?.description && (
                        <div className="text-[10px] text-fog-500 mt-0.5">{meta.description}</div>
                      )}
                    </div>
                    <span className={`text-sm font-mono font-semibold shrink-0 ${criterionTone}`}>{c.score} / 25</span>
                  </div>
                  <div className="h-1 rounded-full bg-soft/[0.08] overflow-hidden">
                    <div className={`h-full ${criterionBar}`} style={{ width: `${(c.score / 25) * 100}%` }} />
                  </div>

                  {/* Evidence details */}
                  {key === 'c1_answer_correctness' && (
                    <div className="text-[10px] text-fog-500 space-y-0.5">
                      {c.baseline_answer && <div><span className="text-fog-400">Baseline:</span> {c.baseline_answer}</div>}
                      {c.candidate_answer && <div><span className="text-fog-400">Candidate:</span> {c.candidate_answer}</div>}
                      {c.match != null && (
                        <div className={c.match ? 'text-emerald-400' : 'text-red-400'}>
                          {c.match ? 'Answers match' : 'Answers differ'}
                        </div>
                      )}
                    </div>
                  )}
                  {key === 'c2_value_fidelity' && (
                    <EvidenceLists good={c.exact_values} bad={c.mutated_values} goodLabel="Exact" badLabel="Mutated" />
                  )}
                  {key === 'c3_completeness' && (
                    <EvidenceLists good={c.covered} bad={c.missing} goodLabel="Covered" badLabel="Missing" />
                  )}
                  {key === 'c4_hallucination' && c.hallucinated_claims && c.hallucinated_claims.length > 0 && (
                    <div className="text-[10px] space-y-0.5">
                      <div className="text-red-400">Hallucinated claims:</div>
                      {c.hallucinated_claims.map((item, i) => (
                        <div key={i} className="text-fog-400 pl-2">• {item}</div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ) : (
          <p className="text-xs text-fog-500">
            {label.toLowerCase().includes('baseline')
              ? 'Baseline column — no judge evaluation needed.'
              : 'No per-criterion data available.'}
          </p>
        )}
      </div>
    </Modal>
  )
}

function EvidenceLists({
  good,
  bad,
  goodLabel,
  badLabel,
}: {
  good?: string[]
  bad?: string[]
  goodLabel: string
  badLabel: string
}) {
  if (!good?.length && !bad?.length) return null
  return (
    <div className="text-[10px] space-y-1">
      {good && good.length > 0 && (
        <div>
          <span className="text-emerald-400">{goodLabel}:</span>
          {good.map((item, i) => <div key={i} className="text-fog-400 pl-2">• {item}</div>)}
        </div>
      )}
      {bad && bad.length > 0 && (
        <div>
          <span className="text-red-400">{badLabel}:</span>
          {bad.map((item, i) => <div key={i} className="text-fog-400 pl-2">• {item}</div>)}
        </div>
      )}
    </div>
  )
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
