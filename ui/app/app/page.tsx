'use client'

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { supabase, authFetch } from '@/lib/supabase'

/* ─────────────────────────── types ─────────────────────────── */

type Session = {
  id: string
  name: string
  created_at: string
  tokens?: number
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
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_name?: string
  tool_calls?: ToolCall[]
}
type Kind = 'project' | 'chat'

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

  const [newMenuOpen, setNewMenuOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [projectModalOpen, setProjectModalOpen] = useState(false)

  // Per-row "⋯" menu — keyed by `${kind}:${id}` so chats and threads don't collide
  const [rowMenuKey, setRowMenuKey] = useState<string | null>(null)
  // Inline rename state
  const [renameKey, setRenameKey] = useState<string | null>(null)
  const [renameValue, setRenameValue] = useState('')
  // Lightweight toast
  const [toast, setToast] = useState<string | null>(null)
  // Composer "+" attach menu
  const [composerMenuOpen, setComposerMenuOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
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
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setComposerMenuOpen(false)
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
    return threadNames[t.id] || 'New thread'
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

  async function deleteSession(sid: string) {
    if (!confirm('Delete this conversation? This cannot be undone.')) return
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

  function moveChatToProject(sid: string) {
    setRowMenuKey(null)
    const current = sessions.find((s) => s.id === sid)
    if (!current) return
    const projectName = window
      .prompt('Project name?', sessionDisplayName(current))
      ?.trim()
    if (!projectName) return

    // The chat's old display name becomes the thread's name (it had no thread name yet).
    const oldChatName = sessionDisplayName(current)
    const ts = threadsMap[sid] || []
    if (ts[0] && !threadNames[ts[0].id]) {
      setThreadName(ts[0].id, oldChatName)
    }

    saveKind(sid, 'project')
    setKinds((prev) => ({ ...prev, [sid]: 'project' }))
    setSessionName(sid, projectName)
    setExpanded((prev) => new Set(prev).add(sid))
    setToast('Moved to projects')
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
            !(m.role === 'assistant' && !m.content?.trim()),
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

  async function uploadFilesToSession(list: FileList | null) {
    if (!list || !list.length || !activeSession) return
    const sid = activeSession
    const fd = new FormData()
    for (const f of Array.from(list)) fd.append('files', f, f.name)

    setUploading(true)
    try {
      const r = await authFetch(`/api/sessions/${sid}/files`, {
        method: 'POST',
        body: fd,
      })
      if (!r.ok) {
        let msg = `${r.status} ${r.statusText}`
        try {
          const body = await r.json()
          if (body?.detail) msg = body.detail
        } catch {}
        setToast(`Upload failed: ${msg.slice(0, 60)}`)
        return
      }
      const data = await r.json()
      const names: string[] = (data?.saved || []).map((s: any) => s.name)
      const existing = filesMap[sid] || []
      const merged = Array.from(new Set([...existing, ...names]))
      setFilesMap((prev) => ({ ...prev, [sid]: merged }))
      saveFiles(sid, merged)
      setToast(
        `Uploaded ${names.length} file${names.length === 1 ? '' : 's'}`,
      )
    } catch (e: any) {
      setToast(`Upload failed: ${e?.message ?? 'network error'}`)
    } finally {
      setUploading(false)
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
  async function loadSessions() {
    const r = await authFetch('/api/sessions')
    if (!r.ok) return
    setSessions(await r.json())
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

  async function createProject(name: string, files: File[]) {
    const r = await authFetch('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
    if (!r.ok) return
    const data = await r.json()
    const s: Session = {
      id: data.id,
      name: data.name,
      created_at: data.created_at,
    }
    const defaultThread: Thread | undefined = data.default_thread

    saveKind(s.id, 'project')
    setKinds((prev) => ({ ...prev, [s.id]: 'project' }))

    const names = files.map((f) => (f as any).webkitRelativePath || f.name)
    if (names.length) {
      saveFiles(s.id, names)
      setFilesMap((prev) => ({ ...prev, [s.id]: names }))
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
      body: JSON.stringify({ name: 'New thread' }),
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
    }
    setExpanded(next)
  }

  async function selectThread(sid: string, tid: string) {
    setActiveSession(sid)
    setActiveThread(tid)
    setMessages([])
    await loadHistory(sid, tid)
  }

  async function selectChat(sid: string) {
    let threads = threadsMap[sid]
    if (!threads) threads = await loadThreads(sid)
    if (threads[0]) selectThread(sid, threads[0].id)
  }

  async function send() {
    if (!input.trim() || !activeSession || !activeThread || sending) return
    const sid = activeSession
    const tid = activeThread
    const msg = input.trim()
    const isFirstMessage = messages.length === 0
    setInput('')
    setSending(true)
    setMessages((prev) => [...prev, { role: 'user', content: msg }])

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
    authFetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, thread_id: tid, message: msg }),
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
        }
      })
      .catch((e: any) => {
        chatErrorDetail = e?.message ?? String(e)
      })

    const FAST_FAIL = new Set([400, 401, 403, 422, 429])
    const deadline = Date.now() + 180_000 // 3 min

    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500))

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
        <div className="px-4 py-3 flex items-center gap-2 border-b border-line">
          <Link href="/" className="flex items-center gap-2 group">
            <Glyph />
            <span className="text-sm tracking-tight font-medium">agent</span>
          </Link>
        </div>

        {/* + New dropdown */}
        <div className="px-3 pt-3 relative" ref={newBtnWrapRef}>
          <button
            onClick={() => setNewMenuOpen((v) => !v)}
            className="w-full flex items-center justify-between gap-2 bg-white text-black hover:bg-fog-50 transition px-3 py-2 rounded-lg text-sm font-medium"
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
                className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06] flex items-center gap-2.5"
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
                className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06] flex items-center gap-2.5"
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
            <span className="text-[11px] uppercase tracking-widest text-fog-400">
              Projects
            </span>
            <span className="text-[11px] text-fog-500">{projects.length}</span>
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
              return (
                <div key={s.id} className="mb-0.5">
                  <button
                    onClick={() => toggleSession(s.id)}
                    className="w-full px-2.5 py-1.5 text-left text-sm flex items-center gap-2 rounded-md hover:bg-white/[0.04] text-fog-100"
                  >
                    <Caret open={isOpen} className="text-fog-400" />
                    <IconFolder className="shrink-0 text-fog-300" small />
                    <span className="truncate flex-1">{s.name}</span>
                    {fileCount > 0 && (
                      <span className="text-[10px] text-fog-500">
                        {fileCount}
                      </span>
                    )}
                  </button>
                  {isOpen && (
                    <div className="pl-3 pt-0.5">
                      {threads.map((t) => {
                        const active = activeThread === t.id
                        return (
                          <button
                            key={t.id}
                            onClick={() => selectThread(s.id, t.id)}
                            className={`group w-full text-left px-2.5 py-1.5 rounded-md flex items-center gap-2.5 text-[13px] transition ${
                              active
                                ? 'bg-white/[0.07] text-white'
                                : 'text-fog-200 hover:bg-white/[0.03] hover:text-white'
                            }`}
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
                              <span className="text-[10px] text-fog-500 tabular-nums shrink-0">
                                {fmtTokens(t.tokens)}
                              </span>
                            )}
                          </button>
                        )
                      })}
                      <button
                        onClick={() => createThread(s.id)}
                        className="w-full text-left px-2.5 py-1.5 rounded-md text-[12px] text-fog-400 hover:text-white hover:bg-white/[0.03] flex items-center gap-2.5"
                      >
                        <PlusIcon small />
                        New thread
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {/* Chats */}
          <div className="px-4 mt-5 mb-1.5 flex items-center justify-between">
            <span className="text-[11px] uppercase tracking-widest text-fog-400">
              Chats
            </span>
            <span className="text-[11px] text-fog-500">{chats.length}</span>
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
                      ? 'bg-white/[0.07] text-white'
                      : 'text-fog-200 hover:bg-white/[0.03]'
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
                        className="flex-1 min-w-0 text-left px-2.5 py-1.5 flex items-center gap-2.5 hover:text-white"
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
                        className={`shrink-0 mr-1 w-6 h-6 rounded hover:bg-white/[0.08] text-fog-300 flex items-center justify-center ${
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

        {/* User menu */}
        <div className="border-t border-line p-3 relative">
          <button
            onClick={() => setUserMenuOpen((v) => !v)}
            className="w-full flex items-center gap-3 px-2 py-2 rounded-md hover:bg-white/[0.04]"
          >
            <span className="w-7 h-7 rounded-full bg-white/10 flex items-center justify-center text-xs">
              {(userEmail ?? '?').slice(0, 1).toUpperCase()}
            </span>
            <span className="text-sm truncate flex-1 text-left">{userEmail}</span>
            <span className="text-fog-400 text-xs">⋯</span>
          </button>
          {userMenuOpen && (
            <div className="absolute bottom-full left-3 right-3 mb-2 z-50 rounded-2xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60">
              <Link
                href="/"
                className="block px-3 py-2 rounded-md hover:bg-white/[0.06]"
              >
                Back to home
              </Link>
              <button
                onClick={signOut}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06]"
              >
                Sign out
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* ────── Main ────── */}
      <main className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <header className="h-12 px-5 border-b border-line flex items-center justify-between">
          <div className="text-sm flex items-center gap-2 min-w-0">
            {activeSessionObj && activeThreadObj ? (
              activeKind === 'project' ? (
                <>
                  <IconFolder small className="text-fog-400" />
                  <span className="text-fog-300 truncate">
                    {activeSessionObj.name}
                  </span>
                  <span className="text-fog-500">/</span>
                  <span className="text-white truncate">
                    {threadDisplayName(activeThreadObj)}
                  </span>
                </>
              ) : (
                <>
                  <IconChat small className="text-fog-400" />
                  <span className="text-white truncate">
                    {sessionDisplayName(activeSessionObj)}
                  </span>
                </>
              )
            ) : (
              <span className="text-fog-400">Select a project or chat</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span className="chip">
              <span className="dot bg-emerald-400" />
              glm-4.5-air
            </span>
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
                {activeFiles.slice(0, 12).map((f) => (
                  <span
                    key={f}
                    className="chip text-[11px] py-0.5"
                    title={f}
                  >
                    <IconFile />
                    {f.split('/').pop()}
                  </span>
                ))}
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
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl px-6 py-10">
            {!activeThread && <EmptyState onPick={(text) => setInput(text)} />}

            <div className="space-y-7">
              {messages
                .filter(
                  (m) =>
                    m.role !== 'tool' &&
                    !(m.role === 'assistant' && !m.content?.trim()),
                )
                .map((m, i) => (
                  <MessageBlock key={i} msg={m} />
                ))}
              {sending && <TypingIndicator />}
              <div ref={endRef} />
            </div>
          </div>
        </div>

        {/* Composer */}
        <footer className="border-t border-line">
          <div className="mx-auto max-w-3xl px-6 py-4">
            <form
              onSubmit={(e) => {
                e.preventDefault()
                send()
              }}
              className="rounded-3xl border border-line bg-ink-100/80 hover:border-lineStrong focus-within:border-lineStrong transition-colors px-3 py-2"
            >
              <div className="flex items-end gap-2">
                <div className="relative shrink-0" ref={composerMenuRef}>
                  <button
                    type="button"
                    onClick={() => setComposerMenuOpen((v) => !v)}
                    disabled={!activeSession || uploading}
                    className="w-9 h-9 rounded-full hover:bg-white/[0.06] text-fog-300 hover:text-white flex items-center justify-center transition disabled:opacity-30 disabled:cursor-not-allowed"
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
                        className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06] flex items-center gap-2.5"
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
                        className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06] flex items-center gap-2.5"
                      >
                        <IconImage />
                        <div>
                          <div className="text-fog-100">Upload photos</div>
                          <div className="text-[11px] text-fog-400">
                            Stored as project files
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
                      uploadFilesToSession(e.target.files)
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
                      uploadFilesToSession(e.target.files)
                      e.target.value = ''
                    }}
                  />
                </div>

                <textarea
                  ref={composerRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      send()
                    }
                  }}
                  disabled={!activeThread || sending}
                  rows={1}
                  placeholder={
                    activeThread
                      ? 'Reply to Agent…'
                      : 'Start a new chat or pick one from the sidebar'
                  }
                  className="flex-1 resize-none bg-transparent outline-none px-1 py-2 text-[15px] placeholder:text-fog-400 disabled:opacity-50 leading-6"
                />

                <button
                  type="submit"
                  disabled={!activeThread || sending || !input.trim()}
                  className="shrink-0 rounded-full bg-white text-black w-9 h-9 flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed hover:bg-fog-50 transition"
                  title="Send"
                >
                  {sending ? (
                    <span className="w-3 h-3 rounded-full border-2 border-black border-t-transparent animate-spin" />
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
      </main>

      {/* New project modal */}
      {projectModalOpen && (
        <NewProjectModal
          onClose={() => setProjectModalOpen(false)}
          onCreate={async (name, files) => {
            setProjectModalOpen(false)
            await createProject(name, files)
          }}
        />
      )}

      {/* Toast */}
      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] px-4 py-2 rounded-full bg-white text-black text-sm shadow-2xl animate-float-up">
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
      <div className="serif text-5xl tracking-tighter text-white mb-3">
        Where should we begin?
      </div>
      <p className="text-fog-300 text-base mb-10 max-w-md mx-auto">
        Start a new chat or open a project from the sidebar. Threads inside a
        project share context; chats stay isolated.
      </p>
      <div className="grid sm:grid-cols-2 gap-3 max-w-xl mx-auto text-left">
        {hints.map((h) => (
          <button
            key={h.title}
            onClick={() => onPick(h.prompt)}
            className="surface p-4 hover:bg-white/[0.03] transition cursor-pointer text-left"
          >
            <div className="text-sm text-white mb-1">{h.title}</div>
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

function TypingIndicator() {
  return (
    <div className="flex items-center gap-1.5 text-fog-400 px-1">
      <span className="w-1.5 h-1.5 rounded-full bg-fog-300 animate-bounce [animation-delay:-0.3s]" />
      <span className="w-1.5 h-1.5 rounded-full bg-fog-300 animate-bounce [animation-delay:-0.15s]" />
      <span className="w-1.5 h-1.5 rounded-full bg-fog-300 animate-bounce" />
      <span className="ml-2 text-xs">thinking</span>
    </div>
  )
}

function MessageBlock({ msg }: { msg: Message }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end animate-float-up">
        <div className="max-w-[80%] rounded-3xl bg-white/[0.06] border border-line text-fog-100 px-4 py-2.5 text-[15px] leading-7 whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    )
  }
  if (msg.role === 'tool') {
    return (
      <div className="text-xs font-mono text-fog-400">
        <div className="text-fog-500 mb-1">↳ {msg.tool_name}</div>
        <pre className="whitespace-pre-wrap rounded-lg border border-line bg-ink-100 p-3">
          {msg.content}
        </pre>
      </div>
    )
  }
  return (
    <div className="animate-float-up">
      <div className="flex items-center gap-2 mb-2 text-[11px] uppercase tracking-widest text-fog-400">
        <span className="w-5 h-5 rounded-full bg-white/10 flex items-center justify-center">
          <span className="w-1.5 h-1.5 bg-white rounded-sm" />
        </span>
        Agent
      </div>
      <div className="text-[15px] leading-7 text-fog-100 markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
      </div>
    </div>
  )
}

/* ─────────────────────────── modal ─────────────────────────── */

function NewProjectModal({
  onClose,
  onCreate,
}: {
  onClose: () => void
  onCreate: (name: string, files: File[]) => void | Promise<void>
}) {
  const [name, setName] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [submitting, setSubmitting] = useState(false)
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    nameRef.current?.focus()
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || submitting) return
    setSubmitting(true)
    await onCreate(name.trim(), files)
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
            <h2 className="serif text-2xl tracking-tighter text-white">
              New project
            </h2>
            <p className="text-xs text-fog-400 mt-0.5">
              Group related threads. They'll share context.
            </p>
          </div>
          <button
            onClick={onClose}
            className="w-8 h-8 rounded-full hover:bg-white/[0.06] text-fog-400 hover:text-white flex items-center justify-center transition"
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
              disabled={!name.trim() || submitting}
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

/* ─────────────────────────── icons ─────────────────────────── */

function Glyph() {
  return (
    <span className="relative w-6 h-6 inline-flex items-center justify-center">
      <span className="absolute inset-0 rounded-md bg-white/10" />
      <span className="relative w-2 h-2 rounded-sm bg-white" />
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
              : 'text-fog-100 hover:bg-white/[0.06]'
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
