'use client'

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { supabase, authFetch, authToken } from '@/lib/supabase'
import { useTheme } from '@/lib/theme'
import { MCPInventoryPanel } from '@/components/mcp-inventory'
import {
  SkillsInventoryPanel,
  SkillsComposerFlyout,
} from '@/components/skills-inventory'
import { PluginsInventoryPanel } from '@/components/plugins-inventory'
import { StrategyDemoPanel } from '@/components/strategy-demo'
import { ContextProfilesPanel } from '@/components/context-profiles'
import { RelevanceCleanupSection } from '@/components/relevance-cleanup'

// Models surfaced at the top of every model picker (chat + demo). Kept in
// sync with the backend's _PROMOTED_PAID_MODELS / _PINNED_MODELS so the
// requested primary models are always one click away rather than buried in
// the alphabetical free-tier list.
const PRIMARY_MODEL_IDS = [
  'google/gemini-3.1-flash-lite',
  'minimax/minimax-m3',
  'stepfun/step-3.7-flash',
]

/* ─────────────────────────── types ─────────────────────────── */

type Session = {
  id: string
  name: string
  created_at: string
  tokens?: number
  mode?: 'auto' | 'confirm'
}
type Thread = {
  id: string
  session_id: string
  name: string
  created_at: string
  tokens?: number
}
type ToolCall = { name: string; args: Record<string, unknown> }
type Message = {
  id?: number
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_name?: string
  tool_calls?: ToolCall[]
}
type Kind = 'project' | 'chat'
// One row in the live activity stream — represents a tool the agent has
// invoked but whose persisted result hasn't arrived yet. Replaced by the
// real tool message as soon as `_record_message` fires its SSE event.
type InflightTool = {
  run_id: string
  tool_name: string
  args: Record<string, any>
  started_at: number
}

type WorkspaceCommit = {
  id: number
  sha: string
  message: string
  pushed_at: string | null
  reverted_at: string | null
  created_at: string
  status: 'local' | 'pushed' | 'reverted'
}
// What the agent is asking permission to do, surfaced when the session is in
// Confirm mode. Matches the shape published from the tool's interrupt() call.
type PendingApproval =
  | {
      kind: 'approval_request'
      tool: 'write_project_file'
      filename: string
      size: number
      preview?: string
    }
  | {
      kind: 'approval_request'
      tool: 'run_shell'
      cmd: string
      cwd: string
      timeout: number
    }
  | {
      kind: 'approval_request'
      tool: string
      [k: string]: any
    }

/** Walk a message list and find the args the agent passed when it invoked
 *  each tool message. The assistant message immediately preceding a tool
 *  result contains `tool_calls` with the args we need to render rich rows
 *  (filename for read/write, cmd for shell, etc.). Returns a parallel array
 *  indexed by message position. */
function pairToolArgs(messages: Message[]): Array<Record<string, any> | undefined> {
  const out: Array<Record<string, any> | undefined> = new Array(messages.length)
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (m.role !== 'tool') continue
    for (let j = i - 1; j >= 0; j--) {
      const prev = messages[j]
      if (prev.role === 'user') break
      if (prev.role === 'assistant' && prev.tool_calls?.length) {
        const match = prev.tool_calls.find((c) => c.name === m.tool_name)
        if (match) {
          out[i] = match.args as Record<string, any>
          break
        }
      }
    }
  }
  return out
}

/** Merge a single SSE-pushed message into the local message list.
 *  - Skip if we already have a row with this `id` (idempotent on re-delivery).
 *  - If incoming is a user message that matches an id-less optimistic entry
 *    by content, replace it in place so the entry gains its real id.
 *  - Otherwise append. */
function mergeIncomingMessage(prev: Message[], incoming: Message): Message[] {
  if (incoming.id != null && prev.some((m) => m.id === incoming.id)) {
    return prev
  }
  if (incoming.role === 'user' && incoming.id != null) {
    const idx = prev.findIndex(
      (m) =>
        m.role === 'user' && m.id == null && m.content === incoming.content,
    )
    if (idx >= 0) {
      return [...prev.slice(0, idx), incoming, ...prev.slice(idx + 1)]
    }
  }
  return [...prev, incoming]
}

/* ─────────────────────────── localStorage classification ─────────────────── */
// Project/chat distinction lives client-side because the backend `sessions`
// table has no `kind` column. Per-device only — sessions created on another
// device will default to 'project'.

const KIND_KEY = 'agent.session_kinds'
const FILES_KEY = 'agent.project_files'
const SESSION_NAMES_KEY = 'agent.session_names'
const THREAD_NAMES_KEY = 'agent.thread_names'

function loadKinds(): Record<string, Kind> {
  if (typeof window === 'undefined') return {}
  try {
    return JSON.parse(localStorage.getItem(KIND_KEY) || '{}')
  } catch {
    return {}
  }
}
function saveKind(id: string, kind: Kind) {
  const all = loadKinds()
  all[id] = kind
  localStorage.setItem(KIND_KEY, JSON.stringify(all))
}
function loadFiles(): Record<string, string[]> {
  if (typeof window === 'undefined') return {}
  try {
    return JSON.parse(localStorage.getItem(FILES_KEY) || '{}')
  } catch {
    return {}
  }
}
function saveFiles(id: string, files: string[]) {
  const all = loadFiles()
  all[id] = files
  localStorage.setItem(FILES_KEY, JSON.stringify(all))
}
function loadNameMap(key: string): Record<string, string> {
  if (typeof window === 'undefined') return {}
  try {
    return JSON.parse(localStorage.getItem(key) || '{}')
  } catch {
    return {}
  }
}
function persistName(key: string, id: string, name: string) {
  const all = loadNameMap(key)
  all[id] = name
  localStorage.setItem(key, JSON.stringify(all))
}

/** Compact token-count formatter: 245, 1.2k, 23k, 1.4M */
function fmtTokens(n: number | undefined): string {
  if (!n || n <= 0) return ''
  if (n < 1000) return String(n)
  if (n < 1_000_000) {
    const k = n / 1000
    return k >= 10 ? `${Math.round(k)}k` : `${k.toFixed(1).replace(/\.0$/, '')}k`
  }
  const m = n / 1_000_000
  return m >= 10 ? `${Math.round(m)}M` : `${m.toFixed(1).replace(/\.0$/, '')}M`
}

/** Heuristic title from a user message — first non-empty line, first 6 words,
 *  capped at 50 chars, trailing punctuation stripped, capitalized. */
