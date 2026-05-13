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
  // Composer "+" attach menu
  const [composerMenuOpen, setComposerMenuOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  // Model picker
  const [models, setModels] = useState<
    { id: string; name: string; context_length: number }[]
  >([])
  const [selectedModel, setSelectedModel] = useState<string>('')
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
  async function loadSessions() {
    const r = await authFetch('/api/sessions')
    if (!r.ok) return
    setSessions(await r.json())
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
      setContextData(await r.json())
    } catch (e: any) {
      setContextError(e?.message ?? 'Network error')
    } finally {
      setContextLoading(false)
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

  async function createProject(name: string, files: File[]) {
    const r = await authFetch('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ name, kind: 'project' }),
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
    setActiveSession(sid)
    setActiveThread(tid)
    setMessages([])
    await loadHistory(sid, tid)
    // Sync file chips with backend reality (agent may have written files).
    refreshFiles(sid)
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
    const filesToUpload = pendingFiles
    const isFirstMessage = messages.length === 0
    setInput('')
    setPendingFiles([])
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
    authFetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        session_id: sid,
        thread_id: tid,
        message: msg,
        attached_files: attachedFiles,
        model: selectedModel || undefined,
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
                    <div className="group relative flex items-center rounded-md hover:bg-white/[0.04]">
                      <button
                        onClick={() => toggleSession(s.id)}
                        className="flex-1 min-w-0 px-2.5 py-1.5 text-left text-sm flex items-center gap-2 text-fog-100"
                      >
                        <Caret open={isOpen} className="text-fog-400" />
                        <IconFolder className="shrink-0 text-fog-300" small />
                        <span className="truncate flex-1">
                          {sessionDisplayName(s)}
                        </span>
                        {fileCount > 0 && (
                          <span className="text-[10px] text-fog-500">
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
                                className="w-full text-left px-2.5 py-1.5 rounded-md flex items-center gap-2.5 text-[13px] text-fog-200 hover:bg-white/[0.03] hover:text-white"
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
                                ? 'bg-white/[0.07] text-white'
                                : 'text-fog-200 hover:bg-white/[0.03]'
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
                                  className="flex-1 min-w-0 text-left px-2.5 py-1.5 flex items-center gap-2.5 hover:text-white"
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
                                  className={`shrink-0 mr-1 w-6 h-6 rounded hover:bg-white/[0.08] text-fog-300 flex items-center justify-center ${
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
                        className="w-full text-left px-2.5 py-1.5 rounded-md text-[12px] text-fog-400 hover:text-white hover:bg-white/[0.03] flex items-center gap-2.5"
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
                onClick={() => {
                  setUserMenuOpen(false)
                  setGithubModalOpen(true)
                }}
                className="w-full text-left px-3 py-2 rounded-md hover:bg-white/[0.06] flex items-center justify-between"
              >
                <span>GitHub</span>
                <span className="text-[11px] text-fog-400 truncate ml-2">
                  {githubUsername ? `@${githubUsername}` : 'Not connected'}
                </span>
              </button>
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
            {models.length > 0 ? (
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
                  className="bg-transparent text-xs text-white outline-none max-w-[14rem] truncate"
                  title="Chat model (OpenRouter free tier)"
                >
                  {models.map((m) => (
                    <option
                      key={m.id}
                      value={m.id}
                      className="bg-ink-200 text-white"
                    >
                      {m.name.replace(/\s*\(free\)\s*$/i, '')}
                    </option>
                  ))}
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
                      className="chip text-[11px] py-0.5 hover:bg-white/[0.08] cursor-pointer"
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
          <div className="mx-auto max-w-3xl px-6 py-10">
            {!activeThread && <EmptyState onPick={(text) => setInput(text)} />}

            <div className="space-y-7">
              {messages
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
                .map((m, i) => (
                  <MessageBlock key={i} msg={m} />
                ))}
              {sending && <TypingIndicator />}
              <div ref={endRef} />
            </div>
          </div>

          {/* Floating context-window button — right side, vertically centered */}
          {activeThread && (
            <button
              onClick={openContextViewer}
              className="fixed right-5 top-1/2 -translate-y-1/2 z-30 w-10 h-10 rounded-full bg-ink-200 border border-lineStrong text-fog-300 hover:text-white hover:bg-white/[0.06] shadow-xl shadow-black/40 flex items-center justify-center transition opacity-70 hover:opacity-100"
              title="View context window"
              aria-label="View context window"
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
                <circle cx="12" cy="12" r="9" />
                <path d="M12 7v5l3 2" />
              </svg>
            </button>
          )}
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
                        className="ml-1 w-4 h-4 rounded-full text-fog-400 hover:text-white hover:bg-white/[0.1] flex items-center justify-center text-[10px]"
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

      {/* Context window side panel */}
      {contextOpen && (
        <ContextViewer
          data={contextData}
          loading={contextLoading}
          error={contextError}
          onClose={() => setContextOpen(false)}
          onRefresh={openContextViewer}
          onDelete={(id) =>
            setConfirmDialog({
              title: 'Remove from context?',
              message:
                'This message will be deleted from the conversation and from the model\'s memory of this thread. This cannot be undone.',
              confirmLabel: 'Remove',
              danger: true,
              onConfirm: () => {
                setConfirmDialog(null)
                deleteContextMessage(id)
              },
            })
          }
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
        Start a new chat or open a project from the sidebar. Chats inside a
        project share context; standalone chats stay isolated.
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
      <h3 className="text-base font-medium text-white mb-2">{title}</h3>
      <p className="text-sm text-fog-300 leading-relaxed mb-5">{message}</p>
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-white/[0.06]"
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          autoFocus
          className={`px-4 py-2 rounded-lg text-sm font-medium ${
            danger
              ? 'bg-red-500/90 hover:bg-red-500 text-white'
              : 'bg-white text-black hover:bg-white/90'
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
      <h3 className="text-base font-medium text-white mb-2">{title}</h3>
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
        className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40 mb-5"
      />
      <div className="flex justify-end gap-2">
        <button
          onClick={onCancel}
          className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-white/[0.06]"
        >
          Cancel
        </button>
        <button
          onClick={() => onConfirm(value)}
          disabled={!value.trim()}
          className="px-4 py-2 rounded-lg text-sm font-medium bg-white text-black hover:bg-white/90 disabled:opacity-40 disabled:cursor-not-allowed"
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
            <span className="text-sm text-white truncate">{name}</span>
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
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-white/[0.06] disabled:opacity-40"
            >
              Copy
            </button>
            <button
              onClick={onClose}
              className="w-7 h-7 rounded hover:bg-white/[0.06] text-fog-300 flex items-center justify-center"
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

/* ─────────────────────────── context window viewer ─────────────────────────── */

function ContextViewer({
  data,
  loading,
  error,
  onClose,
  onRefresh,
  onDelete,
}: {
  data: any
  loading: boolean
  error: string | null
  onClose: () => void
  onRefresh: () => void
  onDelete: (messageId: number) => void
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
          <div className="text-sm text-white font-medium">Context window</div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setRawMode((v) => !v)}
              disabled={loading || !data}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-white/[0.06] disabled:opacity-40"
              title="See system prompt + every message as one block"
            >
              {rawMode ? 'Summary' : 'View raw'}
            </button>
            <button
              onClick={onRefresh}
              disabled={loading}
              className="text-xs px-2 py-1 rounded text-fog-300 hover:bg-white/[0.06] disabled:opacity-40"
            >
              Refresh
            </button>
            <button
              onClick={onClose}
              className="w-7 h-7 rounded hover:bg-white/[0.06] text-fog-300 flex items-center justify-center"
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
                  className="text-[11px] px-2 py-0.5 rounded text-fog-300 hover:bg-white/[0.06]"
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
                <div className="h-2 rounded-full bg-white/[0.06] overflow-hidden">
                  <div
                    className={`h-full ${barColor}`}
                    style={{ width: `${Math.min(100, pct)}%` }}
                  />
                </div>
              </section>

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

              {/* Messages */}
              <section>
                <div className="text-[11px] uppercase tracking-widest text-fog-500 mb-2">
                  Messages · {data.messages.length}
                </div>
                {data.messages.length === 0 && (
                  <p className="text-fog-500 text-xs">No messages yet.</p>
                )}
                <div className="space-y-1.5">
                  {data.messages.map((m: any, i: number) => (
                    <ContextMessageRow
                      key={m.id ?? i}
                      msg={m}
                      onDelete={onDelete}
                    />
                  ))}
                </div>
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
                        className="flex items-center justify-between text-[12.5px] py-1 px-2 rounded hover:bg-white/[0.03]"
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
      <div className="w-full flex items-center gap-2 hover:bg-white/[0.03]">
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
                className="w-full text-left px-2 py-1.5 flex items-center gap-2 hover:bg-white/[0.03] text-[11px] text-fog-300"
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
      <h3 className="text-base font-medium text-white mb-2">GitHub</h3>
      {username ? (
        <>
          <p className="text-sm text-fog-300 leading-relaxed mb-4">
            Connected as{' '}
            <span className="text-white font-medium">@{username}</span>. The
            agent can now read, push to, and revert files in repos you grant
            access to.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-white/[0.06]"
            >
              Close
            </button>
            <button
              onClick={async () => {
                await onDisconnect()
                onClose()
              }}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-red-500/90 hover:bg-red-500 text-white"
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
            className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40 mb-3 font-mono"
          />
          {error && (
            <p className="text-sm text-red-400 mb-3">{error}</p>
          )}
          <div className="flex justify-end gap-2">
            <button
              onClick={onClose}
              disabled={saving}
              className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-white/[0.06] disabled:opacity-40"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={saving || !token.trim()}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-white text-black hover:bg-white/90 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {saving ? 'Verifying…' : 'Connect'}
            </button>
          </div>
        </>
      )}
    </ModalShell>
  )
}