function deriveTitle(text: string): string {
  if (!text) return ''
  const firstLine =
    text
      .split('\n')
      .map((l) => l.trim())
      .find(Boolean) || text.trim()
  let title = firstLine.split(/\s+/).slice(0, 6).join(' ')
  if (title.length > 50) title = title.slice(0, 50).trim()
  title = title.replace(/[.!?,;:'"`]+$/, '')
  if (!title) return ''
  return title.charAt(0).toUpperCase() + title.slice(1)
}

/* ───────────────────────────── component ─────────────────────────── */

export default function AppPage() {
  const router = useRouter()
  const [userEmail, setUserEmail] = useState<string | null>(null)
  const [ready, setReady] = useState(false)

  const [sessions, setSessions] = useState<Session[]>([])
  const [kinds, setKinds] = useState<Record<string, Kind>>({})
  const [filesMap, setFilesMap] = useState<Record<string, string[]>>({})
  const [sessionNames, setSessionNames] = useState<Record<string, string>>({})
  const [threadNames, setThreadNames] = useState<Record<string, string>>({})

  const [threadsMap, setThreadsMap] = useState<Record<string, Thread[]>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [activeThread, setActiveThread] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])

  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  // 'chat': linear conversation. 'task': three-pane (activity / diff / chat).
  // Project sessions only.
  const [viewMode, setViewMode] = useState<'chat' | 'task'>('chat')

  const [newMenuOpen, setNewMenuOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  // MCP inventory takes over the main pane when open — exclusive with the
  // chat view; clicking any session/project/chat row closes it.
  const [mcpsOpen, setMcpsOpen] = useState(false)
  // Strategy comparison demo — runs both context-management strategies
  // in parallel and shows tokens / latency / tool-call counts side by
  // side. Same exclusivity model as MCPs.
  const [demoOpen, setDemoOpen] = useState(false)
  // PR #8: context-profile manager panel. Opens in-place like MCPs/Demo.
  const [contextProfilesOpen, setContextProfilesOpen] = useState(false)
  // Skills manage panel — swaps into the main pane like MCPs/Demo.
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [projectModalOpen, setProjectModalOpen] = useState(false)

  // Per-row "⋯" menu — keyed by `${kind}:${id}` so chats and threads don't collide
  const [rowMenuKey, setRowMenuKey] = useState<string | null>(null)
  // Inline rename state
  const [renameKey, setRenameKey] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  // Lightweight toast
  const [toast, setToast] = useState<string | null>(null)
  // Centered confirm dialog (replaces window.confirm)
  const [confirmDialog, setConfirmDialog] = useState<{
    title: string
    message: string
    confirmLabel?: string
    danger?: boolean
    onConfirm: () => void
  } | null>(null)
  // Centered prompt dialog (replaces window.prompt)
  const [promptDialog, setPromptDialog] = useState<{
    title: string
    message?: string
    placeholder?: string
    initialValue?: string
    confirmLabel?: string
    onConfirm: (value: string) => void
  } | null>(null)
  // Right-side file viewer
  const [viewerFile, setViewerFile] = useState<{
    sessionId: string
    name: string
  } | null>(null)
  const [viewerContent, setViewerContent] = useState<string>('')
  const [viewerLoading, setViewerLoading] = useState(false)
  const [viewerError, setViewerError] = useState<string | null>(null)
  // Files picked but not yet uploaded — uploaded together with the next send.
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  // GitHub PAT connection state
  const [githubModalOpen, setGithubModalOpen] = useState(false)
  const [githubUsername, setGithubUsername] = useState<string | null>(null)
  // Context window viewer
  const [contextOpen, setContextOpen] = useState(false)
  const [contextData, setContextData] = useState<any>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [contextError, setContextError] = useState<string | null>(null)
  // Lightweight summary kept in sync with the live thread so the floating
  // ring button can show a percentage without opening the full viewer.
  const [contextSummary, setContextSummary] = useState<{
    total: number
    limit: number
    percent: number
  } | null>(null)
  // Workspace history (commit timeline + revert)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyData, setHistoryData] = useState<WorkspaceCommit[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [revertingId, setRevertingId] = useState<number | null>(null)
  // Confirm-mode approval gate
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(null)
  const [resolvingApproval, setResolvingApproval] = useState(false)
  // Replay mode: when non-null, only the first N messages are rendered.
  // Useful for stepping through a finished agent run after the fact.
  const [replayIdx, setReplayIdx] = useState<number | null>(null)
  // Tools the agent has started but not yet persisted. Keyed by run_id;
  // SSE `tool_started` adds, `tool_finished` removes. Renders as in-flight
  // event rows below the static messages.
  const [inflightTools, setInflightTools] = useState<InflightTool[]>([])
  // Coarse signal — was the LLM most recently observed thinking?
  const [llmThinking, setLlmThinking] = useState(false)
  // Tokens accumulated from the current `llm_token` SSE stream — rendered as
  // a transient assistant message preview so text appears character-by-
  // character. Cleared when the persisted assistant `message` event lands.
  const [liveTokens, setLiveTokens] = useState('')
  // Composer "+" attach menu
  const [composerMenuOpen, setComposerMenuOpen] = useState(false)
  // Skills quick-toggle flyout that opens to the side of the composer "+" menu.
  const [skillsFlyoutOpen, setSkillsFlyoutOpen] = useState(false)
  // "/" slash-menu: pick a skill to force-activate it for the next message.
  const [slashMenuOpen, setSlashMenuOpen] = useState(false)
  const [slashIndex, setSlashIndex] = useState(0)
  const [skillList, setSkillList] = useState<
    { name: string; description: string }[]
  >([])
  // Skills the user activated via "/" for the next message (names).
  const [triggeredSkills, setTriggeredSkills] = useState<string[]>([])
  // Plugins manage page — swaps into the main pane like Skills/MCPs.
  const [pluginsOpen, setPluginsOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  // Model picker
  const [models, setModels] = useState<
    { id: string; name: string; context_length: number; vision?: boolean }[]
  >([])
  const [selectedModel, setSelectedModel] = useState<string>('')
  // Phase F: user's configured LLM providers + per-session preference.
  // When `providers.length > 0`, the chat-header picker switches from
  // OpenRouter free-model list to a provider list and sets the session's
  // preferred_provider_id on change.
  const [providers, setProviders] = useState<
    { id: string; slug: string; label: string; model_id: string; is_default: boolean }[]
  >([])
  // Map of session_id → preferred_provider_id (null = "use user default").
  const [sessionProviders, setSessionProviders] = useState<
    Record<string, string | null>
  >({})
  // Context-management strategy picker. Loaded from /context/strategies
  // on mount; choice persists in localStorage. Sent on every /chat and
  // /chat/resume so the backend can rebuild the agent if it differs
  // from the default — see _get_agent_for_request.
  const [strategies, setStrategies] = useState<
    { id: string; label: string; summary: string }[]
  >([])
  const [defaultStrategy, setDefaultStrategy] = useState<string>('tool_calling')
  const [selectedStrategy, setSelectedStrategy] = useState<string>('')
  // PR #8: context-management profile picker (richer than the strategy
  // string — bundles tool surface + per-technique toggles). When a
  // profile id is set we send it as context_profile_id on /chat; the
  // backend's resolve_profile then prefers it over context_strategy.
  const [profiles, setProfiles] = useState<
    {
      id: string
      name: string
      built_in: boolean
      summary: string | null
      is_default: boolean
      body: any
    }[]
  >([])
  const [selectedProfileId, setSelectedProfileId] = useState<string>('')
  const composerMenuRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const photoInputRef = useRef<HTMLInputElement>(null)

  const endRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const newBtnWrapRef = useRef<HTMLDivElement>(null)

  /* ─── auth + initial load ─── */
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) {
        router.replace('/login')
        return
      }
      setUserEmail(data.session.user.email ?? null)
      setKinds(loadKinds())
      setFilesMap(loadFiles())
      setSessionNames(loadNameMap(SESSION_NAMES_KEY))
      setThreadNames(loadNameMap(THREAD_NAMES_KEY))
      setReady(true)
      loadSessions()
      loadGithubStatus()
      loadModels()
      loadStrategies()
      loadProfiles()
      loadProviders()
      loadSkillList()
    })
    const { data: sub } = supabase.auth.onAuthStateChange((_e, session) => {
      if (!session) router.replace('/login')
    })
    return () => sub.subscription.unsubscribe()
  }, [router])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // close "+New" dropdown on outside click
  useEffect(() => {
    if (!newMenuOpen) return
    function onClick(e: MouseEvent) {
      if (!newBtnWrapRef.current?.contains(e.target as Node)) {
        setNewMenuOpen(false)
      }
    }
    window.addEventListener('mousedown', onClick)
    return () => window.removeEventListener('mousedown', onClick)
  }, [newMenuOpen])

  // close row "⋯" menu on outside click / Escape
  useEffect(() => {
    if (!rowMenuKey) return
    function onClick(e: MouseEvent) {
      const t = e.target as HTMLElement
      if (!t.closest('[data-row-menu="1"]')) setRowMenuKey(null)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setRowMenuKey(null)
    }
    window.addEventListener('mousedown', onClick)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onClick)
      window.removeEventListener('keydown', onKey)
    }
  }, [rowMenuKey])

  // toast auto-dismiss
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 1800)
    return () => clearTimeout(t)
  }, [toast])

  // close composer attach menu on outside click / Escape
  useEffect(() => {
    if (!composerMenuOpen) return
    function onClick(e: MouseEvent) {
      if (!composerMenuRef.current?.contains(e.target as Node)) {
        setComposerMenuOpen(false)
        setSkillsFlyoutOpen(false)
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setComposerMenuOpen(false)
        setSkillsFlyoutOpen(false)
      }
    }
    window.addEventListener('mousedown', onClick)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onClick)
      window.removeEventListener('keydown', onKey)
    }
  }, [composerMenuOpen])

  // auto-grow composer
  useEffect(() => {
    const el = composerRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 220) + 'px'
  }, [input])

  /* ─── live event stream (SSE) ─── */
  // The SSE handler closure captures whatever values exist at subscribe
  // time, but `historyOpen` / `activeSession` can flip while subscribed.
  // We funnel commit events through a ref-stored callback that's rewritten
  // on every render — so the handler always invokes the latest closure.
  const onSseCommitsRef = useRef<() => void>(() => {})
  useEffect(() => {
    onSseCommitsRef.current = () => {
      if (historyOpen) openWorkspaceHistory()
      if (activeSession) refreshFiles(activeSession)
    }
  })

  // Debounced fetcher for the ring button. A streaming agent run can fire
  // many `message` SSE events in quick succession (assistant + N tool
  // results); collapse them into one refresh.
  const ctxRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const refreshContextSummaryRef = useRef<() => void>(() => {})
  useEffect(() => {
    refreshContextSummaryRef.current = refreshContextSummary
  })
  function scheduleContextRefresh() {
    if (ctxRefreshTimerRef.current) clearTimeout(ctxRefreshTimerRef.current)
    ctxRefreshTimerRef.current = setTimeout(() => {
      refreshContextSummaryRef.current()
    }, 700)
  }

  // Reset + initial fetch when the active thread changes.
  useEffect(() => {
    setContextSummary(null)
    if (activeSession && activeThread) {
      refreshContextSummary()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSession, activeThread, selectedModel])

  useEffect(() => {
    if (!activeSession || !activeThread) return
    let cancelled = false
    let es: EventSource | null = null

    ;(async () => {
      const tok = await authToken()
      if (!tok || cancelled) return
      const url = `/api/sessions/${activeSession}/threads/${activeThread}/stream?token=${encodeURIComponent(tok)}`
      es = new EventSource(url)
      es.onmessage = (ev) => {
        let evt: any
        try {
          evt = JSON.parse(ev.data)
        } catch {
          return
        }
        if (evt?.type === 'message') {
          // A persisted tool message just arrived — flush the matching
          // in-flight row, if any, before appending. Match by tool_name
          // FIFO (the agent invokes tools sequentially in our setup).
          if (evt.role === 'tool' && evt.tool_name) {
            setInflightTools((prev) => {
              const idx = prev.findIndex((t) => t.tool_name === evt.tool_name)
              if (idx < 0) return prev
              return [...prev.slice(0, idx), ...prev.slice(idx + 1)]
            })
          }
          // Final assistant message has landed → drop the live-token
          // preview now that the canonical version is in `messages`.
          if (evt.role === 'assistant' && evt.content) {
            setLiveTokens('')
            // Clear any forced-skill ("/") indicator rows for this turn.
            setInflightTools((prev) =>
              prev.filter((t) => t.tool_name !== 'skill_triggered'),
            )
          }
          setMessages((prev) => mergeIncomingMessage(prev, evt))
          // Keep the floating ring's percentage in sync. Debounce so a
          // burst of tool + assistant messages collapses into one refetch.
          scheduleContextRefresh()
        } else if (evt?.type === 'llm_token') {
          // Append to the live preview. Tokens arrive in order — no
          // need to keep them addressed by run_id for now.
          setLiveTokens((prev) => prev + (evt.text ?? ''))
        } else if (evt?.type === 'commits') {
          onSseCommitsRef.current()
        } else if (evt?.type === 'approval_request') {
          setPendingApproval(evt as PendingApproval)
          setLlmThinking(false)
        } else if (evt?.type === 'tool_started') {
          setLlmThinking(false)
          setInflightTools((prev) => [
            ...prev,
            {
              run_id: evt.run_id,
              tool_name: evt.tool_name,
              args: evt.args || {},
              started_at: Date.now(),
            },
          ])
        } else if (evt?.type === 'tool_finished') {
          // Defensive: clear by run_id in case the matching `message`
          // event was missed (e.g. tool errored without persisting).
          setInflightTools((prev) =>
            prev.filter((t) => t.run_id !== evt.run_id),
          )
        } else if (evt?.type === 'skill_triggered') {
          // A skill the user force-activated via "/" was applied this turn —
          // show it in the activity timeline like a tool/sandbox step.
          setLlmThinking(false)
          setInflightTools((prev) =>
            prev.some((t) => t.run_id === `skill:${evt.skill_name}`)
              ? prev
              : [
                  ...prev,
                  {
                    run_id: `skill:${evt.skill_name}`,
                    tool_name: 'skill_triggered',
                    args: { skill_name: evt.skill_name },
                    started_at: Date.now(),
                  },
                ],
          )
        } else if (evt?.type === 'llm_started') {
          setLlmThinking(true)
          // Fresh LLM call → start a new live-token preview.
          setLiveTokens('')
        } else if (evt?.type === 'llm_finished') {
          setLlmThinking(false)
        } else if (evt?.type === 'cancelled') {
          setInflightTools([])
          setLlmThinking(false)
          setLiveTokens('')
        }
      }
      es.onerror = () => {
        // EventSource auto-reconnects on its own; the sendMessage polling
        // is still in place as a backstop if the connection stays down.
      }
    })()

    return () => {
      cancelled = true
      if (es) es.close()
    }
  }, [activeSession, activeThread])

  /* ─── name helpers ─── */
  function setSessionName(id: string, name: string) {
    persistName(SESSION_NAMES_KEY, id, name)
    setSessionNames((prev) => ({ ...prev, [id]: name }))
  }
  function setThreadName(id: string, name: string) {
    persistName(THREAD_NAMES_KEY, id, name)
    setThreadNames((prev) => ({ ...prev, [id]: name }))
  }
  function sessionDisplayName(s: Session): string {
    const k = kinds[s.id] ?? 'project'
    const override = sessionNames[s.id]
    if (k === 'chat') return override || 'New chat'
    return override || s.name
  }
  function threadDisplayName(t: Thread): string {
    return threadNames[t.id] || 'New chat'
  }

  /* ─── row actions ─── */
  function startRename(key: string, currentName: string) {
    setRowMenuKey(null)
    setRenameKey(key)
    setRenameValue(currentName)
  }
  function commitRename() {
    if (!renameKey) return
    const v = renameValue.trim()
    const [kind, id] = renameKey.split(':')
    if (v) {
      if (kind === 'thread') setThreadName(id, v)
      else setSessionName(id, v)
    }
    setRenameKey(null)
    setRenameValue('')
  }
  function cancelRename() {
    setRenameKey(null)
    setRenameValue('')
  }

  function deleteSession(sid: string) {
    const isProject = (kinds[sid] ?? 'project') === 'project'
    setConfirmDialog({
      title: isProject ? 'Delete project?' : 'Delete chat?',
      message: isProject
        ? 'This will delete the project and all its threads. This cannot be undone.'
        : 'This will permanently delete the chat. This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
      onConfirm: () => {
        void doDeleteSession(sid)
      },
    })
  }

  async function doDeleteSession(sid: string) {
    setRowMenuKey(null)
    const r = await authFetch(`/api/sessions/${sid}`, { method: 'DELETE' })
    if (!r.ok) {
      setToast('Delete failed')
      return
    }
    // clean local state + localStorage
    const ts = threadsMap[sid] || []
    const newThreadNames = { ...threadNames }
    ts.forEach((t) => delete newThreadNames[t.id])
    setThreadNames(newThreadNames)
    localStorage.setItem(THREAD_NAMES_KEY, JSON.stringify(newThreadNames))

    const newKinds = { ...kinds }
    delete newKinds[sid]
    setKinds(newKinds)
    localStorage.setItem(KIND_KEY, JSON.stringify(newKinds))

    const newFiles = { ...filesMap }
    delete newFiles[sid]
    setFilesMap(newFiles)
    localStorage.setItem(FILES_KEY, JSON.stringify(newFiles))

    const newSessionNames = { ...sessionNames }
    delete newSessionNames[sid]
    setSessionNames(newSessionNames)
    localStorage.setItem(SESSION_NAMES_KEY, JSON.stringify(newSessionNames))

    setSessions((prev) => prev.filter((s) => s.id !== sid))
    setThreadsMap((prev) => {
      const next = { ...prev }
      delete next[sid]
      return next
    })
    if (activeSession === sid) {
      setActiveSession(null)
      setActiveThread(null)
      setMessages([])
    }
    setToast('Deleted')
  }

  function deleteThread(sid: string, tid: string) {
    setConfirmDialog({
      title: 'Delete chat?',
      message:
        'This will permanently delete this chat. Files in the project stay. This cannot be undone.',
      confirmLabel: 'Delete',
      danger: true,
      onConfirm: () => {
        void doDeleteThread(sid, tid)
      },
    })
  }

  async function doDeleteThread(sid: string, tid: string) {
    setRowMenuKey(null)
    const r = await authFetch(`/api/sessions/${sid}/threads/${tid}`, {
      method: 'DELETE',
    })
    if (!r.ok) {
      setToast('Delete failed')
      return
    }
    // Clean local state.
    const newThreadNames = { ...threadNames }
    delete newThreadNames[tid]
    setThreadNames(newThreadNames)
    localStorage.setItem(THREAD_NAMES_KEY, JSON.stringify(newThreadNames))

    const remaining = (threadsMap[sid] || []).filter((t) => t.id !== tid)
    setThreadsMap((prev) => ({ ...prev, [sid]: remaining }))

    // If we just deleted the active thread, fall back to the next one
    // in the same project, or clear the active selection entirely.
    if (activeThread === tid) {
      if (remaining[0]) {
        selectThread(sid, remaining[0].id)
      } else {
        setActiveThread(null)
        setMessages([])
      }
    }
    setToast('Chat deleted')
  }

  function moveChatToProject(sid: string) {
    setRowMenuKey(null)
    const current = sessions.find((s) => s.id === sid)
    if (!current) return
    const oldChatName = sessionDisplayName(current)
    setPromptDialog({
      title: 'Move to project',
      message: 'Give the new project a name.',
      placeholder: 'Project name',
      initialValue: oldChatName,
      confirmLabel: 'Create project',
      onConfirm: (raw) => {
        const projectName = raw.trim()
        if (!projectName) return
        const ts = threadsMap[sid] || []
        if (ts[0] && !threadNames[ts[0].id]) {
          setThreadName(ts[0].id, oldChatName)
        }
        saveKind(sid, 'project')
        setKinds((prev) => ({ ...prev, [sid]: 'project' }))
        setSessionName(sid, projectName)
        setExpanded((prev) => new Set(prev).add(sid))
        setToast('Moved to projects')
      },
    })
  }

  async function shareSession(sid: string, tid: string) {
    setRowMenuKey(null)
    try {
      // Pull fresh history so we share the current state, not stale local state.
      const r = await authFetch(`/api/sessions/${sid}/threads/${tid}/history`)
      const history: Message[] = r.ok ? await r.json() : messages
      const session = sessions.find((s) => s.id === sid)
      const titleLine = session ? `# ${sessionDisplayName(session)}\n\n` : ''
      const body = history
        .filter(
          (m) =>
            m.role !== 'tool' &&
            !(m.role === 'assistant' && !m.content?.trim()) &&
            !(
              m.role === 'assistant' &&
              m.tool_calls &&
              m.tool_calls.length > 0
            ),
        )
        .map(
          (m) =>
            `**${m.role === 'user' ? 'You' : 'Agent'}:**\n\n${m.content}\n`,
        )
        .join('\n')
      await navigator.clipboard.writeText(titleLine + body)
      setToast('Copied to clipboard')
    } catch {
      setToast('Copy failed')
    }
  }

  /** Stage files in the composer without uploading. They'll be uploaded
   *  together with the next send. */
  function addPendingFiles(list: FileList | null) {
    if (!list || !list.length) return
    // Snapshot the FileList synchronously — the input's value gets reset
    // right after this returns, which can clear `list` in some browsers.
    const incoming = Array.from(list)
    setPendingFiles((prev) => {
      const seen = new Set(prev.map((f) => f.name))
      const fresh = incoming.filter((f) => !seen.has(f.name))
      return [...prev, ...fresh]
    })
  }

  function removePendingFile(name: string) {
    setPendingFiles((prev) => prev.filter((f) => f.name !== name))
  }

  /** Pull the canonical file list for a session from the backend and merge
   *  it into filesMap. Used after /chat (the agent may have written files
   *  via the write_project_file tool) and when the viewer opens. */
  async function refreshFiles(sid: string) {
    try {
      const r = await authFetch(`/api/sessions/${sid}/files`)
      if (!r.ok) return
      const list: Array<{ name: string }> = await r.json()
      const names = list.map((f) => f.name)
      setFilesMap((prev) => ({ ...prev, [sid]: names }))
      saveFiles(sid, names)
    } catch {
      // ignore — local cache is still fine
    }
  }

  async function openFileViewer(sid: string, name: string) {
    setViewerFile({ sessionId: sid, name })
    setViewerContent('')
    setViewerError(null)
    setViewerLoading(true)
    try {
      const r = await authFetch(
        `/api/sessions/${sid}/files/${encodeURIComponent(name)}`,
      )
      if (!r.ok) {
        const status = r.status
        let detail = `${status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setViewerError(detail)
        return
      }
      const data = await r.json()
      setViewerContent(
        (data?.content ?? '') +
          (data?.truncated ? '\n\n[... truncated ...]' : ''),
      )
    } catch (e: any) {
      setViewerError(e?.message ?? 'Network error')
    } finally {
      setViewerLoading(false)
    }
  }

  /** Best-effort topic-summarized title via the model. Falls back to the
   *  heuristic if /title fails or is rate-limited. */
  async function generateTitle(text: string): Promise<string> {
    try {
      const r = await authFetch('/api/title', {
        method: 'POST',
        body: JSON.stringify({ text }),
      })
      if (r.ok) {
        const data = await r.json()
        const t = (data?.title as string | undefined)?.trim()
        if (t) return t
      }
    } catch {}
    return deriveTitle(text)
  }

  /* ─── api ─── */
  async function signOut() {
    await supabase.auth.signOut()
    router.replace('/login')
  }
  async function loadSkillList() {
    try {
      const r = await authFetch('/api/skills')
      if (!r.ok) return
      const list: any[] = await r.json()
      setSkillList(
        list.map((s) => ({ name: s.name, description: s.description || '' })),
      )
    } catch {
      /* slash menu just stays empty */
    }
  }
  async function loadSessions() {
    const r = await authFetch('/api/sessions')
    if (!r.ok) return
    const list: any[] = await r.json()
    setSessions(list)
    // Cache each session's preferred_provider_id so the chat-header picker
    // can highlight the right entry without an extra fetch.
    setSessionProviders((prev) => {
      const next: Record<string, string | null> = { ...prev }
      for (const s of list) {
        next[s.id] = s.preferred_provider_id ?? null
      }
      return next
    })
  }
  async function loadProviders() {
    try {
      const r = await authFetch('/api/providers')
      if (!r.ok) return
      const list = await r.json()
      setProviders(Array.isArray(list) ? list : [])
    } catch {
      // ignore — chat falls back to env path
    }
  }
  async function setSessionProvider(sid: string, providerId: string | null) {
    // Optimistic update so the picker reacts instantly.
    setSessionProviders((prev) => ({ ...prev, [sid]: providerId }))
    try {
      const body = JSON.stringify({
        // Backend treats "" as "clear override"; UUID as "set".
        preferred_provider_id: providerId ?? '',
      })
      const r = await authFetch(`/api/sessions/${sid}`, {
        method: 'PATCH',
        body,
      })
      if (!r.ok) {
        // Roll back on failure.
        const reloaded = await authFetch('/api/sessions')
        if (reloaded.ok) {
          const list: any[] = await reloaded.json()
          setSessions(list)
          setSessionProviders((prev) => {
            const next = { ...prev }
            for (const s of list) {
              next[s.id] = s.preferred_provider_id ?? null
            }
            return next
          })
        }
      }
    } catch {
      // Best effort — if the PATCH failed transiently, next loadSessions fixes it.
    }
  }
  async function loadModels() {
    try {
      const r = await authFetch('/api/models')
      if (!r.ok) return
      const data = await r.json()
      const list = Array.isArray(data?.models) ? data.models : []
      setModels(list)
      const saved =
        typeof window !== 'undefined'
          ? localStorage.getItem('selected_model') || ''
          : ''
      const initial =
        (saved && list.some((m: any) => m.id === saved) && saved) ||
        data?.default ||
        list[0]?.id ||
        ''
      setSelectedModel(initial)
    } catch {
      // Network/auth error — picker will just be empty; chat still works on backend default.
    }
  }
  async function loadStrategies() {
    try {
      const r = await authFetch('/api/context/strategies')
      if (!r.ok) return
      const data = await r.json()
      const list = Array.isArray(data?.strategies) ? data.strategies : []
      setStrategies(list)
      const fallback = data?.default || list[0]?.id || 'tool_calling'
      setDefaultStrategy(fallback)
      const saved =
        typeof window !== 'undefined'
          ? localStorage.getItem('selected_strategy') || ''
          : ''
      const initial =
        (saved && list.some((s: any) => s.id === saved) && saved) || fallback
      setSelectedStrategy(initial)
    } catch {
      // Non-critical — backend's DEFAULT_CONTEXT_STRATEGY wins if we can't reach this.
    }
  }
  async function loadProfiles() {
    try {
      const r = await authFetch('/api/context/profiles')
      if (!r.ok) return
      const data = await r.json()
      const list = Array.isArray(data?.profiles) ? data.profiles : []
      setProfiles(list)
      // Pick initial: user default → saved localStorage choice → built-in `minimal`.
      const saved =
        typeof window !== 'undefined'
          ? localStorage.getItem('selected_profile_id') || ''
          : ''
      const userDefault = list.find((p: any) => p.user_id && p.is_default)
      const minimal = list.find((p: any) => p.built_in && p.name === 'minimal')
      const initial =
        (saved && list.some((p: any) => p.id === saved) && saved) ||
        userDefault?.id ||
        minimal?.id ||
        list[0]?.id ||
        ''
      setSelectedProfileId(initial)
    } catch {
      // Non-critical — backend will fall back to the built-in `minimal`.
    }
  }
  async function loadGithubStatus() {
    try {
      const r = await authFetch('/api/github/status')
      if (!r.ok) return
      const data = await r.json()
      setGithubUsername(data.connected ? data.username : null)
    } catch {
      // ignore — non-critical
    }
  }
  async function saveGithubToken(token: string): Promise<string | null> {
    const r = await authFetch('/api/github/token', {
      method: 'POST',
      body: JSON.stringify({ token }),
    })
    if (!r.ok) {
      let detail = `${r.status} ${r.statusText}`
      try {
        const body = await r.json()
        if (body?.detail) detail = body.detail
      } catch {}
      return detail // error message
    }
    const data = await r.json()
    setGithubUsername(data.username)
    return null // success
  }
  async function disconnectGithub() {
    await authFetch('/api/github/token', { method: 'DELETE' })
    setGithubUsername(null)
  }

  async function openContextViewer() {
    if (!activeSession || !activeThread) return
    setContextOpen(true)
    setContextLoading(true)
    setContextError(null)
    setContextData(null)
    try {
      const qs = selectedModel
        ? `?model=${encodeURIComponent(selectedModel)}`
        : ''
      const r = await authFetch(
        `/api/sessions/${activeSession}/threads/${activeThread}/context${qs}`,
      )
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setContextError(detail)
        return
      }
      const data = await r.json()
      setContextData(data)
      setContextSummary({
        total: Number(data?.total_tokens) || 0,
        limit: Number(data?.context_limit) || 0,
        percent: Number(data?.percent_used) || 0,
      })
    } catch (e: any) {
      setContextError(e?.message ?? 'Network error')
    } finally {
      setContextLoading(false)
    }
  }

  // Soft refresh — re-fetch the context drawer data WITHOUT nulling it first,
  // so children (e.g. the Relevance cleanup list) don't unmount and lose their
  // local state. Used after a relevance Remove so the remaining suggestions
  // stay on screen instead of forcing a re-Analyze.
  async function softRefreshContext() {
    if (!activeSession || !activeThread) return
    try {
      const qs = selectedModel
        ? `?model=${encodeURIComponent(selectedModel)}`
        : ''
      const r = await authFetch(
        `/api/sessions/${activeSession}/threads/${activeThread}/context${qs}`,
      )
      if (!r.ok) return
      const data = await r.json()
      setContextData(data)
      setContextSummary({
        total: Number(data?.total_tokens) || 0,
        limit: Number(data?.context_limit) || 0,
        percent: Number(data?.percent_used) || 0,
      })
    } catch {
      // best-effort
    }
    if (activeSession && activeThread) loadHistory(activeSession, activeThread)
  }

  // Cheap passive refresh — fetches just the summary block of the context
  // endpoint so the ring button keeps its percentage in sync after the
  // agent writes a new message. Debounced so streaming token bursts don't
  // hammer the endpoint.
  async function refreshContextSummary() {
    if (!activeSession || !activeThread) return
    try {
      const qs = selectedModel
        ? `?model=${encodeURIComponent(selectedModel)}`
        : ''
      const r = await authFetch(
        `/api/sessions/${activeSession}/threads/${activeThread}/context${qs}`,
      )
      if (!r.ok) return
      const data = await r.json()
      setContextSummary({
        total: Number(data?.total_tokens) || 0,
        limit: Number(data?.context_limit) || 0,
        percent: Number(data?.percent_used) || 0,
      })
    } catch {
      // best-effort
    }
  }
  async function deleteContextMessage(messageId: number) {
    if (!activeSession || !activeThread) return
    const qs = selectedModel
      ? `?model=${encodeURIComponent(selectedModel)}`
      : ''
    const r = await authFetch(
      `/api/sessions/${activeSession}/threads/${activeThread}/messages/${messageId}${qs}`,
      { method: 'DELETE' },
    )
    if (!r.ok) {
      let detail = `${r.status} ${r.statusText}`
      try {
        const body = await r.json()
        if (body?.detail) detail = body.detail
      } catch {}
      setToast(`Delete failed: ${detail}`)
      return
    }
    let payload: any = null
    try {
      payload = await r.json()
    } catch {}
    if (payload && payload.removed_from_state === false) {
      setToast('Removed from view (older message — model may still recall it).')
    }
    // Re-fetch the context window so totals + the message list refresh.
    await openContextViewer()
    // Also refresh the main chat history so the deleted message disappears
    // from the conversation.
    if (activeSession && activeThread) {
      loadHistory(activeSession, activeThread)
    }
  }
  async function loadThreads(sid: string) {
    const r = await authFetch(`/api/sessions/${sid}/threads`)
    const data: Thread[] = await r.json()
    setThreadsMap((prev) => ({ ...prev, [sid]: data }))
    return data
  }
  async function loadHistory(sid: string, tid: string) {
    const r = await authFetch(`/api/sessions/${sid}/threads/${tid}/history`)
    setMessages(await r.json())
  }

  async function cancelChat() {
    if (!activeSession || !activeThread) return
    try {
      const r = await authFetch('/api/chat/cancel', {
        method: 'POST',
        body: JSON.stringify({
          session_id: activeSession,
          thread_id: activeThread,
        }),
      })
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setToast(`Cancel failed: ${detail.slice(0, 80)}`)
        return
      }
      const body = await r.json().catch(() => ({}))
      setToast(body?.killed ? 'Stopped running command' : 'Cancel sent')
    } catch (e: any) {
      setToast(`Cancel failed: ${e?.message ?? 'network'}`)
    }
  }

  async function resolveApproval(approved: boolean, reason?: string) {
    if (!activeSession || !activeThread || resolvingApproval) return
    setResolvingApproval(true)
    setSending(true)
    try {
      const r = await authFetch('/api/chat/resume', {
        method: 'POST',
        body: JSON.stringify({
          session_id: activeSession,
          thread_id: activeThread,
          approved,
          reason,
          model: selectedModel || undefined,
          context_strategy: selectedStrategy || undefined,
          context_profile_id: selectedProfileId || undefined,
        }),
      })
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setToast(`Resume failed: ${detail.slice(0, 120)}`)
        setPendingApproval(null)
        setSending(false)
        return
      }
      const body = await r.json()
      if (body?.interrupted && body?.approval) {
        // Chained interrupt — the agent paused again on a *different* tool
        // call after we resumed. Show the new card.
        setPendingApproval(body.approval)
        setSending(false)
      } else {
        // Run finished cleanly. SSE will have flushed any new tool /
        // assistant messages already.
        setPendingApproval(null)
        setSending(false)
      }
    } catch (e: any) {
      setToast(`Resume failed: ${e?.message ?? 'network'}`)
      setPendingApproval(null)
      setSending(false)
    } finally {
      setResolvingApproval(false)
    }
  }

  async function updateSessionMode(sid: string, mode: 'auto' | 'confirm') {
    // Optimistic update — flip locally first, revert on failure.
    const prev = sessions.find((s) => s.id === sid)?.mode
    setSessions((all) =>
      all.map((s) => (s.id === sid ? { ...s, mode } : s)),
    )
    try {
      const r = await authFetch(`/api/sessions/${sid}`, {
        method: 'PATCH',
        body: JSON.stringify({ mode }),
      })
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setToast(`Mode change failed: ${detail.slice(0, 80)}`)
        setSessions((all) =>
          all.map((s) => (s.id === sid ? { ...s, mode: prev } : s)),
        )
      }
    } catch (e: any) {
      setToast(`Mode change failed: ${e?.message ?? 'network'}`)
      setSessions((all) =>
        all.map((s) => (s.id === sid ? { ...s, mode: prev } : s)),
      )
    }
  }

  async function openWorkspaceHistory() {
    if (!activeSession) return
    setHistoryOpen(true)
    setHistoryLoading(true)
    setHistoryError(null)
    try {
      const r = await authFetch(`/api/sessions/${activeSession}/history`)
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setHistoryError(detail)
        return
      }
      setHistoryData(await r.json())
    } catch (e: any) {
      setHistoryError(e?.message ?? 'network error')
    } finally {
      setHistoryLoading(false)
    }
  }

  async function revertCommit(commitId: number) {
    if (!activeSession || revertingId !== null) return
    setRevertingId(commitId)
    try {
      const r = await authFetch(
        `/api/sessions/${activeSession}/history/${commitId}/revert`,
        { method: 'POST' },
      )
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setToast(`Revert failed: ${detail.slice(0, 120)}`)
        return
      }
      // Refresh history + files so the UI catches up.
      await openWorkspaceHistory()
      if (activeSession) await refreshFiles(activeSession)
      setToast('Reverted')
    } catch (e: any) {
      setToast(`Revert failed: ${e?.message ?? 'network error'}`)
    } finally {
      setRevertingId(null)
    }
  }

  type GithubOptions =
    | { mode: 'none' }
    | { mode: 'new_repo'; repoName: string; private: boolean }
    | { mode: 'link_existing'; owner: string; repo: string; branch?: string }

  async function createProject(
    name: string,
    files: File[],
    github: GithubOptions = { mode: 'none' },
  ) {
    const payload: any = { name, kind: 'project' }
    if (github.mode === 'new_repo') {
      payload.github_mode = 'new_repo'
      payload.github_repo_name = github.repoName
      payload.github_private = github.private
    } else if (github.mode === 'link_existing') {
      payload.github_mode = 'link_existing'
      payload.github_owner = github.owner
      payload.github_repo = github.repo
      if (github.branch) payload.github_branch = github.branch
    }
    const r = await authFetch('/api/sessions', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    if (!r.ok) {
      let detail = `${r.status} ${r.statusText}`
      try {
        const body = await r.json()
        if (body?.detail) detail = body.detail
      } catch {}
      setToast(`Project not created: ${detail.slice(0, 120)}`)
      return
    }
    const data = await r.json()
    const s: Session = {
      id: data.id,
      name: data.name,
      created_at: data.created_at,
    }
    const defaultThread: Thread | undefined = data.default_thread

    saveKind(s.id, 'project')
    setKinds((prev) => ({ ...prev, [s.id]: 'project' }))

    // Actually upload the picked files to the backend bucket.
    if (files.length) {
      const fd = new FormData()
      for (const f of files) fd.append('files', f, f.name)
      try {
        const upR = await authFetch(`/api/sessions/${s.id}/files`, {
          method: 'POST',
          body: fd,
        })
        if (!upR.ok) {
          let detail = `${upR.status} ${upR.statusText}`
          try {
            const body = await upR.json()
            if (body?.detail) detail = body.detail
          } catch {}
          setToast(`Some files failed: ${detail.slice(0, 80)}`)
        }
      } catch (e: any) {
        setToast(`Upload failed: ${e?.message ?? 'network error'}`)
      }
      // Refresh from the backend so the sidebar reflects reality.
      await refreshFiles(s.id)
    }

    setSessions((prev) => [s, ...prev])
    setExpanded((prev) => new Set(prev).add(s.id))
    if (defaultThread) {
      setThreadsMap((prev) => ({ ...prev, [s.id]: [defaultThread] }))
      selectThread(s.id, defaultThread.id)
    } else {
      setThreadsMap((prev) => ({ ...prev, [s.id]: [] }))
    }
  }

  async function createChat() {
    const r = await authFetch('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ name: 'New chat' }),
    })
    if (!r.ok) return
    const data = await r.json()
    const s: Session = {
      id: data.id,
      name: data.name,
      created_at: data.created_at,
    }
    const defaultThread: Thread = data.default_thread

    saveKind(s.id, 'chat')
    setKinds((prev) => ({ ...prev, [s.id]: 'chat' }))

    setSessions((prev) => [s, ...prev])
    setThreadsMap((prev) => ({ ...prev, [s.id]: [defaultThread] }))
    selectThread(s.id, defaultThread.id)
  }

  async function createThread(sid: string) {
    // No prompt — auto-name from the first message instead.
    const r = await authFetch(`/api/sessions/${sid}/threads`, {
      method: 'POST',
      body: JSON.stringify({ name: 'New chat' }),
    })
    if (!r.ok) return
    const t: Thread = await r.json()
    setThreadsMap((prev) => ({ ...prev, [sid]: [...(prev[sid] || []), t] }))
    selectThread(sid, t.id)
  }

  async function toggleSession(sid: string) {
    const next = new Set(expanded)
    if (next.has(sid)) {
      next.delete(sid)
    } else {
      next.add(sid)
      if (!threadsMap[sid]) await loadThreads(sid)
      // Sync files alongside threads so the sidebar list is fresh.
      refreshFiles(sid)
    }
    setExpanded(next)
  }

  async function selectThread(sid: string, tid: string) {
    setMcpsOpen(false)
    setDemoOpen(false)
    setContextProfilesOpen(false)
    setSkillsOpen(false)
    setPluginsOpen(false)
    setActiveSession(sid)
    setActiveThread(tid)
    setMessages([])
    setInflightTools([])
    setLlmThinking(false)
    setLiveTokens('')
    setPendingApproval(null)
    setReplayIdx(null)
    await loadHistory(sid, tid)
    // Sync file chips with backend reality (agent may have written files).
    refreshFiles(sid)
  }

  async function selectChat(sid: string) {
    let threads = threadsMap[sid]
    if (!threads) threads = await loadThreads(sid)
    if (threads[0]) selectThread(sid, threads[0].id)
  }

  // Open the project landing pane: active session, no active thread.
  // Clicking on a project row header should put the user here so they can
  // see the project's files, threads, and metadata before diving in.
  async function selectProject(sid: string) {
    setMcpsOpen(false)
    setDemoOpen(false)
    setContextProfilesOpen(false)
    setSkillsOpen(false)
    setPluginsOpen(false)
    setActiveSession(sid)
    setActiveThread(null)
    setMessages([])
    setInflightTools([])
    setLlmThinking(false)
    setLiveTokens('')
    setPendingApproval(null)
    setReplayIdx(null)
    if (!threadsMap[sid]) await loadThreads(sid)
    refreshFiles(sid)
  }

  // "/" slash menu: skills matching the text typed after a leading "/".
  function slashCandidates(): { name: string; description: string }[] {
    if (!input.startsWith('/')) return []
    const q = input.slice(1).toLowerCase().trim()
    return skillList
      .filter((s) => !triggeredSkills.includes(s.name))
      .filter(
        (s) =>
          !q ||
          s.name.toLowerCase().includes(q) ||
          s.description.toLowerCase().includes(q),
      )
      .slice(0, 8)
  }

  function activateSkill(name: string) {
    setTriggeredSkills((prev) =>
      prev.includes(name) ? prev : [...prev, name],
    )
    setInput('')
    setSlashMenuOpen(false)
    setSlashIndex(0)
    composerRef.current?.focus()
  }

  async function send() {
    if (!input.trim() || !activeSession || !activeThread || sending) return
    const sid = activeSession
    const tid = activeThread
    const msg = input.trim()
    const filesToUpload = pendingFiles
    const skillsToTrigger = triggeredSkills
    const isFirstMessage = messages.length === 0
    setInput('')
    setPendingFiles([])
    setTriggeredSkills([])
    setSlashMenuOpen(false)
    setSending(true)
    setMessages((prev) => [...prev, { role: 'user', content: msg }])

    // Upload any pending attachments BEFORE the chat request so the agent
    // can find them via list_project_files / read_project_file.
    let attachedFiles: string[] = []
    if (filesToUpload.length) {
      setUploading(true)
      const fd = new FormData()
      for (const f of filesToUpload) fd.append('files', f, f.name)
      try {
        const r = await authFetch(`/api/sessions/${sid}/files`, {
          method: 'POST',
          body: fd,
        })
        if (r.ok) {
          const data = await r.json()
          const names: string[] = (data?.saved || []).map((s: any) => s.name)
          attachedFiles = names
          // Reflect in the chip strip immediately.
          const existing = filesMap[sid] || []
          const merged = Array.from(new Set([...existing, ...names]))
          setFilesMap((prev) => ({ ...prev, [sid]: merged }))
          saveFiles(sid, merged)
        } else {
          let detail = `${r.status} ${r.statusText}`
          try {
            const body = await r.json()
            if (body?.detail) detail = body.detail
          } catch {}
          setToast(`Upload failed: ${detail.slice(0, 80)}`)
        }
      } catch (e: any) {
        setToast(`Upload failed: ${e?.message ?? 'network error'}`)
      } finally {
        setUploading(false)
      }
    }

    // Auto-title on the first exchange. Show the heuristic title immediately
    // for snappy sidebar feedback, then upgrade to a model-summarized title
    // in the background. Both write through the same setters, so the sidebar
    // updates twice when the model returns.
    if (isFirstMessage) {
      const heuristic = deriveTitle(msg)
      const kind = kinds[sid] ?? 'project'
      const target = kind === 'chat' ? 'session' : 'thread'
      const alreadyNamed =
        target === 'session' ? !!sessionNames[sid] : !!threadNames[tid]
      if (heuristic && !alreadyNamed) {
        if (target === 'session') setSessionName(sid, heuristic)
        else setThreadName(tid, heuristic)
        // Background upgrade — best effort, don't await.
        generateTitle(msg).then((better) => {
          if (!better || better === heuristic) return
          if (target === 'session') setSessionName(sid, better)
          else setThreadName(tid, better)
        })
      }
    }

    // Baseline so we know when a *new* assistant message lands.
    const baselineAssistantCount = messages.filter(
      (m) => m.role === 'assistant' && m.content?.trim(),
    ).length

    // Fire /chat in the background. The backend may complete the model call
    // and DB write even if the connection drops mid-flight (Next.js dev proxy
    // can cut long requests). We trust the DB and poll /history; only surface
    // errors we know won't recover (auth, rate-limit, validation).
    let chatErrorDetail: string | null = null
    let chatStatus: number | null = null
    let chatInterrupted = false
    authFetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sid,
        thread_id: tid,
        message: msg,
        attached_files: attachedFiles,
        triggered_skills: skillsToTrigger,
        model: selectedModel || undefined,
        context_strategy: selectedStrategy || undefined,
        context_profile_id: selectedProfileId || undefined,
      }),
    })
      .then(async (r) => {
        chatStatus = r.status
        if (!r.ok) {
          let detail = `${r.status} ${r.statusText}`
          try {
            const body = await r.json()
            if (body?.detail) detail = body.detail
          } catch {}
          chatErrorDetail = detail
          return
        }
        // Inspect the body — Confirm mode pauses the agent and returns
        // `interrupted: true`. The matching SSE event fires the approval
        // card; we just need to stop the polling loop.
        try {
          const body = await r.json()
          if (body?.interrupted) {
            chatInterrupted = true
            if (body?.approval) setPendingApproval(body.approval)
          }
        } catch {}
      })
      .catch((e: any) => {
        chatErrorDetail = e?.message ?? String(e)
      })

    const FAST_FAIL = new Set([400, 401, 403, 422, 429])
    const deadline = Date.now() + 180_000 // 3 min

    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500))

      // Confirm-mode interrupt — agent is paused waiting for the user's
      // approval card to be answered. Stop polling and let the approval
      // flow take over.
      if (chatInterrupted) {
        setSending(false)
        return
      }

      try {
        const r = await authFetch(
          `/api/sessions/${sid}/threads/${tid}/history`,
        )
        if (r.ok) {
          const history: Message[] = await r.json()
          const newCount = history.filter(
            (m) => m.role === 'assistant' && m.content?.trim(),
          ).length
          if (newCount > baselineAssistantCount) {
            setMessages(history)
            setSending(false)
            // Refresh sidebar token counts.
            loadSessions()
            if (kinds[sid] === 'project') loadThreads(sid)
            // Refresh files — the agent may have created or modified some.
            refreshFiles(sid)
            return
          }
        }
      } catch {
        // ignore transient polling failures
      }

      // Bail early only on errors that definitely won't recover.
      if (chatErrorDetail && chatStatus && FAST_FAIL.has(chatStatus)) {
        break
      }
    }

    // Deadline passed (or we bailed on a fast-fail).
    if (chatErrorDetail) {
      const hint =
        chatStatus === 429
          ? 'The model is rate-limited. Wait a minute and retry, or add OpenRouter credit.'
          : chatStatus === 401
            ? 'Session expired. Try signing in again.'
            : chatErrorDetail
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: `**Error:** ${hint}` },
      ])
    } else {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content:
            '*The model is still working. Refresh in a moment — your reply may have been saved.*',
        },
      ])
    }
    setSending(false)
  }

  /* ─── derived ─── */
  const projects = useMemo(
    () => sessions.filter((s) => (kinds[s.id] ?? 'project') === 'project'),
    [sessions, kinds],
  )
  const chats = useMemo(
    () => sessions.filter((s) => kinds[s.id] === 'chat'),
    [sessions, kinds],
  )

  const activeSessionObj = sessions.find((s) => s.id === activeSession)
  const activeThreadObj = activeSession
    ? threadsMap[activeSession]?.find((t) => t.id === activeThread)
    : undefined
  const activeKind: Kind = activeSession
    ? (kinds[activeSession] ?? 'project')
    : 'project'
  const activeFiles = activeSession ? filesMap[activeSession] || [] : []
  // In replay mode, slice messages to the scrubber position so the user
  // can step through the conversation as if it were happening live.
  const displayMessages =
    replayIdx == null ? messages : messages.slice(0, replayIdx)

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center text-fog-400 text-sm">
        Loading…
      </div>
    )
  }

  /* ─── render ─── */
  return (
    <div className="flex h-screen bg-ink-50 text-fog-100 overflow-hidden">
      {/* ────── Sidebar ────── */}
      <aside className="w-72 shrink-0 border-r border-line bg-ink-100/60 flex flex-col">
        <div className="px-4 py-3 flex items-center justify-between gap-2 border-b border-line">
          <Link href="/" className="flex items-center gap-2 group">
            <Glyph />
            <span className="text-sm tracking-tight font-medium">agent</span>
          </Link>
          <ThemeToggle />
        </div>

        {/* + New dropdown */}
        <div className="px-3 pt-3 relative" ref={newBtnWrapRef}>
          <button
            onClick={() => setNewMenuOpen((v) => !v)}
            className="w-full flex items-center justify-between gap-2 bg-accent text-ink-50 hover:bg-accent/90 transition px-3 py-2 rounded-lg text-sm font-medium"
          >
            <span className="flex items-center gap-2">
              <PlusIcon />
              New
            </span>
            <Caret open={newMenuOpen} className="text-fog-400" />
          </button>

          {newMenuOpen && (
            <div className="absolute left-3 right-3 mt-1.5 z-50 rounded-2xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60">
              <button
                onClick={() => {
                  setNewMenuOpen(false)
                  setProjectModalOpen(true)
                }}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
              >
                <IconFolder />
                <div>
                  <div className="text-fog-100">New project</div>
                  <div className="text-[11px] text-fog-400">
                    Group threads · share context
                  </div>
                </div>
              </button>
              <button
                onClick={() => {
                  setNewMenuOpen(false)
                  createChat()
                }}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
              >
                <IconChat />
                <div>
                  <div className="text-fog-100">New chat</div>
                  <div className="text-[11px] text-fog-400">
                    Standalone · isolated context
                  </div>
                </div>
              </button>
            </div>
          )}
        </div>

        {/* lists */}
        <div className="flex-1 overflow-y-auto pt-3 pb-4">
          {/* Projects */}
          <div className="px-4 mb-1.5 flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-widest text-fog-400 inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-sm bg-amber-500/80" />
              Projects
            </span>
            <span className="text-[10px] text-fog-500 px-1.5 py-0.5 rounded-full bg-soft/[0.06] tabular-nums">
              {projects.length}
            </span>
          </div>
          <div className="px-1.5">
            {projects.length === 0 && (
              <p className="px-3 py-2 text-xs text-fog-500">
                No projects yet.
              </p>
            )}
            {projects.map((s) => {
              const isOpen = expanded.has(s.id)
              const threads = threadsMap[s.id] || []
              const fileCount = filesMap[s.id]?.length || 0
              const rk = `project:${s.id}`
              const renaming = renameKey === rk
              const menuOpen = rowMenuKey === rk
              return (
                <div
                  key={s.id}
                  data-row-menu={menuOpen ? '1' : undefined}
                  className="mb-0.5"
                >
                  {renaming ? (
                    <RenameInput
                      value={renameValue}
                      onChange={setRenameValue}
                      onCommit={commitRename}
                      onCancel={cancelRename}
                      icon={
                        <IconFolder className="shrink-0 text-fog-300" small />
                      }
                    />
                  ) : (
                    <div
                      className={`group relative flex items-center rounded-md hover:bg-soft/[0.04] ${
                        activeSession === s.id
                          ? 'bg-soft/[0.05] ring-1 ring-amber-500/20'
                          : ''
                      }`}
                    >
                      {/* Left accent strip — visible whenever this project
                         (or any thread inside it) is the active session. */}
                      {activeSession === s.id && (
                        <span className="absolute left-0 top-1 bottom-1 w-[2px] rounded-r bg-amber-500/70" />
                      )}
                      <button
                        onClick={() => {
                          toggleSession(s.id)
                          selectProject(s.id)
                        }}
                        className="flex-1 min-w-0 px-2.5 py-1.5 text-left text-sm flex items-center gap-2 text-fog-100 font-medium"
                      >
                        <Caret open={isOpen} className="text-fog-400" />
                        <IconFolder className="shrink-0 text-amber-500/80" small />
                        <span className="truncate flex-1">
                          {sessionDisplayName(s)}
                        </span>
                        {fileCount > 0 && (
                          <span className="text-[10px] text-fog-500 tabular-nums">
                            {fileCount}
                          </span>
                        )}
                      </button>
                      <button
                        data-row-menu="1"
                        onClick={(e) => {
                          e.stopPropagation()
                          setRowMenuKey(menuOpen ? null : rk)
                        }}
                        className={`shrink-0 mr-1 w-6 h-6 rounded hover:bg-soft/[0.08] text-fog-300 flex items-center justify-center ${
                          menuOpen
                            ? 'opacity-100'
                            : 'opacity-0 group-hover:opacity-100 focus:opacity-100'
                        }`}
                        aria-label="More"
                      >
                        <DotsIcon />
                      </button>
                      {menuOpen && (
                        <RowMenu
                          items={[
                            {
                              label: 'Rename',
                              icon: <IconPencil />,
                              onClick: () =>
                                startRename(rk, sessionDisplayName(s)),
                            },
                            {
                              label: 'Delete',
                              icon: <IconTrash />,
                              danger: true,
                              onClick: () => deleteSession(s.id),
                            },
                          ]}
                        />
                      )}
                    </div>
                  )}
                  {isOpen && (
                    <div className="pl-3 pt-0.5">
                      {/* FILES section — uploaded + agent-written */}
                      {(filesMap[s.id]?.length ?? 0) > 0 && (
                        <>
                          <div className="px-2.5 pt-1 pb-0.5 text-[10px] uppercase tracking-widest text-fog-500">
                            Files
                          </div>
                          {(filesMap[s.id] || []).map((f) => {
                            const display = f.split('/').pop() || f
                            return (
                              <button
                                key={`f:${f}`}
                                onClick={() => openFileViewer(s.id, display)}
                                className="w-full text-left px-2.5 py-1.5 rounded-md flex items-center gap-2.5 text-[13px] text-fog-200 hover:bg-soft/[0.03] hover:text-fog-50"
                                title={`Open ${display}`}
                              >
                                <IconFile />
                                <span className="truncate flex-1">{display}</span>
                              </button>
                            )
                          })}
                        </>
                      )}

                      {/* CHATS section */}
                      <div className="px-2.5 pt-2 pb-0.5 text-[10px] uppercase tracking-widest text-fog-500">
                        Chats
                      </div>
                      {threads.map((t) => {
                        const active = activeThread === t.id
                        const tk = `thread:${t.id}`
                        const tRenaming = renameKey === tk
                        const tMenuOpen = rowMenuKey === tk
                        return (
                          <div
                            key={t.id}
                            data-row-menu={tMenuOpen ? '1' : undefined}
                            className={`group relative rounded-md flex items-center text-[13px] ${
                              active
                                ? 'bg-soft/[0.07] text-fog-50'
                                : 'text-fog-200 hover:bg-soft/[0.03]'
                            }`}
                          >
                            {tRenaming ? (
                              <RenameInput
                                value={renameValue}
                                onChange={setRenameValue}
                                onCommit={commitRename}
                                onCancel={cancelRename}
                                icon={
                                  <span
                                    className={`dot shrink-0 ${
                                      active ? 'bg-emerald-400' : 'bg-fog-500'
                                    }`}
                                  />
                                }
                              />
                            ) : (
                              <>
                                <button
                                  onClick={() => selectThread(s.id, t.id)}
                                  className="flex-1 min-w-0 text-left px-2.5 py-1.5 flex items-center gap-2.5 hover:text-fog-50"
                                >
                                  <span
                                    className={`dot shrink-0 ${
                                      active ? 'bg-emerald-400' : 'bg-fog-500'
                                    }`}
                                  />
                                  <span className="truncate flex-1">
                                    {threadDisplayName(t)}
                                  </span>
                                  {!!t.tokens && (
                                    <span
                                      className="text-[10px] text-fog-500 tabular-nums shrink-0"
                                      title={`${t.tokens.toLocaleString()} tokens`}
                                    >
                                      {fmtTokens(t.tokens)}
                                    </span>
                                  )}
                                </button>
                                <button
                                  data-row-menu="1"
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    setRowMenuKey(tMenuOpen ? null : tk)
                                  }}
                                  className={`shrink-0 mr-1 w-6 h-6 rounded hover:bg-soft/[0.08] text-fog-300 flex items-center justify-center ${
                                    tMenuOpen
                                      ? 'opacity-100'
                                      : 'opacity-0 group-hover:opacity-100 focus:opacity-100'
                                  }`}
                                  aria-label="More"
                                >
                                  <DotsIcon />
                                </button>
                                {tMenuOpen && (
                                  <RowMenu
                                    items={[
                                      {
                                        label: 'Rename',
                                        icon: <IconPencil />,
                                        onClick: () =>
                                          startRename(tk, threadDisplayName(t)),
                                      },
                                      {
                                        label: 'Delete',
                                        icon: <IconTrash />,
                                        danger: true,
                                        onClick: () => deleteThread(s.id, t.id),
                                      },
                                    ]}
                                  />
                                )}
                              </>
                            )}
                          </div>
                        )
                      })}
                      <button
                        onClick={() => createThread(s.id)}
                        className="w-full text-left px-2.5 py-1.5 rounded-md text-[12px] text-fog-400 hover:text-fog-50 hover:bg-soft/[0.03] flex items-center gap-2.5"
                      >
                        <PlusIcon small />
                        New chat
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {/* Chats */}
          <div className="px-4 mt-5 mb-1.5 flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-widest text-fog-400 inline-flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-fog-400" />
              Chats
            </span>
            <span className="text-[10px] text-fog-500 px-1.5 py-0.5 rounded-full bg-soft/[0.06] tabular-nums">
              {chats.length}
            </span>
          </div>
          <div className="px-1.5">
            {chats.length === 0 && (
              <p className="px-3 py-2 text-xs text-fog-500">
                No chats yet.
              </p>
            )}
            {chats.map((s) => {
              const active = activeSession === s.id
              const rk = `chat:${s.id}`
              const renaming = renameKey === rk
              const menuOpen = rowMenuKey === rk
              return (
                <div
                  key={s.id}
                  data-row-menu={menuOpen ? '1' : undefined}
                  className={`group relative rounded-md flex items-center text-[13px] ${
                    active
                      ? 'bg-soft/[0.07] text-fog-50'
                      : 'text-fog-200 hover:bg-soft/[0.03]'
                  }`}
                >
                  {renaming ? (
                    <RenameInput
                      value={renameValue}
                      onChange={setRenameValue}
                      onCommit={commitRename}
                      onCancel={cancelRename}
                      icon={
                        <IconChat
                          className="shrink-0 text-fog-400"
                          small
                        />
                      }
                    />
                  ) : (
                    <>
                      <button
                        onClick={() => selectChat(s.id)}
                        className="flex-1 min-w-0 text-left px-2.5 py-1.5 flex items-center gap-2.5 hover:text-fog-50"
                      >
                        <IconChat
                          className="shrink-0 text-fog-400"
                          small
                        />
                        <span className="truncate flex-1">
                          {sessionDisplayName(s)}
                        </span>
                        {!!s.tokens && (
                          <span
                            className="text-[10px] text-fog-500 tabular-nums shrink-0"
                            title={`${s.tokens.toLocaleString()} tokens`}
                          >
                            {fmtTokens(s.tokens)}
                          </span>
                        )}
                      </button>
                      <button
                        data-row-menu="1"
                        onClick={(e) => {
                          e.stopPropagation()
                          setRowMenuKey(menuOpen ? null : rk)
                        }}
                        className={`shrink-0 mr-1 w-6 h-6 rounded hover:bg-soft/[0.08] text-fog-300 flex items-center justify-center ${
                          menuOpen
                            ? 'opacity-100'
                            : 'opacity-0 group-hover:opacity-100 focus:opacity-100'
                        }`}
                        aria-label="More"
                      >
                        <DotsIcon />
                      </button>
                      {menuOpen && (
                        <RowMenu
                          items={[
                            {
                              label: 'Rename',
                              icon: <IconPencil />,
                              onClick: () =>
                                startRename(rk, sessionDisplayName(s)),
                            },
                            {
                              label: 'Move to project',
                              icon: <IconFolder small />,
                              onClick: () => moveChatToProject(s.id),
                            },
                            {
                              label: 'Share',
                              icon: <IconShare />,
                              onClick: () => {
                                const ts = threadsMap[s.id] || []
                                if (ts[0]) shareSession(s.id, ts[0].id)
                              },
                            },
                            {
                              label: 'Delete',
                              icon: <IconTrash />,
                              danger: true,
                              onClick: () => deleteSession(s.id),
                            },
                          ]}
                        />
                      )}
                    </>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {/* PR #8: context profiles entry — bundles tool surface +
            context-management toggles. Sits above Strategy Demo so
            users land on it before running comparisons. */}
        <div className="px-3 pt-2">
          <button
            onClick={() =>
              setContextProfilesOpen((v) => {
                const next = !v
                if (next) {
                  setDemoOpen(false)
                  setMcpsOpen(false)
                  setSkillsOpen(false)
                  setPluginsOpen(false)
                }
                return next
              })
            }
            aria-pressed={contextProfilesOpen}
            className={`w-full flex items-center gap-3 px-2 py-2 rounded-md transition ${
              contextProfilesOpen
                ? 'bg-soft/[0.08] text-fog-50'
                : 'hover:bg-soft/[0.04] text-fog-200'
            }`}
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M4 6h12M4 10h16M4 14h10M4 18h14" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">Context Profiles</span>
          </button>
        </div>

        {/* Strategy demo entry — runs both context-management strategies
            in parallel for the same prompt and shows token / latency /
            tool-call comparison. Sits above MCP Inventory. */}
        <div className="px-3 pt-1">
          <button
            onClick={() =>
              setDemoOpen((v) => {
                const next = !v
                if (next) {
                  setMcpsOpen(false)
                  setContextProfilesOpen(false)
                  setSkillsOpen(false)
                  setPluginsOpen(false)
                }
                return next
              })
            }
            aria-pressed={demoOpen}
            className={`w-full flex items-center gap-3 px-2 py-2 rounded-md transition ${
              demoOpen
                ? 'bg-soft/[0.08] text-fog-50'
                : 'hover:bg-soft/[0.04] text-fog-200'
            }`}
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 3v18M15 3v18M3 9h18M3 15h18" opacity="0.55" />
                <path d="M3 3h18v18H3z" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">Strategy Demo</span>
          </button>
        </div>

        {/* MCP inventory entry — sits above the user profile so it's
            findable without diving into the user menu. Toggles the MCP
            panel in the main pane (in place of chat). */}
        <div className="px-3 pt-1">
          <button
            onClick={() =>
              setMcpsOpen((v) => {
                const next = !v
                if (next) {
                  setDemoOpen(false)
                  setContextProfilesOpen(false)
                  setSkillsOpen(false)
                  setPluginsOpen(false)
                }
                return next
              })
            }
            aria-pressed={mcpsOpen}
            className={`w-full flex items-center gap-3 px-2 py-2 rounded-md transition ${
              mcpsOpen
                ? 'bg-soft/[0.08] text-fog-50'
                : 'hover:bg-soft/[0.04] text-fog-200'
            }`}
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
                <path d="M3.27 6.96L12 12.01l8.73-5.05M12 22.08V12" opacity="0.6" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">MCP Inventory</span>
          </button>
        </div>

        {/* Skills entry — toggleable instruction bundles. Swaps the manage
            panel into the main pane, same exclusivity model as MCPs/Demo. */}
        <div className="px-3 pt-1">
          <button
            onClick={() =>
              setSkillsOpen((v) => {
                const next = !v
                if (next) {
                  setMcpsOpen(false)
                  setDemoOpen(false)
                  setContextProfilesOpen(false)
                  setPluginsOpen(false)
                }
                return next
              })
            }
            aria-pressed={skillsOpen}
            className={`w-full flex items-center gap-3 px-2 py-2 rounded-md transition ${
              skillsOpen
                ? 'bg-soft/[0.08] text-fog-50'
                : 'hover:bg-soft/[0.04] text-fog-200'
            }`}
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3l2.4 5.6L20 10l-4.5 3.9L17 20l-5-3-5 3 1.5-6.1L4 10l5.6-1.4z" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">Skills</span>
          </button>
        </div>

        {/* Plugins entry — code-defined tools the agent can call. Swaps the
            manage page into the main pane, same exclusivity model as Skills. */}
        <div className="px-3 pt-1">
          <button
            onClick={() =>
              setPluginsOpen((v) => {
                const next = !v
                if (next) {
                  setMcpsOpen(false)
                  setDemoOpen(false)
                  setContextProfilesOpen(false)
                  setSkillsOpen(false)
                }
                return next
              })
            }
            aria-pressed={pluginsOpen}
            className={`w-full flex items-center gap-3 px-2 py-2 rounded-md transition ${
              pluginsOpen
                ? 'bg-soft/[0.08] text-fog-50'
                : 'hover:bg-soft/[0.04] text-fog-200'
            }`}
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2a3 3 0 0 1 3 3v2h2a2 2 0 0 1 2 2v3a3 3 0 0 1 0 6 2 2 0 0 1-2 2h-3v-2a3 3 0 0 0-6 0v2H5a2 2 0 0 1-2-2v-3a3 3 0 0 0 0-6V9a2 2 0 0 1 2-2h2V5a3 3 0 0 1 3-3z" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">Plugins</span>
          </button>
        </div>

        {/* Settings entry — hub linking to Providers, MCP, and Context
            Profiles. Navigates to the standalone /app/settings route. */}
        <div className="px-3 pt-1">
          <Link
            href="/app/settings"
            className="w-full flex items-center gap-3 px-2 py-2 rounded-md transition hover:bg-soft/[0.04] text-fog-200"
          >
            <span className="w-7 h-7 rounded-md bg-soft/10 text-accent flex items-center justify-center">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" opacity="0.85" />
              </svg>
            </span>
            <span className="text-sm flex-1 text-left">Settings</span>
          </Link>
        </div>

        {/* User menu */}
        <div className="border-t border-line p-3 relative">
          <button
            onClick={() => setUserMenuOpen((v) => !v)}
            className="w-full flex items-center gap-3 px-2 py-2 rounded-md hover:bg-soft/[0.04]"
          >
            <span className="w-7 h-7 rounded-full bg-soft/10 flex items-center justify-center text-xs">
              {(userEmail ?? '?').slice(0, 1).toUpperCase()}
            </span>
            <span className="text-sm truncate flex-1 text-left">{userEmail}</span>
            <span className="text-fog-400 text-xs">⋯</span>
          </button>
          {userMenuOpen && (
            <div className="absolute bottom-full left-3 right-3 mb-2 z-50 rounded-2xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60">
              <Link
                href="/"
                className="block px-3 py-2 rounded-md hover:bg-soft/[0.06]"
              >
                Back to home
              </Link>
              <button
                onClick={() => {
                  setUserMenuOpen(false)
                  setGithubModalOpen(true)
                }}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center justify-between"
              >
                <span>GitHub</span>
                <span className="text-[11px] text-fog-400 truncate ml-2">
                  {githubUsername ? `@${githubUsername}` : 'Not connected'}
                </span>
              </button>
              <button
                onClick={signOut}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06]"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* ────── Main ────── */}
      <main className="flex-1 flex flex-col min-w-0">
        {contextProfilesOpen ? (
          <ContextProfilesPanel
            onPickProfile={(id) => {
              setSelectedProfileId(id)
              if (typeof window !== 'undefined') {
                localStorage.setItem('selected_profile_id', id)
              }
              setContextProfilesOpen(false)
            }}
            onListChanged={() => loadProfiles()}
          />
        ) : demoOpen ? (
          <StrategyDemoPanel />
        ) : mcpsOpen ? (
          <MCPInventoryPanel embedded />
        ) : skillsOpen ? (
          <SkillsInventoryPanel embedded />
        ) : pluginsOpen ? (
          <PluginsInventoryPanel embedded />
        ) : (
        <>
        {/* Top bar */}
        <header className="h-12 px-5 border-b border-line flex items-center justify-between">
          <div className="text-sm flex items-center gap-2 min-w-0">
            {activeSessionObj ? (
              activeKind === 'project' ? (
                <>
                  <IconFolder small className="text-amber-500/80" />
                  <button
                    onClick={() => selectProject(activeSessionObj.id)}
                    className="text-fog-200 truncate hover:text-fog-50 transition"
                    title="Open project home"
                  >
                    {sessionDisplayName(activeSessionObj)}
                  </button>
                  {activeThreadObj && (
                    <>
                      <span className="text-fog-500">/</span>
                      <span className="text-fog-50 truncate">
                        {threadDisplayName(activeThreadObj)}
                      </span>
                    </>
                  )}
                </>
              ) : (
                <>
                  <IconChat small className="text-fog-400" />
                  <span className="text-fog-50 truncate">
                    {sessionDisplayName(activeSessionObj)}
                  </span>
                </>
              )
            ) : (
              <span className="text-fog-400">Select a project or chat</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {activeThread && messages.length > 0 && (
              <ReplayControls
                replayIdx={replayIdx}
                total={messages.length}
                onEnter={() => setReplayIdx(1)}
                onExit={() => setReplayIdx(null)}
                onStep={(d) =>
                  setReplayIdx((cur) => {
                    if (cur == null) return cur
                    return Math.min(messages.length, Math.max(1, cur + d))
                  })
                }
              />
            )}
            {activeSessionObj && activeKind === 'project' && (
              <ViewToggle mode={viewMode} onChange={setViewMode} />
            )}
            {activeSessionObj && activeKind === 'project' && (
              <ModeToggle
                mode={activeSessionObj.mode ?? 'auto'}
                onChange={(m) => updateSessionMode(activeSessionObj.id, m)}
              />
            )}
            {profiles.length > 0 ? (
              <ProfilePicker
                value={selectedProfileId}
                profiles={profiles}
                onChange={(id) => {
                  setSelectedProfileId(id)
                  if (typeof window !== 'undefined') {
                    localStorage.setItem('selected_profile_id', id)
                  }
                }}
                onOpenManager={() => {
                  setMcpsOpen(false)
                  setDemoOpen(false)
                  setContextProfilesOpen(true)
                }}
              />
            ) : (
              <StrategyToggle
                value={selectedStrategy || defaultStrategy}
                strategies={strategies}
                onChange={(id) => {
                  setSelectedStrategy(id)
                  if (typeof window !== 'undefined') {
                    localStorage.setItem('selected_strategy', id)
                  }
                }}
              />
            )}
            {providers.length > 0 ? (
              <div className="chip flex items-center gap-2 pr-1" title="LLM provider for this session">
                <span className="dot bg-emerald-400" />
                <select
                  value={
                    (activeSession && sessionProviders[activeSession]) || ''
                  }
                  onChange={(e) => {
                    const v = e.target.value
                    if (!activeSession) return
                    setSessionProvider(activeSession, v || null)
                  }}
                  disabled={!activeSession}
                  className="bg-transparent text-xs text-fog-50 outline-none max-w-[18rem] truncate disabled:opacity-50"
                >
                  <option value="" className="bg-ink-200 text-fog-50">
                    {(() => {
                      const def = providers.find((p) => p.is_default)
                      return def
                        ? `Default · ${def.label} · ${def.model_id}`
                        : 'Default (env fallback)'
                    })()}
                  </option>
                  {providers.map((p) => (
                    <option
                      key={p.id}
                      value={p.id}
                      className="bg-ink-200 text-fog-50"
                    >
                      {p.label} · {p.model_id}
                    </option>
                  ))}
                </select>
              </div>
            ) : models.length > 0 ? (
              <div className="chip flex items-center gap-2 pr-1">
                <span className="dot bg-emerald-400" />
                <select
                  value={selectedModel}
                  onChange={(e) => {
                    const v = e.target.value
                    setSelectedModel(v)
                    if (typeof window !== 'undefined') {
                      localStorage.setItem('selected_model', v)
                    }
                  }}
                  className="bg-transparent text-xs text-fog-50 outline-none max-w-[14rem] truncate"
                  title="Chat model"
                >
                  {(() => {
                    type M = { id: string; name: string; vision?: boolean }
                    const clean = (n: string) => n.replace(/\s*\(free\)\s*$/i, '')
                    const opt = (m: M) => (
                      <option key={m.id} value={m.id} className="bg-ink-200 text-fog-50">
                        {clean(m.name)}{m.vision ? '  👁' : ''}
                      </option>
                    )
                    const primary = PRIMARY_MODEL_IDS
                      .map((id) => models.find((m) => m.id === id))
                      .filter(Boolean) as M[]
                    const rest = models.filter((m) => !PRIMARY_MODEL_IDS.includes(m.id))
                    const visionRest = rest.filter((m) => m.vision)
                    const textRest = rest.filter((m) => !m.vision)
                    return (
                      <>
                        {primary.length > 0 && (
                          <optgroup label="Primary">{primary.map(opt)}</optgroup>
                        )}
                        {visionRest.length > 0 && (
                          <optgroup label="Vision (👁 — needed for Visual method)">
                            {visionRest.map(opt)}
                          </optgroup>
                        )}
                        {textRest.length > 0 && (
                          <optgroup label="Text-only">{textRest.map(opt)}</optgroup>
                        )}
                      </>
                    )
                  })()}
                </select>
              </div>
            ) : (
              <span className="chip">
                <span className="dot bg-emerald-400" />
                {selectedModel || 'default'}
              </span>
            )}
          </div>
        </header>

        {/* Files chip strip — shown for chats and projects when files exist */}
        {activeFiles.length > 0 && (
          <div className="px-6 pt-3 border-b border-line/60 pb-3">
            <div className="mx-auto max-w-3xl flex items-start gap-3">
              <span className="text-[11px] uppercase tracking-widest text-fog-400 mt-1.5 shrink-0">
                {activeKind === 'project' ? 'Project files' : 'Files'}
              </span>
              <div className="flex flex-wrap gap-1.5">
                {activeFiles.slice(0, 12).map((f) => {
                  const display = f.split('/').pop() || f
                  return (
                    <button
                      key={f}
                      onClick={() =>
                        activeSession && openFileViewer(activeSession, display)
                      }
                      className="chip text-[11px] py-0.5 hover:bg-soft/[0.08] cursor-pointer"
                      title={`Open ${display}`}
                    >
                      <IconFile />
                      {display}
                    </button>
                  )
                })}
                {activeFiles.length > 12 && (
                  <span className="chip text-[11px] py-0.5">
                    +{activeFiles.length - 12} more
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Conversation */}
        <div className="flex-1 overflow-y-auto relative">
          {viewMode === 'task' && activeKind === 'project' && activeThread ? (
            <TaskView
              messages={displayMessages}
              sessionId={activeSession}
              threadId={activeThread}
              sending={sending && replayIdx == null}
              pendingApproval={replayIdx == null ? pendingApproval : null}
              resolvingApproval={resolvingApproval}
              onDecide={(approved, reason) => resolveApproval(approved, reason)}
              onCancel={cancelChat}
              endRef={endRef}
              inflightTools={replayIdx == null ? inflightTools : []}
              llmThinking={llmThinking}
              liveTokens={replayIdx == null ? liveTokens : ''}
            />
          ) : (
            <div className="mx-auto max-w-3xl px-6 py-10">
              {!activeThread &&
                (activeKind === 'project' && activeSessionObj ? (
                  <ProjectLanding
                    session={activeSessionObj}
                    threads={threadsMap[activeSessionObj.id] || []}
                    files={filesMap[activeSessionObj.id] || []}
                    threadNames={threadNames}
                    sessionName={sessionDisplayName(activeSessionObj)}
                    onOpenThread={(tid) =>
                      selectThread(activeSessionObj.id, tid)
                    }
                    onOpenFile={(name) => openFileViewer(activeSessionObj.id, name)}
                    onNewThread={() => createThread(activeSessionObj.id)}
                  />
                ) : (
                  <EmptyState onPick={(text) => setInput(text)} />
                ))}

              <div className="space-y-5">
                {(() => {
                  const args = pairToolArgs(displayMessages)
                  return displayMessages
                    .map((m, i) => ({ m, i, args: args[i] }))
                    .filter(({ m }) =>
                      !(
                        m.role === 'assistant' &&
                        !m.content?.trim() &&
                        !(m.tool_calls && m.tool_calls.length > 0)
                      ) &&
                      !(
                        m.role === 'assistant' &&
                        m.tool_calls &&
                        m.tool_calls.length > 0
                      ),
                    )
                    .map(({ m, i, args }) => (
                      <MessageBlock
                        key={i}
                        msg={m}
                        sessionId={activeSession}
                        threadId={activeThread}
                        toolArgs={args}
                      />
                    ))
                })()}
                {/* In-flight tools — shown live as they start, before the
                   persisted message arrives. */}
                {replayIdx == null && inflightTools.map((t) => (
                  <InflightToolRow key={t.run_id} tool={t} />
                ))}
                {/* Live token preview — tokens stream in as the model
                   generates; replaced by the canonical message when it
                   lands in the DB. */}
                {replayIdx == null && liveTokens && (
                  <LiveAssistantPreview tokens={liveTokens} />
                )}
                {replayIdx == null && sending && !pendingApproval && !liveTokens && (
                  <TypingIndicator label={llmThinking ? 'thinking' : 'working'} />
                )}
                {replayIdx == null && pendingApproval && (
                  <ApprovalCard
                    approval={pendingApproval}
                    disabled={resolvingApproval}
                    onDecide={(approved, reason) =>
                      resolveApproval(approved, reason)
                    }
                  />
                )}
                {replayIdx == null && sending && (
                  <div className="flex justify-end">
                    <button
                      onClick={cancelChat}
                      className="text-[11px] px-2.5 py-1 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50"
                    >
                      Stop
                    </button>
                  </div>
                )}
                <div ref={endRef} />
              </div>
            </div>
          )}

          {/* Floating context-window ring — right side, vertically centered.
             Shows live token usage as a percentage with an arc that fills
             proportionally and changes color as usage climbs. */}
          {activeThread && (
            <ContextRingButton
              summary={contextSummary}
              onClick={openContextViewer}
            />
          )}

          {/* Floating history button — only for project sessions. Sits below
             the context-window button so the two stack on the right edge. */}
          {activeThread && activeKind === 'project' && (
            <button
              onClick={openWorkspaceHistory}
              className="fixed right-5 z-30 w-10 h-10 rounded-full bg-ink-200 border border-lineStrong text-fog-300 hover:text-fog-50 hover:bg-soft/[0.06] shadow-xl shadow-black/40 flex items-center justify-center transition opacity-70 hover:opacity-100"
              style={{ top: 'calc(50% + 48px)' }}
              title="Workspace history"
              aria-label="Workspace history"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M3 12a9 9 0 1 0 3-6.7" />
                <path d="M3 4v5h5" />
                <path d="M12 7v5l3 2" />
              </svg>
            </button>
          )}
        </div>

        {/* Composer — hidden on the project landing pane (no thread to
           send into; user picks or creates a thread first). */}
        <footer
          className={`border-t border-line ${
            activeKind === 'project' && !activeThread ? 'hidden' : ''
          }`}
        >
          <div className="mx-auto max-w-3xl px-6 py-4">
            <form
              onSubmit={(e) => {
                e.preventDefault()
                send()
              }}
              className="relative rounded-3xl border border-line bg-ink-100/80 hover:border-lineStrong focus-within:border-lineStrong transition-colors px-3 py-2"
            >
              {/* "/" slash menu — pick a skill to activate it for this message */}
              {slashMenuOpen &&
                (() => {
                  const cands = slashCandidates()
                  if (!cands.length) return null
                  return (
                    <div className="absolute bottom-full left-0 right-0 mb-2 z-50 rounded-xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60 max-h-72 overflow-y-auto">
                      <div className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wide text-fog-500">
                        Skills
                      </div>
                      {cands.map((s, i) => (
                        <button
                          key={s.name}
                          type="button"
                          onMouseEnter={() => setSlashIndex(i)}
                          onClick={() => activateSkill(s.name)}
                          className={`w-full text-left px-3 py-2 rounded-md flex items-start gap-2.5 ${
                            i === slashIndex ? 'bg-soft/[0.08]' : 'hover:bg-soft/[0.06]'
                          }`}
                        >
                          <span className="text-accent mt-0.5">
                            <IconSkill />
                          </span>
                          <span className="min-w-0">
                            <span className="text-fog-50 font-medium">
                              /{s.name}
                            </span>
                            <span className="block text-[11px] text-fog-400 line-clamp-1">
                              {s.description}
                            </span>
                          </span>
                        </button>
                      ))}
                    </div>
                  )
                })()}

              {/* Active-skill chips (force-activated via "/") */}
              {triggeredSkills.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-1.5 pb-2">
                  {triggeredSkills.map((name) => (
                    <span
                      key={name}
                      className="chip text-[11px] py-0.5 pr-1 bg-accent/10 border-accent/30 text-accent"
                      title={`Skill ${name} will run for this message`}
                    >
                      <IconSkill />
                      <span className="max-w-[180px] truncate">{name}</span>
                      <button
                        type="button"
                        onClick={() =>
                          setTriggeredSkills((prev) =>
                            prev.filter((n) => n !== name),
                          )
                        }
                        className="ml-1 w-4 h-4 rounded-full text-accent/70 hover:text-accent hover:bg-soft/[0.1] flex items-center justify-center text-[10px]"
                        aria-label={`Remove ${name}`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}

              {pendingFiles.length > 0 && (
                <div className="flex flex-wrap gap-1.5 px-1.5 pb-2">
                  {pendingFiles.map((f) => (
                    <span
                      key={f.name}
                      className="chip text-[11px] py-0.5 pr-1 group"
                      title={f.name}
                    >
                      <IconFile />
                      <span className="max-w-[180px] truncate">{f.name}</span>
                      <button
                        type="button"
                        onClick={() => removePendingFile(f.name)}
                        className="ml-1 w-4 h-4 rounded-full text-fog-400 hover:text-fog-50 hover:bg-soft/[0.1] flex items-center justify-center text-[10px]"
                        aria-label={`Remove ${f.name}`}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <div className="flex items-end gap-2">
                <div className="relative shrink-0" ref={composerMenuRef}>
                  <button
                    type="button"
                    onClick={() => setComposerMenuOpen((v) => !v)}
                    disabled={!activeSession || uploading}
                    className="w-9 h-9 rounded-full hover:bg-soft/[0.06] text-fog-300 hover:text-fog-50 flex items-center justify-center transition disabled:opacity-30 disabled:cursor-not-allowed"
                    title={
                      activeSession ? 'Attach files' : 'Pick a chat first'
                    }
                  >
                    {uploading ? (
                      <span className="w-3 h-3 rounded-full border-2 border-fog-300 border-t-transparent animate-spin" />
                    ) : (
                      <PlusIcon />
                    )}
                  </button>

                  {composerMenuOpen && (
                    <div className="absolute bottom-full mb-2 left-0 z-50 w-52 rounded-xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60">
                      <button
                        type="button"
                        onClick={() => {
                          setComposerMenuOpen(false)
                          fileInputRef.current?.click()
                        }}
                        className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
                      >
                        <IconFile />
                        <div>
                          <div className="text-fog-100">Upload files</div>
                          <div className="text-[11px] text-fog-400">
                            PDF, Word, text, code…
                          </div>
                        </div>
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setComposerMenuOpen(false)
                          photoInputRef.current?.click()
                        }}
                        className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
                      >
                        <IconImage />
                        <div>
                          <div className="text-fog-100">Upload photos</div>
                          <div className="text-[11px] text-fog-400">
                            Stored as project files
                          </div>
                        </div>
                      </button>

                      <div className="my-1 border-t border-line" />

                      <button
                        type="button"
                        onClick={() => setSkillsFlyoutOpen((v) => !v)}
                        aria-expanded={skillsFlyoutOpen}
                        className={`w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5 ${
                          skillsFlyoutOpen ? 'bg-soft/[0.06]' : ''
                        }`}
                      >
                        <IconSkill />
                        <div className="flex-1">
                          <div className="text-fog-100">Skills</div>
                          <div className="text-[11px] text-fog-400">
                            Turn specialist instructions on
                          </div>
                        </div>
                        <span className="text-fog-400 text-xs">›</span>
                      </button>

                      {/* Quick-toggle flyout to the side, claude.ai-style.
                          Nested in the menu's DOM so the outside-click guard
                          still treats interactions here as "inside". */}
                      {skillsFlyoutOpen && (
                        <div className="absolute left-full bottom-0 ml-2">
                          <SkillsComposerFlyout
                            onManage={() => {
                              setDemoOpen(false)
                              setContextProfilesOpen(false)
                              setMcpsOpen(false)
                              setSkillsOpen(true)
                            }}
                            onClose={() => {
                              setSkillsFlyoutOpen(false)
                              setComposerMenuOpen(false)
                            }}
                          />
                        </div>
                      )}

                      <button
                        type="button"
                        onClick={() => {
                          setComposerMenuOpen(false)
                          setSkillsFlyoutOpen(false)
                          setDemoOpen(false)
                          setContextProfilesOpen(false)
                          setMcpsOpen(false)
                          setSkillsOpen(false)
                          setPluginsOpen(true)
                        }}
                        className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
                      >
                        <IconPlugin />
                        <div className="flex-1">
                          <div className="text-fog-100">Add plugins…</div>
                          <div className="text-[11px] text-fog-400">
                            Give the agent new tools
                          </div>
                        </div>
                      </button>
                    </div>
                  )}

                  <input
                    ref={fileInputRef}
                    type="file"
                    multiple
                    className="hidden"
                    onChange={(e) => {
                      addPendingFiles(e.target.files)
                      e.target.value = ''
                    }}
                  />
                  <input
                    ref={photoInputRef}
                    type="file"
                    multiple
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      addPendingFiles(e.target.files)
                      e.target.value = ''
                    }}
                  />
                </div>

                <textarea
                  ref={composerRef}
                  value={input}
                  onChange={(e) => {
                    const v = e.target.value
                    setInput(v)
                    const open = v.startsWith('/')
                    setSlashMenuOpen(open)
                    if (open) setSlashIndex(0)
                  }}
                  onKeyDown={(e) => {
                    const cands = slashMenuOpen ? slashCandidates() : []
                    if (slashMenuOpen && cands.length) {
                      if (e.key === 'ArrowDown') {
                        e.preventDefault()
                        setSlashIndex((i) => (i + 1) % cands.length)
                        return
                      }
                      if (e.key === 'ArrowUp') {
                        e.preventDefault()
                        setSlashIndex((i) => (i - 1 + cands.length) % cands.length)
                        return
                      }
                      if (e.key === 'Enter' || e.key === 'Tab') {
                        e.preventDefault()
                        activateSkill(cands[Math.min(slashIndex, cands.length - 1)].name)
                        return
                      }
                      if (e.key === 'Escape') {
                        e.preventDefault()
                        setSlashMenuOpen(false)
                        return
                      }
                    }
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      send()
                    }
                  }}
                  disabled={!activeThread || sending}
                  rows={1}
                  placeholder={
                    activeThread
                      ? 'Reply to Agent…  (type / for skills)'
                      : 'Start a new chat or pick one from the sidebar'
                  }
                  className="flex-1 resize-none bg-transparent outline-none px-1 py-2 text-[15px] placeholder:text-fog-400 disabled:opacity-50 leading-6"
                />

                <button
                  type="submit"
                  disabled={!activeThread || sending || !input.trim()}
                  className="shrink-0 rounded-full bg-accent text-ink-50 w-9 h-9 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed hover:bg-accent/90 transition"
                  title="Send"
                >
                  {sending ? (
                    <span className="w-3 h-3 rounded-full border-2 border-ink-50 border-t-transparent animate-spin" />
                  ) : (
                    <ArrowUp />
                  )}
                </button>
              </div>
            </form>
            <p className="text-[11px] text-fog-500 mt-3 text-center">
              The agent can be wrong. Verify important outputs.
            </p>
          </div>
        </footer>
        </>
        )}
      </main>

      {/* New project modal */}
      {projectModalOpen && (
        <NewProjectModal
          githubUsername={githubUsername}
          onOpenGithub={() => {
            setProjectModalOpen(false)
            setGithubModalOpen(true)
          }}
          onClose={() => setProjectModalOpen(false)}
          onCreate={async (name, files, github) => {
            setProjectModalOpen(false)
            await createProject(name, files, github)
          }}
        />
      )}

      {/* Centered confirm dialog */}
      {confirmDialog && (
        <ConfirmDialog
          title={confirmDialog.title}
          message={confirmDialog.message}
          confirmLabel={confirmDialog.confirmLabel}
          danger={confirmDialog.danger}
          onCancel={() => setConfirmDialog(null)}
          onConfirm={() => {
            const fn = confirmDialog.onConfirm
            setConfirmDialog(null)
            fn()
          }}
        />
      )}

      {/* Centered prompt dialog */}
      {promptDialog && (
        <PromptDialog
          title={promptDialog.title}
          message={promptDialog.message}
          placeholder={promptDialog.placeholder}
          initialValue={promptDialog.initialValue}
          confirmLabel={promptDialog.confirmLabel}
          onCancel={() => setPromptDialog(null)}
          onConfirm={(value) => {
            const fn = promptDialog.onConfirm
            setPromptDialog(null)
            fn(value)
          }}
        />
      )}

      {/* File viewer side panel */}
      {viewerFile && (
        <FileViewer
          name={viewerFile.name}
          content={viewerContent}
          loading={viewerLoading}
          error={viewerError}
          onClose={() => setViewerFile(null)}
        />
      )}

      {/* Workspace history side panel */}
      {historyOpen && (
        <HistoryPanel
          commits={historyData}
          loading={historyLoading}
          error={historyError}
          revertingId={revertingId}
          onClose={() => setHistoryOpen(false)}
          onRefresh={openWorkspaceHistory}
          onRevert={(id, msg) =>
            setConfirmDialog({
              title: 'Undo this change?',
              message: `This will run \`git revert\` for "${msg}". The file(s) will return to the state before this commit. A new "Revert …" commit will be added to history.`,
              confirmLabel: 'Undo',
              danger: false,
              onConfirm: () => {
                setConfirmDialog(null)
                revertCommit(id)
              },
            })
          }
        />
      )}

      {/* Context window side panel */}
      {contextOpen && (
        <ContextViewer
          data={contextData}
          loading={contextLoading}
          error={contextError}
          sessionId={activeSession}
          threadId={activeThread}
          onClose={() => setContextOpen(false)}
          onRefresh={openContextViewer}
          onSoftRefresh={softRefreshContext}
          onDelete={(id) => {
            // Ask once per browser; after the user acknowledges, delete
            // straight away on subsequent clicks (better UX for bulk cleanup).
            const skip =
              typeof window !== 'undefined' &&
              localStorage.getItem('acm_skip_msg_delete_confirm') === '1'
            if (skip) {
              deleteContextMessage(id)
              return
            }
            setConfirmDialog({
              title: 'Remove from context?',
              message:
                "This message will be deleted from the conversation and from the model's memory of this thread. This can't be undone. You won't be asked again for message deletions in this browser.",
              confirmLabel: 'Remove',
              danger: true,
              onConfirm: () => {
                setConfirmDialog(null)
                if (typeof window !== 'undefined') {
                  localStorage.setItem('acm_skip_msg_delete_confirm', '1')
                }
                deleteContextMessage(id)
              },
            })
          }}
        />
      )}

      {/* GitHub connection modal */}
      {githubModalOpen && (
        <GithubConnectModal
          username={githubUsername}
          onClose={() => setGithubModalOpen(false)}
          onSave={saveGithubToken}
          onDisconnect={async () => {
            await disconnectGithub()
            setToast('GitHub disconnected')
          }}
        />
      )}

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] px-4 py-2 rounded-full bg-accent text-ink-50 text-sm shadow-2xl animate-float-up">
          {toast}
        </div>
      )}
    </div>
  )
}

/* ─────────────────────────── empty state ─────────────────────────── */

function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="text-center pt-16 pb-8 animate-float-up">
      <div className="serif text-5xl tracking-tighter text-fog-50 mb-3">
        Where should we begin?
      </div>
      <p className="text-fog-300 text-base mb-10 max-w-md mx-auto">
        Start a new chat or open a project from the sidebar. Chats inside a
        project share context; standalone chats stay isolated.
      </p>
      <div className="grid sm:grid-cols-2 gap-3 max-w-xl mx-auto text-left">
        {hints.map((h) => (
          <button
            key={h.title}
            onClick={() => onPick(h.prompt)}
            className="surface p-4 hover:bg-soft/[0.03] transition cursor-pointer text-left"
          >
            <div className="text-sm text-fog-50 mb-1">{h.title}</div>
            <div className="text-xs text-fog-400 leading-relaxed">{h.body}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

const hints = [
  {
    title: 'Plan a migration',
    body: 'Walk through a multi-step refactor across files.',
    prompt:
      'I need to plan a migration. Help me break it into ordered steps and call out the risky parts.',
  },
  {
    title: 'Review a change',
    body: 'Paste a diff and ask for a focused review.',
    prompt:
      "Here's a diff — review it for correctness, edge cases, and code-style issues:\n\n",
  },
  {
    title: 'Draft a spec',
    body: 'Brainstorm an API or schema together.',
    prompt:
      "Help me draft a spec. I'll describe the goals; please ask clarifying questions before drafting.",
  },
  {
    title: 'Debug a problem',
    body: 'Share logs and stack traces.',
    prompt:
      "Help me debug. Here's what I'm seeing:\n\n",
  },
]

/* ─────────────────────────── messages ─────────────────────────── */

function LiveAssistantPreview({ tokens }: { tokens: string }) {
  return (
    <div className="animate-float-up">
      <div className="flex items-center gap-2 mb-2 text-[11px] uppercase tracking-widest text-fog-400">
        <span className="w-5 h-5 rounded-full bg-soft/10 flex items-center justify-center">
          <span className="w-1.5 h-1.5 bg-accent rounded-sm" />
        </span>
        Agent
      </div>
      <div className="text-[15px] leading-7 text-fog-100 whitespace-pre-wrap">
        {tokens}
        <span
          className="inline-block w-[7px] h-[14px] -mb-[2px] ml-0.5 bg-fog-300 animate-pulse"
          aria-hidden
        />
      </div>
    </div>
  )
}

function TypingIndicator({ label = 'thinking' }: { label?: string }) {
  return (
    <div className="flex gap-3 text-[13px] animate-float-up">
      <div className="flex flex-col items-center pt-1.5 shrink-0">
        <span className="relative w-2 h-2">
          <span className="absolute inset-0 rounded-full bg-sky-300 animate-ping" />
          <span className="relative w-2 h-2 rounded-full bg-sky-300 inline-block" />
        </span>
      </div>
      <div className="flex items-center gap-2 text-fog-400">
        <span className="italic">{label}…</span>
      </div>
    </div>
  )
}

function MessageBlock({
  msg,
  sessionId,
  threadId,
  toolArgs,
}: {
  msg: Message
  sessionId: string | null
  threadId: string | null
  toolArgs?: Record<string, any>
}) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end animate-float-up">
        <div className="max-w-[80%] rounded-3xl bg-soft/[0.06] border border-line text-fog-100 px-4 py-2.5 text-[15px] leading-7 whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    )
  }
  if (msg.role === 'tool') {
    return (
      <ToolMessageCard
        msg={msg}
        sessionId={sessionId}
        threadId={threadId}
        args={toolArgs}
      />
    )
  }
  return (
    <div className="animate-float-up">
      <div className="flex items-center gap-2 mb-2 text-[11px] uppercase tracking-widest text-fog-400">
        <span className="w-5 h-5 rounded-full bg-soft/10 flex items-center justify-center">
          <span className="w-1.5 h-1.5 bg-accent rounded-sm" />
        </span>
        Agent
      </div>
      <div className="text-[15px] leading-7 text-fog-100 markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
      </div>
    </div>
  )
}

/* ─────────────────────────── approval card (Confirm mode) ─────────────────── */

function ApprovalCard({
  approval,
  disabled,
  onDecide,
}: {
  approval: PendingApproval
  disabled: boolean
  onDecide: (approved: boolean, reason?: string) => void
}) {
  const tool = approval.tool
  return (
    <div className="animate-float-up rounded-xl border border-amber-300/30 bg-amber-300/[0.04] p-3.5 text-[13px]">
      <div className="flex items-center gap-2 mb-2 text-[11px] uppercase tracking-widest text-amber-300/90">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 9v3.75M12 20.25h.008v.008H12v-.008Z" />
          <path d="M10.363 3.591 1.91 18.083a1.875 1.875 0 0 0 1.636 2.793h16.908a1.875 1.875 0 0 0 1.636-2.793L13.637 3.591a1.875 1.875 0 0 0-3.274 0Z" />
        </svg>
        Agent wants to {tool === 'write_project_file' ? 'edit a file' : tool === 'run_shell' ? 'run a command' : tool}
      </div>

      {tool === 'write_project_file' && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-fog-100">
            <span className="text-fog-400">Write</span>
            <code className="font-mono text-fog-50">
              {(approval as any).filename}
            </code>
            <span className="text-[11px] font-mono text-fog-500">
              {(approval as any).size} bytes
            </span>
          </div>
          {(approval as any).preview && (
            <pre className="font-mono text-[12px] text-fog-200 bg-ink-100 border border-line rounded p-2 max-h-44 overflow-y-auto whitespace-pre-wrap">
              {(approval as any).preview}
            </pre>
          )}
        </div>
      )}

      {tool === 'run_shell' && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-fog-100">
            <span className="text-fog-400">Run in</span>
            <code className="font-mono text-fog-200">{(approval as any).cwd}</code>
          </div>
          <pre className="font-mono text-[12px] text-fog-100 bg-ink-100 border border-line rounded p-2 whitespace-pre-wrap">
            $ {(approval as any).cmd}
          </pre>
        </div>
      )}

      <div className="flex items-center justify-end gap-2 mt-3">
        <button
          onClick={() => onDecide(false, 'denied via confirm prompt')}
          disabled={disabled}
          className="text-[12px] px-3 py-1.5 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 disabled:opacity-40 transition"
        >
          Deny
        </button>
        <button
          onClick={() => onDecide(true)}
          disabled={disabled}
          className="text-[12px] px-3 py-1.5 rounded-md bg-accent text-ink-100 hover:bg-fog-100 disabled:opacity-40 transition font-medium"
        >
          {disabled ? 'Working…' : 'Approve'}
        </button>
      </div>
    </div>
  )
}

/* ─────────────────────────── three-pane task view ─────────────────────────── */

function TaskView({
  messages,
  sessionId,
  threadId,
  sending,
  pendingApproval,
  resolvingApproval,
  onDecide,
  onCancel,
  endRef,
  inflightTools,
  llmThinking,
  liveTokens,
}: {
  messages: Message[]
  sessionId: string | null
  threadId: string | null
  sending: boolean
  pendingApproval: PendingApproval | null
  resolvingApproval: boolean
  onDecide: (approved: boolean, reason?: string) => void
  onCancel: () => void
  endRef: React.RefObject<HTMLDivElement>
  inflightTools: InflightTool[]
  llmThinking: boolean
  liveTokens: string
}) {
  const argsByIdx = pairToolArgs(messages)

  const conversation = messages
    .map((m, i) => ({ m, i }))
    .filter(({ m }) =>
      (m.role === 'user' || (m.role === 'assistant' && m.content?.trim())) &&
      !(m.role === 'assistant' && m.tool_calls && m.tool_calls.length > 0),
    )

  const activity = messages
    .map((m, i) => ({ m, i }))
    .filter(({ m }) =>
      m.role === 'tool' ||
      (m.role === 'assistant' && m.tool_calls && m.tool_calls.length > 0),
    )

  // Latest commit SHA — drives the diff pane. We watch messages for
  // committed-as-... markers; the diff pane refetches on change.
  const latestSha = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.tool_name === 'write_project_file' && m.content) {
        const match = m.content.match(/committed as Agent: \w+ .+? \(([a-f0-9]+)\)/)
        if (match) return match[1]
      }
    }
    return null
  })()

  return (
    <div className="grid grid-cols-[minmax(260px,1fr)_minmax(320px,1.4fr)_minmax(320px,1.2fr)] divide-x divide-line h-full min-h-0">
      {/* Column 1: Activity feed */}
      <div className="flex flex-col min-h-0">
        <div className="px-3 py-2 border-b border-line text-[11px] uppercase tracking-widest text-fog-400 flex items-center justify-between">
          <span>Activity</span>
          <span className="text-fog-500 normal-case tracking-normal">
            {activity.length} event{activity.length === 1 ? '' : 's'}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
          {activity.length === 0 && inflightTools.length === 0 && (
            <div className="text-[12px] text-fog-500 px-1 py-4">
              No tool calls yet. The agent's commands and file edits will
              stream here as they happen.
            </div>
          )}
          {activity.map(({ m, i }) =>
            m.role === 'tool' ? (
              <MessageBlock
                key={i}
                msg={m}
                sessionId={sessionId}
                threadId={threadId}
                toolArgs={argsByIdx[i]}
              />
            ) : (
              <ActivityToolCallStub key={i} msg={m} />
            ),
          )}
          {inflightTools.map((t) => (
            <InflightToolRow key={t.run_id} tool={t} />
          ))}
          {sending && !pendingApproval && (
            <TypingIndicator label={llmThinking ? 'thinking' : 'working'} />
          )}
        </div>
      </div>

      {/* Column 2: Diff pane */}
      <div className="flex flex-col min-h-0">
        <div className="px-3 py-2 border-b border-line text-[11px] uppercase tracking-widest text-fog-400">
          Working diff
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          {latestSha && sessionId ? (
            <DiffPane sessionId={sessionId} sha={latestSha} />
          ) : (
            <div className="text-[12px] text-fog-500">
              No edits in this session yet. The most recent file change will
              show its unified diff here.
            </div>
          )}
        </div>
      </div>

      {/* Column 3: Chat + composer */}
      <div className="flex flex-col min-h-0">
        <div className="px-3 py-2 border-b border-line text-[11px] uppercase tracking-widest text-fog-400">
          Chat
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">
          {conversation.map(({ m, i }) => (
            <MessageBlock
              key={i}
              msg={m}
              sessionId={sessionId}
              threadId={threadId}
              toolArgs={argsByIdx[i]}
            />
          ))}
          {liveTokens && <LiveAssistantPreview tokens={liveTokens} />}
          {sending && !pendingApproval && !liveTokens && (
            <TypingIndicator label={llmThinking ? 'thinking' : 'working'} />
          )}
          {pendingApproval && (
            <ApprovalCard
              approval={pendingApproval}
              disabled={resolvingApproval}
              onDecide={onDecide}
            />
          )}
          {sending && (
            <div className="flex justify-end">
              <button
                onClick={onCancel}
                className="text-[11px] px-2.5 py-1 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50"
              >
                Stop
              </button>
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>
    </div>
  )
}

// One-line preview of an assistant tool-call intermediate — used in the
// Activity column so the user sees what the agent decided to call.
function ActivityToolCallStub({ msg }: { msg: Message }) {
  const names = (msg.tool_calls ?? []).map((c) => c.name).join(', ')
  return (
    <div className="text-[11px] text-fog-400 font-mono px-2 py-1 rounded border border-line bg-ink-100/40">
      → invoking {names || 'tool'}
    </div>
  )
}

// Tiny self-contained component that fetches and renders a single commit's
// diff. Reuses the DiffBlock renderer.
function DiffPane({ sessionId, sha }: { sessionId: string; sha: string }) {
  const [diff, setDiff] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setErr(null)
    setDiff(null)
    ;(async () => {
      try {
        const r = await authFetch(
          `/api/sessions/${sessionId}/commits/${sha}/diff`,
        )
        if (cancelled) return
        if (!r.ok) {
          let detail = `${r.status} ${r.statusText}`
          try {
            const body = await r.json()
            if (body?.detail) detail = body.detail
          } catch {}
          setErr(detail)
          return
        }
        const body = await r.json()
        setDiff(body.diff ?? '')
      } catch (e: any) {
        if (!cancelled) setErr(e?.message ?? 'network')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [sessionId, sha])

  if (loading) return <div className="text-[12px] text-fog-400">Loading diff…</div>
  if (err)
    return <div className="text-[12px] text-rose-300">Failed: {err}</div>
  if (!diff) return <div className="text-[12px] text-fog-500">No diff content.</div>
  return (
    <div className="rounded border border-line bg-ink-100 overflow-hidden">
      <div className="px-3 py-1.5 border-b border-line text-[11px] text-fog-500 font-mono">
        {sha.slice(0, 7)}
      </div>
      <DiffBlock diff={diff} />
    </div>
  )
}

/* ─────────────────────────── replay scrubber ─────────────────────────────── */

function ReplayControls({
  replayIdx,
  total,
  onEnter,
  onExit,
  onStep,
}: {
  replayIdx: number | null
  total: number
  onEnter: () => void
  onExit: () => void
  onStep: (delta: number) => void
}) {
  if (replayIdx == null) {
    return (
      <button
        onClick={onEnter}
        className="text-[11px] px-2.5 py-1 rounded-full border border-line bg-ink-200 text-fog-300 hover:text-fog-50 hover:bg-soft/[0.06]"
        title="Step through this thread message-by-message"
      >
        Replay
      </button>
    )
  }
  return (
    <div className="flex items-center gap-1 rounded-full border border-amber-300/30 bg-amber-300/[0.06] p-0.5 text-[11px]">
      <span className="px-2 text-amber-300/90 font-mono">
        {replayIdx}/{total}
      </span>
      <button
        onClick={() => onStep(-1)}
        disabled={replayIdx <= 1}
        className="px-2 py-1 rounded-full text-fog-200 hover:text-fog-50 hover:bg-soft/[0.06] disabled:opacity-40"
        title="Previous"
      >
        ◀
      </button>
      <button
        onClick={() => onStep(1)}
        disabled={replayIdx >= total}
        className="px-2 py-1 rounded-full text-fog-200 hover:text-fog-50 hover:bg-soft/[0.06] disabled:opacity-40"
        title="Next"
      >
        ▶
      </button>
      <button
        onClick={onExit}
        className="px-2 py-1 rounded-full text-fog-300 hover:text-fog-50 hover:bg-soft/[0.06]"
        title="Exit replay"
      >
        Live
      </button>
    </div>
  )
}

/* ─────────────────────────── view toggle (Chat / Task) ───────────────────── */

function ViewToggle({
  mode,
  onChange,
}: {
  mode: 'chat' | 'task'
  onChange: (m: 'chat' | 'task') => void
}) {
  return (
    <div
      className="flex items-center rounded-full border border-line bg-ink-200 p-0.5 text-[11px]"
      title={
        mode === 'chat'
          ? 'Chat view — linear conversation.'
          : 'Task view — activity feed, live diff, and chat side by side.'
      }
    >
      {(['chat', 'task'] as const).map((m) => (
        <button
          key={m}
          onClick={() => mode !== m && onChange(m)}
          className={`px-2.5 py-1 rounded-full transition ${
            mode === m
              ? 'bg-soft/[0.10] text-fog-50'
              : 'text-fog-400 hover:text-fog-50'
          }`}
        >
          {m === 'chat' ? 'Chat' : 'Task'}
        </button>
      ))}
    </div>
  )
}

/* ─────────────────────────── mode toggle (Auto / Confirm) ────────────────── */

/* PR #8: profile picker — dropdown over the user's saved + built-in
 * context-management profiles. Replaces StrategyToggle in the header.
 * Each entry is one bundle of (tool surface + technique toggles); the
 * tooltip shows which techniques are on so the user can scan at a
 * glance. */
function ProfilePicker({
  value,
  profiles,
  onChange,
  onOpenManager,
}: {
  value: string
  profiles: {
    id: string
    name: string
    built_in: boolean
    summary: string | null
    is_default: boolean
    body: any
  }[]
  onChange: (id: string) => void
  onOpenManager: () => void
}) {
  const current = profiles.find((p) => p.id === value)
  function describe(p: any): string {
    const cm = p.body?.context_management ?? {}
    const on: string[] = []
    if (cm.tool_result_trimming?.enabled) on.push('trim')
    if (cm.summarization?.enabled) on.push('summarise')
    if (cm.memory?.enabled) on.push('memory')
    if (cm.subagent?.enabled) on.push('subagent')
    if (cm.sliding_window?.enabled) on.push('sliding')
    return on.length ? `${p.body?.tool_surface} · ${on.join(' · ')}` : p.body?.tool_surface
  }
  return (
    <div
      className="chip flex items-center gap-2 pr-1"
      title={
        current
          ? `${current.name}: ${current.summary ?? describe(current)}`
          : 'Context-management profile (bundle of tool surface + edit toggles)'
      }
    >
      <span className="dot bg-emerald-400" />
      <select
        value={value}
        onChange={(e) => {
          if (e.target.value === '__manage__') {
            onOpenManager()
            return
          }
          onChange(e.target.value)
        }}
        className="bg-transparent text-xs text-fog-50 outline-none max-w-[16rem] truncate"
      >
        {profiles.map((p) => (
          <option key={p.id} value={p.id} className="bg-ink-200 text-fog-50">
            {p.name}{p.is_default ? ' ★' : ''}
          </option>
        ))}
        <option disabled className="bg-ink-200 text-fog-500">──────────</option>
        <option value="__manage__" className="bg-ink-200 text-fog-50">
          Manage profiles…
        </option>
      </select>
    </div>
  )
}

function StrategyToggle({
  value,
  onChange,
  strategies,
}: {
  value: string
  onChange: (id: string) => void
  strategies: { id: string; label: string; summary: string }[]
}) {
  // Hide entirely when the backend reports fewer than two options —
  // there's nothing to toggle.
  if (strategies.length < 2) return null
  const current = strategies.find((s) => s.id === value)
  return (
    <div
      className="flex items-center rounded-full border border-line bg-ink-200 p-0.5 text-[11px]"
      title={
        current
          ? `${current.label}: ${current.summary}`
          : 'Context-management strategy'
      }
    >
      {strategies.map((s) => (
        <button
          key={s.id}
          onClick={() => value !== s.id && onChange(s.id)}
          className={`px-2.5 py-1 rounded-full transition ${
            value === s.id
              ? 'bg-soft/[0.10] text-fog-50'
              : 'text-fog-400 hover:text-fog-50'
          }`}
        >
          {s.label}
        </button>
      ))}
    </div>
  )
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: 'auto' | 'confirm'
  onChange: (m: 'auto' | 'confirm') => void
}) {
  return (
    <div
      className="flex items-center rounded-full border border-line bg-ink-200 p-0.5 text-[11px]"
      title={
        mode === 'auto'
          ? 'Auto: agent edits and runs commands freely.'
          : 'Confirm: agent asks before each file edit or shell command.'
      }
    >
      {(['auto', 'confirm'] as const).map((m) => (
        <button
          key={m}
          onClick={() => mode !== m && onChange(m)}
          className={`px-2.5 py-1 rounded-full transition ${
            mode === m
              ? 'bg-soft/[0.10] text-fog-50'
              : 'text-fog-400 hover:text-fog-50'
          }`}
        >
          {m === 'auto' ? 'Auto' : 'Confirm'}
        </button>
      ))}
    </div>
  )
}

/* ─────────────────────────── tool-message cards ─────────────────────────── */

function ToolMessageCard({
  msg,
  sessionId,
  threadId,
  args,
}: {
  msg: Message
  sessionId: string | null
  threadId: string | null
  args?: Record<string, any>
}) {
  const name = msg.tool_name ?? ''
  const content = msg.content ?? ''
  const isError = content.startsWith('Error')

  // Visual method: the output was rasterised into page image(s) and the
  // transcript only kept an `[image]` marker. Show a viewer that fetches
  // the real PNGs from the checkpoint on click.
  if (!isError && content.includes('[image]')) {
    return (
      <VisualImagesEventRow
        sessionId={sessionId}
        threadId={threadId}
        messageId={msg.id}
        toolName={name}
        content={content}
      />
    )
  }

  if (name === 'run_shell')
    return <ShellEventRow content={content} cmdArg={args?.cmd} />
  if (name === 'write_project_file')
    return (
      <WriteFileEventRow
        content={content}
        isError={isError}
        sessionId={sessionId}
        filenameArg={args?.filename}
      />
    )
  if (name === 'read_project_file')
    return (
      <ReadFileEventRow
        content={content}
        isError={isError}
        filenameArg={args?.filename}
      />
    )
  if (name === 'list_project_files')
    return <ListFilesEventRow content={content} isError={isError} />

  if (name === 'read_skill')
    return (
      <EventRow
        icon={<SkillRunIcon />}
        verb="Skill"
        target={String(args?.skill_name ?? '')}
        verbColor="text-accent"
        subtext={
          <span className="text-fog-500">
            {isError ? 'not found' : 'instructions loaded'}
          </span>
        }
      />
    )

  if (name === 'skill_used')
    return (
      <EventRow
        icon={<SkillRunIcon />}
        verb="Skill"
        target={content}
        verbColor="text-accent"
        subtext={<span className="text-fog-500">applied</span>}
      />
    )

  if (PLUGIN_TOOL_NAMES.has(name))
    return (
      <EventRow
        icon={<PluginRunIcon />}
        verb="Plugin"
        target={name}
        verbColor="text-accent"
      >
        <DetailsBlock content={content} />
      </EventRow>
    )

  return (
    <EventRow icon={<ToolIcon />} verb={name || 'tool'} verbColor="text-fog-200">
      <DetailsBlock content={content} />
    </EventRow>
  )
}

/** Visual-method tool result: the output was converted into one or more
 *  page images. Click to fetch + view them (lazily, from the checkpoint). */
function VisualImagesEventRow({
  sessionId,
  threadId,
  messageId,
  toolName,
  content,
}: {
  sessionId: string | null
  threadId: string | null
  messageId?: number
  toolName: string
  content: string
}) {
  const [open, setOpen] = useState(false)
  const [images, setImages] = useState<string[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Best-effort page count from the flattened markers, for the badge.
  const pageHint = (() => {
    const m = content.match(/\[page \d+\/(\d+)\]/)
    if (m) return parseInt(m[1], 10)
    return (content.match(/\[image\]/g) || []).length || 1
  })()

  // REFERENCES text the model can cite — strip the image/page markers.
  const refs = content
    .replace(/\[page \d+\/\d+\]/g, '')
    .replace(/\[image\]/g, '')
    .trim()

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && images === null && !loading) {
      if (!sessionId || !threadId || !messageId) {
        setError('Cannot load images for this message.')
        return
      }
      setLoading(true)
      setError(null)
      try {
        const r = await authFetch(
          `/api/sessions/${sessionId}/threads/${threadId}/messages/${messageId}/images`,
        )
        if (!r.ok) throw new Error(`Failed to load (HTTP ${r.status})`)
        const data = await r.json()
        const list: string[] = Array.isArray(data?.images) ? data.images : []
        setImages(list)
        if (list.length === 0) {
          setError(
            'No images available — they may have been removed from the conversation context.',
          )
        }
      } catch (e: any) {
        setError(e?.message ?? 'Failed to load images')
      } finally {
        setLoading(false)
      }
    }
  }

  return (
    <EventRow
      icon={<ToolIcon />}
      verb={toolName || 'tool'}
      target="→ image"
      verbColor="text-fog-200"
      badge={
        <span className="text-[10px] uppercase tracking-wider text-violet-300 bg-violet-500/10 rounded px-1.5 py-0.5">
          visual · {pageHint} page{pageHint === 1 ? '' : 's'}
        </span>
      }
      onToggle={toggle}
      open={open}
    >
      {open && (
        <div className="space-y-2">
          {refs && (
            <pre className="text-[11px] text-fog-400 whitespace-pre-wrap max-h-32 overflow-auto bg-ink-200/50 rounded p-2 border border-line">
              {refs}
            </pre>
          )}
          {loading && (
            <div className="text-[12px] text-fog-500">Loading images…</div>
          )}
          {error && <div className="text-[12px] text-rose-300">{error}</div>}
          {images?.map((src, i) => (
            <div
              key={i}
              className="border border-line rounded overflow-hidden bg-white"
            >
              <div className="text-[10px] text-fog-500 px-2 py-1 bg-ink-200/60 border-b border-line">
                page {i + 1}/{images.length}
              </div>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={src} alt={`page ${i + 1}`} className="w-full block" />
            </div>
          ))}
        </div>
      )}
    </EventRow>
  )
}

/** Compact one-row event in the activity timeline.
 *  Left: status indicator (green dot when done, spinner when in-flight, red ×
 *  on error). Middle: bold verb + target + small subtext. Right: optional
 *  action button. Below: optional expandable details. */
function EventRow({
  status = 'done',
  icon,
  verb,
  target,
  verbColor = 'text-fog-300',
  badge,
  subtext,
  action,
  children,
  onToggle,
  open,
}: {
  status?: 'done' | 'running' | 'error'
  icon?: React.ReactNode
  verb: string
  target?: React.ReactNode
  verbColor?: string
  badge?: React.ReactNode
  subtext?: React.ReactNode
  action?: React.ReactNode
  children?: React.ReactNode
  // When provided, the whole header (verb + target + subtext) becomes a single
  // clickable control that calls onToggle; a chevron reflects `open`.
  onToggle?: () => void
  open?: boolean
}) {
  const header = (
    <div className="flex items-center gap-2 flex-wrap">
      {icon && <span className="text-fog-500">{icon}</span>}
      <span className={`font-medium ${verbColor}`}>{verb}</span>
      {target && (
        <code className="font-mono text-fog-50 text-[13px] truncate">
          {target}
        </code>
      )}
      {badge}
      {onToggle ? (
        <span className="ml-auto text-fog-500 group-hover:text-fog-300 text-[11px]">
          {open ? '▾' : '▸'}
        </span>
      ) : (
        action && <span className="ml-auto">{action}</span>
      )}
    </div>
  )
  const sub = subtext && (
    <div className="text-[11px] text-fog-500 mt-0.5">{subtext}</div>
  )
  return (
    <div className="flex gap-3 text-[13px] group animate-float-up">
      {/* Status indicator + connecting line */}
      <div className="flex flex-col items-center pt-1.5 shrink-0">
        <StatusDot status={status} />
      </div>
      <div className="flex-1 min-w-0">
        {onToggle ? (
          <button
            type="button"
            onClick={onToggle}
            className="w-full text-left -mx-1 px-1 py-0.5 rounded cursor-pointer hover:bg-soft/[0.05]"
          >
            {header}
            {sub}
          </button>
        ) : (
          <>
            {header}
            {sub}
          </>
        )}
        {children && <div className="mt-2">{children}</div>}
      </div>
    </div>
  )
}

function StatusDot({ status }: { status: 'done' | 'running' | 'error' }) {
  if (status === 'running') {
    return (
      <span className="relative w-2 h-2">
        <span className="absolute inset-0 rounded-full bg-sky-300 animate-ping" />
        <span className="relative w-2 h-2 rounded-full bg-sky-300 inline-block" />
      </span>
    )
  }
  if (status === 'error') {
    return <span className="w-2 h-2 rounded-full bg-rose-400" />
  }
  return <span className="w-2 h-2 rounded-full bg-emerald-400" />
}

function ShellEventRow({
  content,
  cmdArg,
}: {
  content: string
  cmdArg?: string
}) {
  const cmdMatch = content.match(/^\$ ([\s\S]*?)(?=\n\(exit |\n--- |$)/)
  const exitMatch = content.match(/\(exit (\d+), (\d+) ms\)/)
  const stdoutMatch = content.match(
    /--- stdout ---\n([\s\S]*?)(?=\n--- stderr ---|$)/,
  )
  const stderrMatch = content.match(/--- stderr ---\n([\s\S]*)$/)
  const noOutput = /\(no output\)/.test(content)

  const cmd = (cmdMatch?.[1] ?? cmdArg ?? '').trim()
  const exitCode = exitMatch ? Number(exitMatch[1]) : null
  const duration = exitMatch ? Number(exitMatch[2]) : null
  const stdout = (stdoutMatch?.[1] ?? '').replace(/\n+$/, '')
  const stderr = (stderrMatch?.[1] ?? '').replace(/\n+$/, '')
  const hasOutput = stdout || stderr || noOutput
  const stdoutLines = stdout ? stdout.split('\n').length : 0

  const [open, setOpen] = useState(false)
  const status: 'done' | 'error' =
    exitCode != null && exitCode !== 0 ? 'error' : 'done'

  const subtext = (
    <span className="font-mono">
      {exitCode != null && (
        <span className={exitCode === 0 ? 'text-fog-500' : 'text-rose-300'}>
          exit {exitCode}
        </span>
      )}
      {duration != null && <span className="text-fog-500"> · {duration} ms</span>}
      {stdoutLines > 0 && (
        <span className="text-fog-500"> · {stdoutLines} line{stdoutLines === 1 ? '' : 's'} of output</span>
      )}
    </span>
  )

  return (
    <EventRow
      status={status}
      icon={<TerminalIcon />}
      verb="Run"
      target={cmd}
      subtext={subtext}
      onToggle={hasOutput ? () => setOpen((v) => !v) : undefined}
      open={open}
    >
      {open && hasOutput && (
        <div className="rounded border border-line bg-ink-100/60 px-3 py-2 font-mono text-[12px] max-h-72 overflow-y-auto whitespace-pre-wrap">
          {stdout && <span className="text-fog-200">{stdout}</span>}
          {stdout && stderr && '\n'}
          {stderr && <span className="text-rose-300/80">{stderr}</span>}
          {!stdout && !stderr && noOutput && (
            <span className="text-fog-500 italic">(no output)</span>
          )}
        </div>
      )}
    </EventRow>
  )
}

function WriteFileEventRow({
  content,
  isError,
  sessionId,
  filenameArg,
}: {
  content: string
  isError: boolean
  sessionId: string | null
  filenameArg?: string
}) {
  const [diffOpen, setDiffOpen] = useState(false)
  const [diff, setDiff] = useState<string | null>(null)
  const [diffLoading, setDiffLoading] = useState(false)
  const [diffError, setDiffError] = useState<string | null>(null)
  // Fallback "view file" state for writes that weren't committed (no sha → no
  // diff). We fetch the file's current content from storage on demand.
  const [fileOpen, setFileOpen] = useState(false)
  const [fileBody, setFileBody] = useState<string | null>(null)
  const [fileLoading, setFileLoading] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)

  if (isError) return <ToolErrorRow verb="Edit" content={content} />

  // Capture the full path up to the *terminal* period (the one before a space
  // or end of string), not the first dot — otherwise "foo.txt." truncates to
  // "foo" and the filename loses its extension.
  const sizeMatch = content.match(/^Wrote (\d+) bytes to (.+?)\.(?:\s|$)/)
  const commitMatch = content.match(
    /committed as Agent: (created|updated) (.+?) \(([a-f0-9]+)\)/,
  )
  const bytes = sizeMatch ? Number(sizeMatch[1]) : 0
  const fullPath = sizeMatch?.[2] ?? ''
  const filename =
    (fullPath.split('/').pop() ?? fullPath) || filenameArg || '(unnamed)'
  const verb = commitMatch?.[1] // 'created' | 'updated' | undefined
  const sha = commitMatch?.[3] ?? ''
  const verbLabel = verb === 'created' ? 'Create' : 'Edit'

  async function toggleDiff() {
    if (diffOpen) {
      setDiffOpen(false)
      return
    }
    setDiffOpen(true)
    if (diff !== null || !sha || !sessionId) return
    setDiffLoading(true)
    setDiffError(null)
    try {
      const r = await authFetch(
        `/api/sessions/${sessionId}/commits/${sha}/diff`,
      )
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setDiffError(detail)
        return
      }
      const body = await r.json()
      setDiff(body.diff ?? '')
    } catch (e: any) {
      setDiffError(e?.message ?? 'network error')
    } finally {
      setDiffLoading(false)
    }
  }

  // Loads a red/green diff for files with no git commit (chat-session S3
  // files). Prefers the stored unified diff of the last write; if none exists
  // (e.g. a brand-new file), falls back to showing the current content as an
  // all-green "added" diff. Either way the result is rendered via SplitDiffBlock.
  async function toggleFile() {
    if (fileOpen) {
      setFileOpen(false)
      return
    }
    setFileOpen(true)
    if (fileBody !== null || !sessionId || !filename) return
    setFileLoading(true)
    setFileError(null)
    try {
      const dr = await authFetch(
        `/api/sessions/${sessionId}/files/${encodeURIComponent(filename)}/diff`,
      )
      if (dr.ok) {
        const body = await dr.json()
        if (body?.diff) {
          setFileBody(body.diff)
          return
        }
      }
      // No recorded diff — show current content as all additions (green).
      const r = await authFetch(
        `/api/sessions/${sessionId}/files/${encodeURIComponent(filename)}`,
      )
      if (!r.ok) {
        let detail = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) detail = body.detail
        } catch {}
        setFileError(detail)
        return
      }
      const body = await r.json()
      const content =
        (body?.content ?? '') + (body?.truncated ? '\n[... truncated ...]' : '')
      const lines = content.split('\n')
      setFileBody(
        `@@ -0,0 +1,${lines.length} @@\n` + lines.map((l: string) => '+' + l).join('\n'),
      )
    } catch (e: any) {
      setFileError(e?.message ?? 'network error')
    } finally {
      setFileLoading(false)
    }
  }

  const summary = (() => {
    if (!diff) return null
    let added = 0
    let removed = 0
    for (const line of diff.split('\n')) {
      if (line.startsWith('+') && !line.startsWith('+++')) added++
      else if (line.startsWith('-') && !line.startsWith('---')) removed++
    }
    return { added, removed }
  })()

  const subtext = (
    <span className="font-mono">
      {summary
        ? (
          <>
            {summary.added > 0 && (
              <span className="text-emerald-300">
                +{summary.added} line{summary.added === 1 ? '' : 's'}
              </span>
            )}
            {summary.added > 0 && summary.removed > 0 && (
              <span className="text-fog-500"> · </span>
            )}
            {summary.removed > 0 && (
              <span className="text-rose-300">
                −{summary.removed} line{summary.removed === 1 ? '' : 's'}
              </span>
            )}
            {sha && <span className="text-fog-500"> · {sha}</span>}
          </>
        )
        : (
          <>
            {verb === 'created' ? 'Added' : 'Modified'}
            {bytes ? <span className="text-fog-500"> · {bytes} bytes</span> : null}
            {sha && <span className="text-fog-500"> · {sha}</span>}
          </>
        )}
    </span>
  )

  return (
    <EventRow
      icon={<FileEditIcon />}
      verb={verbLabel}
      target={filename}
      subtext={subtext}
      onToggle={
        sha && sessionId
          ? toggleDiff
          : sessionId && filename !== '(unnamed)'
            ? toggleFile
            : undefined
      }
      open={diffOpen || fileOpen}
    >
      {diffOpen && (
        <div>
          {diffLoading && (
            <div className="text-[12px] text-fog-400 px-1">Loading diff…</div>
          )}
          {diffError && (
            <div className="text-[12px] text-rose-300 px-1">
              Failed to load diff: {diffError}
            </div>
          )}
          {diff !== null && !diffLoading && !diffError && (
            <SplitDiffBlock diff={diff} />
          )}
        </div>
      )}
      {fileOpen && (
        <div>
          {fileLoading && (
            <div className="text-[12px] text-fog-400 px-1">Loading file…</div>
          )}
          {fileError && (
            <div className="text-[12px] text-rose-300 px-1">
              Failed to load file: {fileError}
            </div>
          )}
          {fileBody !== null && !fileLoading && !fileError && (
            <SplitDiffBlock diff={fileBody} />
          )}
        </div>
      )}
    </EventRow>
  )
}

/** Side-by-side diff renderer.
 *
 *  Parses a unified diff into per-hunk rows. Each row pairs a "before" line
 *  (with deletions shown in red) and an "after" line (with additions in
 *  green). Pure-context lines appear on both sides; sequences of pure
 *  deletes get paired with the corresponding adds when possible. */
function SplitDiffBlock({ diff }: { diff: string }) {
  type Row = {
    left: string | null
    right: string | null
    leftNo: number | null
    rightNo: number | null
    kind: 'context' | 'change' | 'hunk'
  }
  const rows: Row[] = []

  let dels: { text: string; no: number }[] = []
  let adds: { text: string; no: number }[] = []
  let oldLine = 0
  let newLine = 0
  const flush = () => {
    const n = Math.max(dels.length, adds.length)
    for (let i = 0; i < n; i++) {
      const d = dels[i]
      const a = adds[i]
      rows.push({
        left: d ? d.text : null,
        right: a ? a.text : null,
        leftNo: d ? d.no : null,
        rightNo: a ? a.no : null,
        kind: 'change',
      })
    }
    dels = []
    adds = []
  }

  for (const raw of diff.split('\n')) {
    if (raw.startsWith('@@')) {
      flush()
      const m = raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/)
      if (m) {
        oldLine = parseInt(m[1], 10)
        newLine = parseInt(m[2], 10)
      }
      rows.push({ left: raw, right: raw, leftNo: null, rightNo: null, kind: 'hunk' })
      continue
    }
    if (
      raw.startsWith('diff --git') ||
      raw.startsWith('index ') ||
      raw.startsWith('+++') ||
      raw.startsWith('---') ||
      raw.startsWith('new file mode') ||
      raw.startsWith('deleted file mode')
    ) {
      flush()
      continue
    }
    if (raw.startsWith('+')) {
      adds.push({ text: raw.slice(1), no: newLine++ })
    } else if (raw.startsWith('-')) {
      dels.push({ text: raw.slice(1), no: oldLine++ })
    } else {
      flush()
      rows.push({
        left: raw.slice(1),
        right: raw.slice(1),
        leftNo: oldLine++,
        rightNo: newLine++,
        kind: 'context',
      })
    }
  }
  flush()

  // Diagonal hatch fills the "no line on this side" padding cells.
  const HATCH: React.CSSProperties = {
    backgroundImage:
      'repeating-linear-gradient(45deg, rgba(255,255,255,0.04) 0, rgba(255,255,255,0.04) 1px, transparent 1px, transparent 7px)',
  }
  const Cell = (
    text: string | null,
    no: number | null,
    kind: Row['kind'],
    side: 'left' | 'right',
  ) => {
    const empty = text == null
    const changed = kind === 'change' && !empty
    const hunk = kind === 'hunk'
    const bg = changed ? (side === 'left' ? 'bg-rose-500/20' : 'bg-emerald-500/20') : ''
    // Keep the code text bright/legible regardless of the line colour; only the
    // background and the +/- sign carry the red/green meaning.
    const textColor = hunk ? 'text-sky-300' : changed ? 'text-fog-50' : 'text-fog-200'
    const signColor = side === 'left' ? 'text-rose-300' : 'text-emerald-300'
    const sign = changed ? (side === 'left' ? '-' : '+') : ''
    return (
      <div className={`flex items-start ${bg}`} style={empty ? HATCH : undefined}>
        <span className="select-none shrink-0 w-9 pr-2 text-right text-fog-500 tabular-nums">
          {no ?? ''}
        </span>
        <span className={`select-none shrink-0 w-3 text-center ${signColor}`}>{sign}</span>
        <span className={`pr-3 whitespace-pre overflow-x-auto ${textColor}`}>
          {empty ? '' : text || ' '}
        </span>
      </div>
    )
  }

  return (
    <div className="rounded border border-line bg-ink-100 overflow-hidden">
      <div className="grid grid-cols-2 divide-x divide-line font-mono text-[12px] leading-[1.55] max-h-80 overflow-y-auto">
        <div>
          {rows.map((r, i) => (
            <div key={i}>{Cell(r.left, r.leftNo, r.kind, 'left')}</div>
          ))}
        </div>
        <div>
          {rows.map((r, i) => (
            <div key={i}>{Cell(r.right, r.rightNo, r.kind, 'right')}</div>
          ))}
        </div>
      </div>
    </div>
  )
}

// Old DiffBlock kept for backwards compat (HistoryPanel / DiffPane in
// TaskView still call it). Wraps the split view.
function DiffBlock({ diff }: { diff: string }) {
  return <SplitDiffBlock diff={diff} />
}

function ReadFileEventRow({
  content,
  isError,
  filenameArg,
}: {
  content: string
  isError: boolean
  filenameArg?: string
}) {
  const [open, setOpen] = useState(false)
  if (isError) return <ToolErrorRow verb="Read" content={content} />
  const bytes = new Blob([content]).size
  const lines = content.split('\n').length
  return (
    <EventRow
      icon={<ReadIcon />}
      verb="Read"
      target={filenameArg || '(unnamed)'}
      subtext={
        <span className="font-mono">
          {lines} line{lines === 1 ? '' : 's'}
          <span className="text-fog-500"> · {bytes} bytes</span>
        </span>
      }
      onToggle={content ? () => setOpen((v) => !v) : undefined}
      open={open}
    >
      {open && content && (
        <pre className="rounded border border-line bg-ink-100/60 px-3 py-2 font-mono text-[12px] text-fog-200 max-h-96 overflow-auto whitespace-pre-wrap">
          {content}
        </pre>
      )}
    </EventRow>
  )
}

function ListFilesEventRow({
  content,
  isError,
}: {
  content: string
  isError: boolean
}) {
  if (isError) return <ToolErrorRow verb="List" content={content} />
  const lines = content.split('\n')
  const fileLines = lines.filter((l) => /^- /.test(l.trim()))
  const [open, setOpen] = useState(false)
  return (
    <EventRow
      icon={<ListIcon />}
      verb="List"
      target="project files"
      subtext={
        <span className="font-mono">
          {fileLines.length} file{fileLines.length === 1 ? '' : 's'}
        </span>
      }
      action={
        fileLines.length > 0 ? (
          <button
            onClick={() => setOpen((v) => !v)}
            className="text-[11px] text-fog-400 hover:text-fog-50"
          >
            {open ? 'Hide' : 'Show'}
          </button>
        ) : undefined
      }
    >
      {open && (
        <pre className="font-mono text-[12px] text-fog-200 rounded border border-line bg-ink-100/60 px-3 py-2 whitespace-pre-wrap max-h-64 overflow-y-auto">
          {content}
        </pre>
      )}
    </EventRow>
  )
}

function ToolErrorRow({
  verb,
  content,
}: {
  verb: string
  content: string
}) {
  return (
    <EventRow
      status="error"
      icon={<ErrorIcon />}
      verb={verb}
      verbColor="text-rose-300"
      subtext={
        <span className="font-mono text-rose-300/80 whitespace-pre-wrap">
          {content}
        </span>
      }
    />
  )
}

/** Tool whose result hasn't landed yet — shown with a spinner. */
// Tool names contributed by plugins (backend/plugins_catalog.py). Used to badge
// their calls as "Plugin" in the activity timeline.
const PLUGIN_TOOL_NAMES = new Set([
  'fetch_url',
  'json_tool',
  'convert_units',
  'text_transform',
  'generate_uuid',
  'datetime_tool',
  'regex_test',
  'system_info',
  'list_directory',
  'review_python',
])

function InflightToolRow({ tool }: { tool: InflightTool }) {
  const args = tool.args || {}
  const name = tool.tool_name
  let verb = name
  let target: string | undefined
  let icon: React.ReactNode = <ToolIcon />
  let subtext: React.ReactNode = (
    <span className="text-fog-500 italic">running…</span>
  )
  if (name === 'run_shell') {
    verb = 'Run'
    target = String(args.cmd ?? '')
    icon = <TerminalIcon />
  } else if (name === 'write_project_file') {
    verb = 'Edit'
    target = String(args.filename ?? '')
    icon = <FileEditIcon />
  } else if (name === 'read_project_file') {
    verb = 'Read'
    target = String(args.filename ?? '')
    icon = <ReadIcon />
  } else if (name === 'list_project_files') {
    verb = 'List'
    target = 'project files'
    icon = <ListIcon />
  } else if (name === 'read_skill') {
    verb = 'Skill'
    target = String(args.skill_name ?? '')
    icon = <SkillRunIcon />
    subtext = <span className="text-fog-500 italic">loading…</span>
  } else if (name === 'skill_triggered') {
    verb = 'Skill'
    target = String(args.skill_name ?? '')
    icon = <SkillRunIcon />
    subtext = <span className="text-fog-500 italic">applied</span>
  } else if (PLUGIN_TOOL_NAMES.has(name)) {
    verb = 'Plugin'
    target = name
    icon = <PluginRunIcon />
  }
  return (
    <EventRow
      status="running"
      icon={icon}
      verb={verb}
      target={target}
      subtext={subtext}
    />
  )
}

function SkillRunIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-accent">
      <path d="M12 3l2.4 5.6L20 10l-4.5 3.9L17 20l-5-3-5 3 1.5-6.1L4 10l5.6-1.4z" />
    </svg>
  )
}
function PluginRunIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-accent">
      <path d="M12 2a3 3 0 0 1 3 3v2h2a2 2 0 0 1 2 2v3a3 3 0 0 1 0 6 2 2 0 0 1-2 2h-3v-2a3 3 0 0 0-6 0v2H5a2 2 0 0 1-2-2v-3a3 3 0 0 0 0-6V9a2 2 0 0 1 2-2h2V5a3 3 0 0 1 3-3z" />
    </svg>
  )
}

function DetailsBlock({ content }: { content: string }) {
  return (
    <pre className="font-mono text-[12px] text-fog-200 whitespace-pre-wrap rounded border border-line bg-ink-100/60 px-3 py-2 max-h-64 overflow-y-auto">
      {content}
    </pre>
  )
}

function TerminalIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-fog-400">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  )
}
function FileEditIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-fog-400">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
      <path d="m18 14-3 3v3h3l3-3-3-3z" />
    </svg>
  )
}
function ReadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-fog-400">
      <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" />
      <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
    </svg>
  )
}
function ListIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-fog-400">
      <line x1="8" y1="6" x2="21" y2="6" />
      <line x1="8" y1="12" x2="21" y2="12" />
      <line x1="8" y1="18" x2="21" y2="18" />
      <line x1="3" y1="6" x2="3.01" y2="6" />
      <line x1="3" y1="12" x2="3.01" y2="12" />
      <line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  )
}
function ToolIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
    </svg>
  )
}
function ErrorIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-rose-300">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  )
}

/* ─────────────────────────── modal ─────────────────────────── */

type GithubMode = 'none' | 'new_repo' | 'link_existing'
type ModalGithub =
  | { mode: 'none' }
  | { mode: 'new_repo'; repoName: string; private: boolean }
  | { mode: 'link_existing'; owner: string; repo: string; branch?: string }

function NewProjectModal({
  githubUsername,
  onOpenGithub,
  onClose,
  onCreate,
}: {
  githubUsername: string | null
  onOpenGithub: () => void
  onClose: () => void
  onCreate: (
    name: string,
    files: File[],
    github: ModalGithub,
  ) => void | Promise<void>
}) {
  const [name, setName] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)

  // GitHub linkage
  const [ghMode, setGhMode] = useState<GithubMode>('none')
  const [ghRepoName, setGhRepoName] = useState('')
  const [ghPrivate, setGhPrivate] = useState(true)
  const [ghOwner, setGhOwner] = useState('')
  const [ghRepo, setGhRepo] = useState('')
  const [ghBranch, setGhBranch] = useState('')
  const ghConnected = Boolean(githubUsername)

  // Slugified placeholder for the "new repo" name input — only used when the
  // user hasn't typed one. Spaces → hyphens, strip illegal chars.
  const slugDefault = name
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')

  useEffect(() => {
    nameRef.current?.focus()
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function buildGithub(): ModalGithub {
    if (ghMode === 'new_repo') {
      return {
        mode: 'new_repo',
        repoName: (ghRepoName.trim() || slugDefault) || 'project',
        private: ghPrivate,
      }
    }
    if (ghMode === 'link_existing') {
      return {
        mode: 'link_existing',
        owner: ghOwner.trim(),
        repo: ghRepo.trim(),
        branch: ghBranch.trim() || undefined,
      }
    }
    return { mode: 'none' }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || submitting) return
    if (ghMode !== 'none' && !ghConnected) return
    if (ghMode === 'link_existing' && (!ghOwner.trim() || !ghRepo.trim())) return
    setSubmitting(true)
    await onCreate(name.trim(), files, buildGithub())
  }

  function onPickFiles(e: React.ChangeEvent<HTMLInputElement>) {
    const list = e.target.files
    if (!list) return
    setFiles(Array.from(list))
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center px-4 bg-black/85 backdrop-blur-md animate-float-up"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="relative w-full max-w-md rounded-2xl border border-lineStrong bg-ink-200 shadow-2xl shadow-black/80 p-6">
        <div className="flex items-start justify-between mb-1">
          <div>
            <h2 className="serif text-2xl tracking-tighter text-fog-50">
              New project
            </h2>
            <p className="text-xs text-fog-400 mt-0.5">
              Group related threads. They'll share context.
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-soft/[0.06] text-fog-400 hover:text-fog-50 flex items-center justify-center transition"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <form onSubmit={submit} className="mt-5 space-y-4">
          <label className="block">
            <span className="block text-xs text-fog-400 mb-1.5">
              Project name
            </span>
            <input
              ref={nameRef}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Auth migration"
              required
              className="w-full bg-ink-200 border border-line focus:border-lineStrong rounded-lg px-3 py-2.5 text-[15px] outline-none transition placeholder:text-fog-500"
            />
          </label>

          <div>
            <span className="block text-xs text-fog-400 mb-1.5">
              Upload folder <span className="text-fog-500">(optional)</span>
            </span>
            <label className="block cursor-pointer">
              <input
                type="file"
                onChange={onPickFiles}
                multiple
                // @ts-ignore — webkitdirectory isn't in standard React types
                webkitdirectory=""
                directory=""
                className="hidden"
              />
              <div className="flex items-center gap-3 rounded-lg border border-dashed border-line hover:border-lineStrong bg-ink-200/40 px-4 py-5 text-center justify-center transition">
                <IconFolder className="text-fog-300" />
                <div className="text-sm">
                  <div className="text-fog-100">
                    {files.length === 0
                      ? 'Choose a folder'
                      : `${files.length} file${files.length === 1 ? '' : 's'} selected`}
                  </div>
                  <div className="text-[11px] text-fog-500 mt-0.5">
                    File names are stored as project context only
                  </div>
                </div>
              </div>
            </label>

            {files.length > 0 && (
              <div className="mt-2 max-h-28 overflow-y-auto rounded-md border border-line bg-ink-200/40 p-2">
                <div className="flex flex-wrap gap-1.5">
                  {files.slice(0, 30).map((f, i) => (
                    <span
                      key={i}
                      className="chip text-[11px] py-0.5"
                      title={(f as any).webkitRelativePath || f.name}
                    >
                      <IconFile />
                      {((f as any).webkitRelativePath || f.name)
                        .split('/')
                        .pop()}
                    </span>
                  ))}
                  {files.length > 30 && (
                    <span className="chip text-[11px] py-0.5">
                      +{files.length - 30} more
                    </span>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* GitHub linkage — optional rollback / history layer */}
          <div>
            <span className="block text-xs text-fog-400 mb-1.5">
              Save history to GitHub{' '}
              <span className="text-fog-500">(optional)</span>
            </span>
            <div className="grid grid-cols-3 gap-1.5 rounded-lg border border-line bg-ink-200/40 p-1">
              {(
                [
                  ['none', "Don't link"],
                  ['new_repo', 'Create new repo'],
                  ['link_existing', 'Use existing repo'],
                ] as [GithubMode, string][]
              ).map(([m, label]) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setGhMode(m)}
                  className={`text-[12px] py-1.5 rounded-md transition ${
                    ghMode === m
                      ? 'bg-soft/[0.10] text-fog-50'
                      : 'text-fog-300 hover:text-fog-50 hover:bg-soft/[0.04]'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {ghMode !== 'none' && !ghConnected && (
              <div className="mt-2 text-[11px] text-amber-300/90 flex items-center gap-2">
                <span>
                  Connect a GitHub PAT first to enable this option.
                </span>
                <button
                  type="button"
                  onClick={onOpenGithub}
                  className="underline underline-offset-2 hover:text-fog-50"
                >
                  Connect now
                </button>
              </div>
            )}

            {ghMode === 'new_repo' && ghConnected && (
              <div className="mt-2 space-y-2">
                <label className="block">
                  <span className="block text-[11px] text-fog-400 mb-1">
                    Repo name
                  </span>
                  <input
                    value={ghRepoName}
                    onChange={(e) => setGhRepoName(e.target.value)}
                    placeholder={slugDefault || 'project-name'}
                    className="w-full bg-ink-200 border border-line focus:border-lineStrong rounded-md px-2.5 py-1.5 text-[13px] outline-none transition placeholder:text-fog-500"
                  />
                  <span className="block text-[10px] text-fog-500 mt-0.5">
                    Will be created under{' '}
                    <code className="font-mono">{githubUsername}/</code>
                    {ghRepoName.trim() || slugDefault || 'project-name'}
                  </span>
                </label>
                <label className="flex items-center gap-2 text-[12px] text-fog-200 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={ghPrivate}
                    onChange={(e) => setGhPrivate(e.target.checked)}
                    className="accent-white"
                  />
                  Private repo
                </label>
              </div>
            )}

            {ghMode === 'link_existing' && ghConnected && (
              <div className="mt-2 grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="block text-[11px] text-fog-400 mb-1">
                    Owner
                  </span>
                  <input
                    value={ghOwner}
                    onChange={(e) => setGhOwner(e.target.value)}
                    placeholder={githubUsername || 'octocat'}
                    className="w-full bg-ink-200 border border-line focus:border-lineStrong rounded-md px-2.5 py-1.5 text-[13px] outline-none transition placeholder:text-fog-500"
                  />
                </label>
                <label className="block">
                  <span className="block text-[11px] text-fog-400 mb-1">
                    Repo
                  </span>
                  <input
                    value={ghRepo}
                    onChange={(e) => setGhRepo(e.target.value)}
                    placeholder="hello-world"
                    className="w-full bg-ink-200 border border-line focus:border-lineStrong rounded-md px-2.5 py-1.5 text-[13px] outline-none transition placeholder:text-fog-500"
                  />
                </label>
                <label className="block col-span-2">
                  <span className="block text-[11px] text-fog-400 mb-1">
                    Branch{' '}
                    <span className="text-fog-500">(defaults to main)</span>
                  </span>
                  <input
                    value={ghBranch}
                    onChange={(e) => setGhBranch(e.target.value)}
                    placeholder="main"
                    className="w-full bg-ink-200 border border-line focus:border-lineStrong rounded-md px-2.5 py-1.5 text-[13px] outline-none transition placeholder:text-fog-500"
                  />
                </label>
              </div>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="btn-ghost text-sm"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={
                !name.trim() ||
                submitting ||
                (ghMode !== 'none' && !ghConnected) ||
                (ghMode === 'link_existing' &&
                  (!ghOwner.trim() || !ghRepo.trim()))
              }
              className="btn-white text-sm disabled:opacity-50"
            >
              {submitting ? 'Creating…' : 'Create project'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

/* ─────────────────────────── project landing pane ─────────────────────────── */

function ProjectLanding({
  session,
  threads,
  files,
  threadNames,
  sessionName,
  onOpenThread,
  onOpenFile,
  onNewThread,
}: {
  session: Session
  threads: Thread[]
  files: string[]
  threadNames: Record<string, string>
  sessionName: string
  onOpenThread: (tid: string) => void
  onOpenFile: (name: string) => void
  onNewThread: () => void
}) {
  const mode = session.mode || 'auto'
  return (
    <div className="animate-float-up">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="min-w-0">
          <div className="text-[11px] uppercase tracking-widest text-fog-400 mb-1 inline-flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-sm bg-amber-500/80" />
            Project
          </div>
          <h1 className="serif text-3xl tracking-tighter text-fog-50 truncate">
            {sessionName}
          </h1>
          <div className="flex items-center gap-2 mt-2 text-[11px] text-fog-400">
            <span
              className={`chip ${
                mode === 'confirm'
                  ? 'border-amber-500/40 text-amber-300'
                  : ''
              }`}
            >
              {mode === 'confirm' ? 'Confirm mode' : 'Auto mode'}
            </span>
            <span className="chip">
              {threads.length} {threads.length === 1 ? 'thread' : 'threads'}
            </span>
            <span className="chip">
              {files.length} {files.length === 1 ? 'file' : 'files'}
            </span>
          </div>
        </div>
        <button
          onClick={onNewThread}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent text-ink-50 text-sm font-medium hover:bg-accent/90 transition"
        >
          <PlusIcon small />
          New thread
        </button>
      </div>

      {/* Files */}
      <section className="mb-6">
        <h2 className="text-[11px] uppercase tracking-widest text-fog-400 mb-2">
          Files
        </h2>
        {files.length === 0 ? (
          <p className="text-sm text-fog-500 px-2">
            No files yet. Upload one from a thread or have the agent write some.
          </p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {files.map((f) => {
              const display = f.split('/').pop() || f
              return (
                <button
                  key={f}
                  onClick={() => onOpenFile(display)}
                  className="surface flex items-center gap-2.5 px-3 py-2 text-left hover:bg-soft/[0.03] transition"
                  title={`Open ${display}`}
                >
                  <IconFile />
                  <span className="text-[13px] text-fog-100 truncate flex-1">
                    {display}
                  </span>
                </button>
              )
            })}
          </div>
        )}
      </section>

      {/* Threads */}
      <section>
        <h2 className="text-[11px] uppercase tracking-widest text-fog-400 mb-2">
          Threads
        </h2>
        {threads.length === 0 ? (
          <p className="text-sm text-fog-500 px-2">
            No threads yet. Start one with the button above.
          </p>
        ) : (
          <div className="space-y-1">
            {threads.map((t) => (
              <button
                key={t.id}
                onClick={() => onOpenThread(t.id)}
                className="w-full surface flex items-center gap-3 px-3 py-2.5 text-left hover:bg-soft/[0.03] transition"
              >
                <IconChat className="shrink-0 text-fog-400" small />
                <span className="flex-1 truncate text-[14px] text-fog-100">
                  {threadNames[t.id] || t.name || 'New chat'}
                </span>
                {!!t.tokens && (
                  <span
                    className="text-[10px] text-fog-500 tabular-nums"
                    title={`${t.tokens.toLocaleString()} tokens`}
                  >
                    {fmtTokens(t.tokens)}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

/* ─────────────────────────── context ring button ─────────────────────────── */

function ContextRingButton({
  summary,
  onClick,
}: {
  summary: { total: number; limit: number; percent: number } | null
  onClick: () => void
}) {
  const pct = summary?.percent ?? 0
  // Clamp so an unexpectedly-large total never wraps the arc.
  const clamped = Math.max(0, Math.min(100, pct))
  // Threshold colors — green → amber → orange → red as usage climbs.
  const color =
    clamped < 50
      ? '#22c55e'
      : clamped < 80
        ? '#f59e0b'
        : clamped < 95
          ? '#f97316'
          : '#ef4444'
  // SVG arc geometry. r=15 gives ~94 circumference at strokeWidth=3, which
  // leaves enough room for the percentage label inside a 40px button.
  const r = 15
  const c = 2 * Math.PI * r
  const dash = (clamped / 100) * c
  const offset = c - dash
  const hasData = summary != null && summary.limit > 0
  const label = hasData ? `${Math.round(clamped)}%` : '—'
  const title = hasData
    ? `${summary!.total.toLocaleString()} / ${summary!.limit.toLocaleString()} tokens (${clamped.toFixed(1)}%)`
    : 'Context window'
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      className="fixed right-5 top-1/2 -translate-y-1/2 z-30 w-11 h-11 rounded-full bg-ink-200 border border-lineStrong shadow-xl shadow-black/40 flex items-center justify-center transition hover:bg-soft/[0.06]"
    >
      <svg width="40" height="40" viewBox="0 0 40 40" className="-rotate-90">
        {/* track */}
        <circle
          cx="20"
          cy="20"
          r={r}
          fill="none"
          stroke="currentColor"
          strokeOpacity="0.18"
          strokeWidth="3"
          className="text-fog-300"
        />
        {/* progress arc */}
        {hasData && (
          <circle
            cx="20"
            cy="20"
            r={r}
            fill="none"
            stroke={color}
            strokeWidth="3"
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={offset}
            style={{ transition: 'stroke-dashoffset 0.4s ease, stroke 0.3s' }}
          />
        )}
      </svg>
      <span
        className={`absolute font-mono tabular-nums leading-none ${
          hasData ? 'text-fog-100' : 'text-fog-400'
        }`}
        style={{ fontSize: clamped >= 100 ? 9 : 10 }}
      >
        {label}
      </span>
    </button>
  )
}

/* ─────────────────────────── theme toggle ─────────────────────────── */

function ThemeToggle() {
  const [theme, setTheme] = useTheme()
  const dark = theme === 'dark'
  return (
    <button
      onClick={() => setTheme(dark ? 'light' : 'dark')}
      title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
      aria-label="Toggle color theme"
      className="w-7 h-7 rounded-full text-fog-300 hover:text-fog-50 hover:bg-soft/[0.08] flex items-center justify-center transition"
    >
      {dark ? (
        // moon
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      ) : (
        // sun
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      )}
    </button>
  )
}

/* ─────────────────────────── icons ─────────────────────────── */

function Glyph() {
  return (
    <span className="relative w-6 h-6 inline-flex items-center justify-center">
      <span className="absolute inset-0 rounded-md bg-soft/10" />
      <span className="relative w-2 h-2 rounded-sm bg-accent" />
    </span>
  )
}
function PlusIcon({ small }: { small?: boolean } = {}) {
  const s = small ? 11 : 14
  return (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none">
      <path
        d="M12 5v14M5 12h14"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  )
}
function Caret({
  open,
  className = '',
}: {
  open: boolean
  className?: string
}) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      className={`transition-transform ${open ? 'rotate-90' : ''} ${className}`}
    >
      <path
        d="M9 6l6 6-6 6"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function ArrowUp() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <path
        d="M12 19V5M5 12l7-7 7 7"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconFolder({
  className = '',
  small,
}: {
  className?: string
  small?: boolean
} = {}) {
  const s = small ? 13 : 16
  return (
    <svg
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
    >
      <path
        d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconChat({
  className = '',
  small,
}: {
  className?: string
  small?: boolean
} = {}) {
  const s = small ? 13 : 16
  return (
    <svg
      width={s}
      height={s}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
    >
      <path
        d="M5 5h14a2 2 0 012 2v8a2 2 0 01-2 2h-7l-4 3v-3H5a2 2 0 01-2-2V7a2 2 0 012-2z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconSkill() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
      <path
        d="M12 3l2.4 5.6L20 10l-4.5 3.9L17 20l-5-3-5 3 1.5-6.1L4 10l5.6-1.4z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}
function IconPlugin() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2a3 3 0 0 1 3 3v2h2a2 2 0 0 1 2 2v3a3 3 0 0 1 0 6 2 2 0 0 1-2 2h-3v-2a3 3 0 0 0-6 0v2H5a2 2 0 0 1-2-2v-3a3 3 0 0 0 0-6V9a2 2 0 0 1 2-2h2V5a3 3 0 0 1 3-3z" />
    </svg>
  )
}
function IconFile() {
  return (
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
      <path
        d="M6 3h8l4 4v14a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M14 3v4h4" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}
function IconImage() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
      <rect
        x="3"
        y="4"
        width="18"
        height="16"
        rx="2"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <circle cx="9" cy="10" r="1.6" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M4 17l5-5 4 4 3-3 4 4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function DotsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
      <circle cx="5" cy="12" r="1.7" />
      <circle cx="12" cy="12" r="1.7" />
      <circle cx="19" cy="12" r="1.7" />
    </svg>
  )
}
function IconPencil() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
      <path
        d="M4 20l4-1 11-11-3-3L5 16l-1 4z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconShare() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
      <path
        d="M4 12v6a2 2 0 002 2h12a2 2 0 002-2v-6M16 6l-4-4-4 4M12 2v14"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
function IconTrash() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none">
      <path
        d="M4 7h16M9 7V4h6v3M6 7l1 13a2 2 0 002 2h6a2 2 0 002-2l1-13"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/* ─────────────────────────── row menu + rename ─────────────────────────── */

type MenuItem = {
  label: string
  icon?: ReactNode
  danger?: boolean
  onClick: () => void
}

function RowMenu({ items }: { items: MenuItem[] }) {
  return (
    <div
      data-row-menu="1"
      className="absolute right-1 top-full mt-1 z-50 w-48 rounded-xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60"
      onMouseDown={(e) => e.stopPropagation()}
    >
      {items.map((item) => (
        <button
          key={item.label}
          onClick={(e) => {
            e.stopPropagation()
            item.onClick()
          }}
          className={`w-full text-left px-3 py-2 rounded-md flex items-center gap-2.5 ${
            item.danger
              ? 'text-red-300 hover:bg-red-500/10'
              : 'text-fog-100 hover:bg-soft/[0.06]'
          }`}
        >
          {item.icon && <span className="text-fog-300">{item.icon}</span>}
          {item.label}
        </button>
      ))}
    </div>
  )
}

function RenameInput({
  value,
  onChange,
  onCommit,
  onCancel,
  icon,
}: {
  value: string
  onChange: (v: string) => void
  onCommit: () => void
  onCancel: () => void
  icon?: ReactNode
}) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])
  return (
    <div className="flex-1 px-2.5 py-1 flex items-center gap-2.5">
      {icon}
      <input
        ref={ref}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onCommit()
          } else if (e.key === 'Escape') {
            e.preventDefault()
            onCancel()
          }
        }}
        onBlur={onCommit}
        className="flex-1 min-w-0 bg-ink-300 border border-lineStrong rounded px-2 py-1 text-[13px] outline-none"
      />
    </div>
  )
}

/* ─────────────────────────── confirm / prompt dialogs ─────────────────────────── */

function ModalShell({
  onClose,
  children,
}: {
  onClose: () => void
  children: ReactNode
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 backdrop-blur-sm animate-float-up"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(92vw,420px)] rounded-2xl border border-lineStrong bg-ink-200 p-6 shadow-2xl shadow-black/60"
      >
        {children}
      </div>
    </div>
  )
}

function ConfirmDialog({
  title,
  message,
  confirmLabel = 'Confirm',
  danger,
  onCancel,
  onConfirm,
}: {
  title: string
  message: string
  confirmLabel?: string
  danger?: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <ModalShell onClose={onCancel}>
      <h3 className="text-base font-medium text-fog-50 mb-2">{title}</h3>
      <p className="text-sm text-fog-300 leading-relaxed mb-5">{message}</p>
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-soft/[0.06]"
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          autoFocus
          className={`px-4 py-2 rounded-lg text-sm font-medium ${
            danger
              ? 'bg-red-500/90 hover:bg-red-500 text-fog-50'
              : 'bg-accent text-ink-50 hover:bg-soft/90'
          }`}
        >
          {confirmLabel}
        </button>
      </div>
    </ModalShell>
  )
}

function PromptDialog({
  title,
  message,
  placeholder,
  initialValue = '',
  confirmLabel = 'OK',
  onCancel,
  onConfirm,
}: {
  title: string
  message?: string
  placeholder?: string
  initialValue?: string
  confirmLabel?: string
  onCancel: () => void
  onConfirm: (value: string) => void
}) {
  const [value, setValue] = useState(initialValue)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])
  return (
    <ModalShell onClose={onCancel}>
      <h3 className="text-base font-medium text-fog-50 mb-2">{title}</h3>
      {message && (
        <p className="text-sm text-fog-300 leading-relaxed mb-3">{message}</p>
      )}
      <input
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            if (value.trim()) onConfirm(value)
          }
        }}
        placeholder={placeholder}
        className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-fog-50 outline-none focus:border-lineStrong mb-5"
      />
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-soft/[0.06]"
        >
          Cancel
        </button>
        <button
          onClick={() => onConfirm(value)}
          disabled={!value.trim()}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-accent text-ink-50 hover:bg-soft/90 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {confirmLabel}
        </button>
      </div>
    </ModalShell>
  )
}

/* ─────────────────────────── file viewer ─────────────────────────── */

function FileViewer({
  name,
  content,
  loading,
  error,
  onClose,
}: {
  name: string
  content: string
  loading: boolean
  error: string | null
  onClose: () => void
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const lines = content ? content.split('\n') : []
  const padWidth = String(Math.max(lines.length, 1)).length

  async function copyAll() {
    try {
      await navigator.clipboard.writeText(content)
    } catch {
      // best-effort
    }
  }

  return (
    <div
      className="fixed inset-0 z-[70] flex justify-end bg-black/40"
      onClick={onClose}
    >
      <aside
        onClick={(e) => e.stopPropagation()}
        className="w-[min(720px,90vw)] h-full bg-ink-200 border-l border-lineStrong flex flex-col shadow-2xl shadow-black/60"
      >
        <header className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="flex items-center gap-2 min-w-0">
            <IconFile />
            <span className="text-sm text-fog-50 truncate">{name}</span>
            {!loading && !error && (
              <span className="text-[11px] text-fog-500 shrink-0 ml-2">
                {lines.length} line{lines.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={copyAll}
              disabled={loading || !!error}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              Copy
            </button>
            <button
              onClick={onClose}
              className="w-7 h-7 rounded hover:bg-soft/[0.06] text-fog-300 flex items-center justify-center"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-auto">
          {loading && (
            <div className="p-6 text-sm text-fog-400">Loading…</div>
          )}
          {error && (
            <div className="p-6 text-sm text-red-400">Error: {error}</div>
          )}
          {!loading && !error && (
            <pre className="m-0 text-[12.5px] leading-[1.55] font-mono">
              {lines.map((line, i) => (
                <div key={i} className="flex">
                  <span
                    className="select-none pl-3 pr-3 text-right text-fog-500 tabular-nums shrink-0"
                    style={{ minWidth: `${padWidth + 2}ch` }}
                  >
                    {i + 1}
                  </span>
                  <span className="whitespace-pre text-fog-100 pr-4">
                    {line || ' '}
                  </span>
                </div>
              ))}
            </pre>
          )}
        </div>
      </aside>
    </div>
  )
}

/* ─────────────────────────── GitHub connect ─────────────────────────── */

/* ─────────────────────────── workspace history panel ───────────────────────── */

function HistoryPanel({
  commits,
  loading,
  error,
  revertingId,
  onClose,
  onRefresh,
  onRevert,
}: {
  commits: WorkspaceCommit[] | null
  loading: boolean
  error: string | null
  revertingId: number | null
  onClose: () => void
  onRefresh: () => void
  onRevert: (id: number, message: string) => void
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-[70] flex justify-end bg-black/40"
      onClick={onClose}
    >
      <aside
        onClick={(e) => e.stopPropagation()}
        className="w-[min(560px,90vw)] h-full bg-ink-200 border-l border-lineStrong flex flex-col shadow-2xl shadow-black/60"
      >
        <header className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-sm text-fog-50 font-medium">Workspace history</div>
          <div className="flex items-center gap-1">
            <button
              onClick={onRefresh}
              disabled={loading}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              {loading ? 'Loading…' : 'Refresh'}
            </button>
            <button
              onClick={onClose}
              className="w-7 h-7 rounded hover:bg-soft/[0.06] text-fog-300 flex items-center justify-center"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-3">
          {loading && (
            <div className="text-sm text-fog-400 px-2 py-4">Loading history…</div>
          )}
          {error && (
            <div className="text-sm text-red-300 px-2 py-4">
              Failed to load history: {error}
            </div>
          )}
          {!loading && !error && commits && commits.length === 0 && (
            <div className="text-sm text-fog-400 px-2 py-4">
              No commits yet. The agent's file changes will show up here as they
              happen.
            </div>
          )}
          {!loading && !error && commits && commits.length > 0 && (
            <ul className="space-y-1.5">
              {commits.map((c) => (
                <li
                  key={c.id}
                  className="rounded-md border border-line bg-ink-200/40 hover:border-lineStrong px-3 py-2.5 transition"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[11px] text-fog-500">
                        <code className="font-mono">{c.sha.slice(0, 7)}</code>
                        <span>{new Date(c.created_at).toLocaleString()}</span>
                        <StatusBadge status={c.status} />
                      </div>
                      <div className="text-[13px] text-fog-100 mt-1 break-words">
                        {c.message}
                      </div>
                    </div>
                    <button
                      onClick={() => onRevert(c.id, c.message)}
                      disabled={
                        c.status === 'reverted' ||
                        revertingId !== null ||
                        c.message === 'Initial commit'
                      }
                      className="shrink-0 text-[11px] px-2 py-1 rounded border border-line text-fog-200 hover:bg-soft/[0.06] hover:text-fog-50 disabled:opacity-30 disabled:cursor-not-allowed transition"
                      title={
                        c.status === 'reverted'
                          ? 'Already reverted'
                          : c.message === 'Initial commit'
                            ? 'Cannot undo the initial commit'
                            : 'Undo this change'
                      }
                    >
                      {revertingId === c.id ? 'Undoing…' : 'Undo'}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer className="px-4 py-2.5 border-t border-line text-[11px] text-fog-500">
          Commits sync at the end of every chat turn. Click Refresh after a
          manual git operation.
        </footer>
      </aside>
    </div>
  )
}

function StatusBadge({ status }: { status: WorkspaceCommit['status'] }) {
  const styles =
    status === 'reverted'
      ? 'text-rose-300/90 bg-rose-300/10 border-rose-300/20'
      : status === 'pushed'
        ? 'text-emerald-300/90 bg-emerald-300/10 border-emerald-300/20'
        : 'text-fog-400 bg-soft/[0.04] border-line'
  return (
    <span
      className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded border ${styles}`}
    >
      {status}
    </span>
  )
}

/* ─────────────────────────── context window viewer ─────────────────────────── */

function ContextViewer({
  data,
  loading,
  error,
  onClose,
  onRefresh,
  onSoftRefresh,
  onDelete,
  sessionId,
  threadId,
}: {
  data: any
  loading: boolean
  error: string | null
  onClose: () => void
  onRefresh: () => void
  onSoftRefresh: () => void
  onDelete: (messageId: number) => void
  sessionId: string | null
  threadId: string | null
}) {
  const [systemOpen, setSystemOpen] = useState(false)
  const [rawMode, setRawMode] = useState(false)

  const rawText = useMemo(() => {
    if (!data) return ''
    const parts: string[] = []
    if (data.system_prompt) {
      parts.push(`=== SYSTEM PROMPT ===\n${data.system_prompt}`)
    }
    for (const m of (data.messages || []) as any[]) {
      const label = m.tool_name ? `${m.role}:${m.tool_name}` : m.role
      const tc =
        m.tool_calls && m.tool_calls.length > 0
          ? `\n[tool_calls] ${JSON.stringify(m.tool_calls)}`
          : ''
      parts.push(
        `--- ${String(label).toUpperCase()} (${m.tokens} tokens) ---\n${m.content || ''}${tc}`,
      )
    }
    return parts.join('\n\n')
  }, [data])

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function fmtTokenCount(n: number): string {
    if (n < 1000) return String(n)
    return `${(n / 1000).toFixed(1)}k`
  }

  const pct = data?.percent_used ?? 0
  const barColor =
    pct > 75 ? 'bg-red-500' : pct > 50 ? 'bg-yellow-500' : 'bg-emerald-500'

  return (
    <div
      className="fixed inset-0 z-[70] flex justify-end bg-black/40"
      onClick={onClose}
    >
      <aside
        onClick={(e) => e.stopPropagation()}
        className="w-[min(640px,92vw)] h-full bg-ink-200 border-l border-lineStrong flex flex-col shadow-2xl shadow-black/60"
      >
        <header className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-sm text-fog-50 font-medium">Context window</div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setRawMode((v) => !v)}
              disabled={loading || !data}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-soft/[0.06] disabled:opacity-40"
              title="See system prompt + every message as one block"
            >
              {rawMode ? 'Summary' : 'View raw'}
            </button>
            <button
              onClick={onRefresh}
              disabled={loading}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              Refresh
            </button>
            <button
              onClick={onClose}
              className="w-7 h-7 rounded hover:bg-soft/[0.06] text-fog-300 flex items-center justify-center"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-auto px-5 py-4 space-y-5 text-sm">
          {loading && <div className="text-fog-400">Loading…</div>}
          {error && <div className="text-red-400">Error: {error}</div>}

          {!loading && !error && data && rawMode && (
            <section>
              <div className="flex items-center justify-between mb-2">
                <div className="text-[11px] uppercase tracking-widest text-fog-500">
                  Full context · {data.total_tokens.toLocaleString()} tokens
                </div>
                <button
                  onClick={() => {
                    if (typeof navigator !== 'undefined' && navigator.clipboard) {
                      navigator.clipboard.writeText(rawText)
                    }
                  }}
                  className="text-[11px] px-2 py-0.5 rounded text-fog-300 hover:bg-soft/[0.06]"
                >
                  Copy
                </button>
              </div>
              <pre className="text-[12px] leading-relaxed text-fog-100 whitespace-pre-wrap bg-ink-300 border border-line rounded-md p-3 max-h-[75vh] overflow-auto font-mono">
                {rawText}
              </pre>
            </section>
          )}

          {!loading && !error && data && !rawMode && (
            <>
              {/* Model + token usage */}
              <section>
                <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-2">
                  Model
                </div>
                <div className="text-fog-100 font-mono text-[12.5px] mb-3">
                  {data.model}
                </div>
                <div className="flex items-baseline justify-between mb-1.5">
                  <span className="text-fog-300">
                    {data.total_tokens.toLocaleString()} /{' '}
                    {data.context_limit.toLocaleString()} tokens
                  </span>
                  <span className="text-fog-400 text-xs">
                    {pct.toFixed(1)}%
                  </span>
                </div>
                <div className="h-2 rounded-full bg-soft/[0.06] overflow-hidden">
                  <div
                    className={`h-full ${barColor}`}
                    style={{ width: `${Math.min(100, pct)}%` }}
                  />
                </div>
                {/* Image-recall caching — prompt tokens served from the
                    provider cache so far. Only shown when caching is active
                    (the cache / cache+evict techniques on a cache-capable
                    provider report cache_read_tokens > 0). */}
                {(Number(data.cache_read_tokens) || 0) > 0 && (
                  <div className="mt-2 flex items-baseline justify-between text-[12px] bg-sky-500/8 border border-sky-500/25 rounded-md px-3 py-1.5">
                    <span className="text-fog-400 uppercase tracking-widest text-[10px]">
                      ⚡ Prompt cache
                    </span>
                    <span className="font-mono text-sky-300">
                      {Number(data.cache_read_tokens).toLocaleString()} tokens reused
                      {data.total_tokens
                        ? ` · ${((Number(data.cache_read_tokens) / data.total_tokens) * 100).toFixed(0)}%`
                        : ''}
                    </span>
                  </div>
                )}
              </section>

              {/* Per-turn token trajectory (PR #0) */}
              <TrajectorySection
                trajectory={data.trajectory || []}
                fmtTokenCount={fmtTokenCount}
              />

              {/* Applied context edits (PR #0) — empty until a
                  context-management strategy actually fires. */}
              <AppliedEditsSection
                edits={data.applied_edits || []}
                fmtTokenCount={fmtTokenCount}
              />

              {/* Task-aware relevance cleanup — suggest & remove finished /
                  unrelated episodes. Suggest-only; refreshes context on apply. */}
              <RelevanceCleanupSection
                sessionId={sessionId}
                threadId={threadId}
                model={data.model}
                onApplied={onSoftRefresh}
              />

              {/* System prompt */}
              <section>
                <button
                  onClick={() => setSystemOpen((v) => !v)}
                  className="w-full flex items-center justify-between text-[11px] uppercase tracking-widest text-fog-500 mb-2 hover:text-fog-300"
                >
                  <span>
                    System prompt · {fmtTokenCount(data.system_tokens)} tokens
                  </span>
                  <span>{systemOpen ? '▾' : '▸'}</span>
                </button>
                {systemOpen && (
                  <pre className="text-[12px] leading-relaxed text-fog-200 whitespace-pre-wrap bg-ink-300 border border-line rounded-md p-3 max-h-72 overflow-auto">
                    {data.system_prompt}
                  </pre>
                )}
              </section>

              {/* Messages — grouped by user turn. Top level shows the user
                  prompt + the turn's token total; expand to see and remove the
                  assistant/tool messages it produced. */}
              <section>
                {(() => {
                  const turns = groupTurns(data.messages || [])
                  return (
                    <>
                      <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-2">
                        Conversation · {turns.length} turn{turns.length === 1 ? '' : 's'}
                      </div>
                      {turns.length === 0 && (
                        <p className="text-fog-500 text-xs">No messages yet.</p>
                      )}
                      <div className="space-y-1.5">
                        {turns.map((g, i) => (
                          <MessageTurnGroup
                            key={g.user?.id ?? `turn-${i}`}
                            group={g}
                            onDelete={onDelete}
                          />
                        ))}
                      </div>
                    </>
                  )
                })()}
              </section>

              {/* Files */}
              {data.files.length > 0 && (
                <section>
                  <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-2">
                    Project files · {data.files.length}
                  </div>
                  <div className="space-y-0.5">
                    {data.files.map((f: any) => (
                      <div
                        key={f.name}
                        className="flex items-center justify-between text-[12.5px] py-1 px-2 rounded hover:bg-soft/[0.03]"
                      >
                        <span className="text-fog-200 truncate">{f.name}</span>
                        <span className="text-fog-500 text-[11px] shrink-0 ml-2">
                          {f.size} B
                        </span>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

/* ────────── PR #0: trajectory sparkline + applied-edits list ──────────
 *
 * Both panels read straight off the `/context` payload — no extra
 * round trip. They stay visible (with an empty-state hint) when no
 * data is present yet, so a user opening the panel before any
 * context-management strategy has fired still sees where these views
 * live. Future strategy PRs (B1/B2/B6/B4) populate them by calling
 * `_record_context_event(...)` on the backend.
 */

type TrajectoryPoint = {
  turn: number
  input_tokens: number
  output_tokens: number
  turn_tokens: number
  cumulative_tokens: number
  cache_read_tokens?: number
  messages: number
}

type AppliedEdit = {
  id: number
  turn: number
  type: string
  freed_tokens: number
  details: any
  at: string | null
}

const EDIT_TYPE_LABEL: Record<string, { label: string; icon: string }> = {
  tool_result_trimming: { label: 'Tool-result trimming', icon: '↺' },
  summarization: { label: 'Summarisation', icon: '📝' },
  sliding_window: { label: 'Sliding window', icon: '✂️' },
  image_eviction: { label: 'Image eviction', icon: '🖼️' },
  subagent_call: { label: 'Sub-agent dispatch', icon: '🧠' },
  memory_write: { label: 'Memory write', icon: '💾' },
  memory_read: { label: 'Memory read', icon: '🗂️' },
}

function TrajectorySection({
  trajectory,
  fmtTokenCount,
}: {
  trajectory: TrajectoryPoint[]
  fmtTokenCount: (n: number) => string
}) {
  // Geometry. Width grows linearly with the turn count so very long
  // conversations still get a useful sparkline; height stays fixed.
  const W = Math.max(220, trajectory.length * 24)
  const H = 60
  const PAD = 4

  const max = Math.max(1, ...trajectory.map((t) => t.cumulative_tokens))
  const points = trajectory.map((t, i) => {
    const x =
      trajectory.length === 1
        ? W / 2
        : PAD + (i * (W - PAD * 2)) / (trajectory.length - 1)
    const y = H - PAD - ((H - PAD * 2) * t.cumulative_tokens) / max
    return { x, y, t }
  })
  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
    .join(' ')
  const areaPath =
    points.length > 0
      ? `${path} L ${points[points.length - 1].x.toFixed(1)} ${H - PAD} L ${
          points[0].x.toFixed(1)
        } ${H - PAD} Z`
      : ''

  const lastTurn = trajectory[trajectory.length - 1]
  const firstTurn = trajectory[0]

  return (
    <section>
      <div className="flex items-baseline justify-between mb-2">
        <span className="text-[11px] uppercase tracking-widest text-fog-500">
          Per-turn trajectory · {trajectory.length} turn
          {trajectory.length === 1 ? '' : 's'}
        </span>
        {lastTurn && (
          <span className="text-[11px] text-fog-400">
            peak {fmtTokenCount(lastTurn.cumulative_tokens)}
          </span>
        )}
      </div>
      {trajectory.length === 0 ? (
        <div className="text-[12px] text-fog-500 bg-ink-300 border border-line rounded-md p-3">
          No turns yet — send a message to start the trajectory.
        </div>
      ) : (
        <div className="bg-ink-300 border border-line rounded-md p-2 overflow-x-auto">
          <svg
            width={W}
            height={H}
            viewBox={`0 0 ${W} ${H}`}
            className="block"
            preserveAspectRatio="none"
          >
            {areaPath && (
              <path d={areaPath} fill="currentColor" className="text-emerald-500/15" />
            )}
            <path
              d={path}
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              className="text-emerald-400"
            />
            {points.map((p, i) => (
              <g key={i}>
                <circle
                  cx={p.x}
                  cy={p.y}
                  r={2.5}
                  className="fill-emerald-400"
                />
                <title>
                  {`Turn ${p.t.turn} · +${p.t.turn_tokens} tok (cum ${p.t.cumulative_tokens})${(p.t.cache_read_tokens ?? 0) > 0 ? ` · ⚡${p.t.cache_read_tokens} cached` : ''}`}
                </title>
              </g>
            ))}
          </svg>
          <div className="flex justify-between text-[10px] text-fog-500 px-1 mt-1">
            <span>
              turn {firstTurn?.turn} · {fmtTokenCount(firstTurn?.turn_tokens ?? 0)}
            </span>
            <span>
              turn {lastTurn?.turn} · +{fmtTokenCount(lastTurn?.turn_tokens ?? 0)} this turn
            </span>
          </div>
        </div>
      )}
    </section>
  )
}

function AppliedEditsSection({
  edits,
  fmtTokenCount,
}: {
  edits: AppliedEdit[]
  fmtTokenCount: (n: number) => string
}) {
  // Collapsed by default — expand to inspect what fired this thread.
  const [open, setOpen] = useState(false)
  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-fog-500 mb-2 hover:text-fog-300"
        title={open ? 'Collapse' : 'Expand'}
      >
        <span className="shrink-0">{open ? '▾' : '▸'}</span>
        <span>Applied context edits · {edits.length}</span>
      </button>
      {!open ? null : edits.length === 0 ? (
        <div className="text-[12px] text-fog-500 bg-ink-300 border border-line rounded-md p-3">
          Nothing has been trimmed, summarised, or delegated yet. When a
          context-management strategy fires (e.g. tool-result trimming
          or summarisation), the action shows up here.
        </div>
      ) : (
        <div className="space-y-1.5">
          {edits.map((e) => {
            const meta = EDIT_TYPE_LABEL[e.type] || { label: e.type, icon: '•' }
            const detail =
              typeof e.details === 'string'
                ? e.details
                : e.details && typeof e.details === 'object'
                  ? (e.details.note ||
                      e.details.summary ||
                      Object.keys(e.details).join(', '))
                  : null
            return (
              <div
                key={e.id}
                className="text-[12px] bg-ink-300 border border-line rounded-md px-3 py-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-fog-100">
                    <span className="mr-1.5">{meta.icon}</span>
                    {meta.label}
                  </span>
                  <span className="text-fog-500 text-[10px] uppercase tracking-widest">
                    turn {e.turn}
                  </span>
                </div>
                <div className="text-fog-400 text-[11px] mt-0.5">
                  {e.freed_tokens > 0
                    ? `freed ${fmtTokenCount(e.freed_tokens)} tokens`
                    : 'no tokens freed'}
                  {detail ? ` · ${String(detail).slice(0, 80)}` : ''}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// A conversation turn: the user prompt plus the assistant/tool messages it
// produced. Messages before the first user prompt fall into a leading group.
type Turn = { user: any | null; children: any[] }

function groupTurns(messages: any[]): Turn[] {
  const groups: Turn[] = []
  let cur: Turn | null = null
  for (const m of messages) {
    if (m.role === 'user') {
      if (cur) groups.push(cur)
      cur = { user: m, children: [] }
    } else {
      if (!cur) cur = { user: null, children: [] }
      cur.children.push(m)
    }
  }
  if (cur) groups.push(cur)
  return groups
}

// Each message's OWN size (not the per-call total, which includes all prior
// history). Assistant turns use their completion tokens; everything else is
// estimated from content length.
function ownTokens(m: any): number {
  if (!m) return 0
  if (m.role === 'assistant' && Number(m.output_tokens) > 0) {
    return Number(m.output_tokens)
  }
  return Math.ceil((String(m.content || '').length) / 4)
}

function turnTokens(g: Turn): number {
  return ownTokens(g.user) + g.children.reduce((acc, m) => acc + ownTokens(m), 0)
}

function MessageTurnGroup({
  group,
  onDelete,
}: {
  group: Turn
  onDelete?: (messageId: number) => void
}) {
  const [open, setOpen] = useState(false)
  const preview = String(group.user?.content || '')
    .slice(0, 90)
    .replace(/\n/g, ' ')
  const childCount = group.children.length
  const total = turnTokens(group)
  return (
    <div className="rounded-md border border-line bg-ink-300/40">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-soft/[0.03]"
      >
        <span className="text-fog-500 text-[11px] shrink-0">
          {open ? '▾' : '▸'}
        </span>
        <span className="text-[10px] uppercase tracking-wider font-mono text-blue-300 shrink-0">
          {group.user ? 'user' : 'context'}
        </span>
        <span className="text-fog-200 text-[12px] truncate flex-1">
          {preview || (group.user ? '(empty)' : 'leading messages')}
        </span>
        <span className="text-fog-500 text-[10px] shrink-0">
          {childCount} msg
        </span>
        <span className="text-fog-400 text-[11px] shrink-0 tabular-nums">
          {total.toLocaleString()} tok
        </span>
      </button>
      {open && (
        <div className="px-2 pb-2 space-y-1">
          {group.user && (
            <ContextMessageRow msg={group.user} onDelete={onDelete} />
          )}
          {group.children.map((m: any, i: number) => (
            <ContextMessageRow key={m.id ?? i} msg={m} onDelete={onDelete} />
          ))}
        </div>
      )}
    </div>
  )
}

function ContextMessageRow({
  msg,
  onDelete,
}: {
  msg: any
  onDelete?: (messageId: number) => void
}) {
  const [open, setOpen] = useState(false)
  const [tokensOpen, setTokensOpen] = useState(false)
  const preview = (msg.content || '').slice(0, 100).replace(/\n/g, ' ')
  const roleColor =
    msg.role === 'user'
      ? 'text-blue-300'
      : msg.role === 'assistant'
      ? 'text-emerald-300'
      : 'text-purple-300'

  const totalTok = Number(msg.tokens || 0)
  const inputTok = Number(msg.input_tokens || 0)
  const outputTok = Number(msg.output_tokens || 0)
  const thinkingTok = Number(msg.thinking_tokens || 0)
  const answerTok = Math.max(0, outputTok - thinkingTok)
  const hasBreakdown =
    msg.role === 'assistant' && (inputTok > 0 || outputTok > 0)

  const canDelete = onDelete && typeof msg.id === 'number'

  return (
    <div className="rounded-md border border-line bg-ink-300/40 group">
      <div className="w-full flex items-center gap-2 hover:bg-soft/[0.03]">
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-left px-3 py-2 flex items-center gap-2 flex-1 min-w-0"
        >
          <span
            className={`text-[10px] uppercase tracking-wider font-mono shrink-0 ${roleColor}`}
          >
            {msg.role}
            {msg.tool_name ? `:${msg.tool_name}` : ''}
          </span>
          <span className="text-fog-300 text-[12px] truncate flex-1">
            {preview || (msg.tool_calls ? '(tool call)' : '(empty)')}
          </span>
          <span className="text-fog-500 text-[11px] shrink-0 tabular-nums">
            {msg.tokens}
          </span>
        </button>
        {canDelete && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onDelete!(msg.id)
            }}
            className="shrink-0 w-7 h-7 mr-1 rounded text-red-400 hover:text-red-300 hover:bg-red-500/10 flex items-center justify-center text-base"
            title={
              msg.has_langgraph_id === false
                ? 'Remove from view (older message — model may still recall it)'
                : 'Remove from context'
            }
            aria-label="Remove from context"
          >
            ✕
          </button>
        )}
      </div>
      {open && (
        <div className="px-3 pb-3 pt-1 text-[12px] text-fog-200">
          {hasBreakdown && (
            <div className="mb-2 border border-line rounded bg-ink-300/60">
              <button
                onClick={() => setTokensOpen((v) => !v)}
                className="w-full text-left px-2 py-1.5 flex items-center gap-2 hover:bg-soft/[0.03] text-[11px] text-fog-300"
              >
                <span className="text-fog-500 w-3 inline-block">
                  {tokensOpen ? '▾' : '▸'}
                </span>
                <span className="flex-1">Tokens</span>
                <span className="text-fog-500 tabular-nums">
                  {totalTok.toLocaleString()}
                </span>
              </button>
              {tokensOpen && (
                <div className="px-2 pb-2 pt-0.5 space-y-0.5 text-[11px]">
                  <div className="flex items-center justify-between text-fog-400">
                    <span>Input</span>
                    <span className="tabular-nums">
                      {inputTok.toLocaleString()} tokens
                    </span>
                  </div>
                  {thinkingTok > 0 && (
                    <div className="flex items-center justify-between text-fog-400">
                      <span className="pl-3">Thinking</span>
                      <span className="tabular-nums">
                        {thinkingTok.toLocaleString()} tokens
                      </span>
                    </div>
                  )}
                  <div className="flex items-center justify-between text-fog-400">
                    <span className={thinkingTok > 0 ? 'pl-3' : ''}>
                      {thinkingTok > 0 ? 'Answer' : 'Output'}
                    </span>
                    <span className="tabular-nums">
                      {answerTok.toLocaleString()} tokens
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-fog-300 pt-0.5 mt-0.5 border-t border-line">
                    <span>Total</span>
                    <span className="tabular-nums">
                      {totalTok.toLocaleString()} tokens
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}
          {msg.tool_calls && msg.tool_calls.length > 0 && (
            <pre className="whitespace-pre-wrap text-[11.5px] text-fog-400 bg-ink-300 border border-line rounded p-2 mb-2 max-h-48 overflow-auto">
              {JSON.stringify(msg.tool_calls, null, 2)}
            </pre>
          )}
          <pre className="whitespace-pre-wrap leading-relaxed">
            {msg.content || '(no content)'}
          </pre>
        </div>
      )}
    </div>
  )
}


function GithubConnectModal({
  username,
  onClose,
  onSave,
  onDisconnect,
}: {
  username: string | null
  onClose: () => void
  onSave: (token: string) => Promise<string | null>
  onDisconnect: () => Promise<void>
}) {
  const [token, setToken] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSave() {
    if (!token.trim()) return
    setSaving(true)
    setError(null)
    const err = await onSave(token.trim())
    setSaving(false)
    if (err) {
      setError(err)
    } else {
      setToken('')
      onClose()
    }
  }

  return (
    <ModalShell onClose={onClose}>
      <h3 className="text-base font-medium text-fog-50 mb-2">GitHub</h3>
      {username ? (
        <>
          <p className="text-sm text-fog-300 leading-relaxed mb-4">
            Connected as{' '}
            <span className="text-fog-50 font-medium">@{username}</span>. The
            agent can now read, push to, and revert files in repos you grant
            access to.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-soft/[0.06]"
            >
              Close
            </button>
            <button
              onClick={async () => {
                await onDisconnect()
                onClose()
              }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-red-500/90 hover:bg-red-500 text-fog-50"
            >
              Disconnect
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="text-sm text-fog-300 leading-relaxed mb-3">
            Paste a Personal Access Token (PAT). Generate one at{' '}
            <a
              href="https://github.com/settings/tokens"
              target="_blank"
              rel="noopener noreferrer"
              className="underline text-fog-100"
            >
              github.com/settings/tokens
            </a>{' '}
            with the <code className="text-fog-100">repo</code> scope.
          </p>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && token.trim()) handleSave()
            }}
            placeholder="ghp_..."
            className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-fog-50 outline-none focus:border-lineStrong mb-3 font-mono"
          />
          {error && (
            <p className="text-sm text-red-400 mb-3">{error}</p>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving || !token.trim()}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-accent text-ink-50 hover:bg-soft/90 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {saving ? 'Verifying…' : 'Connect'}
            </button>
          </div>
        </>
      )}
    </ModalShell>
  )
}
