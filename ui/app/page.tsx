'use client'

import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { supabase, authFetch } from '@/lib/supabase'

type Session = { id: string; name: string; created_at: string }
type Thread = { id: string; session_id: string; name: string; created_at: string }
type ToolCall = { name: string; args: Record<string, unknown> }
type Message = {
  role: 'user' | 'assistant' | 'tool'
  content: string
  tool_name?: string
  tool_calls?: ToolCall[]
}

export default function Home() {
  const router = useRouter()
  const [userEmail, setUserEmail] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [sessions, setSessions] = useState<Session[]>([])
  const [threadsMap, setThreadsMap] = useState<Record<string, Thread[]>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [activeSession, setActiveSession] = useState<string | null>(null)
  const [activeThread, setActiveThread] = useState<string | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) {
        router.replace('/login')
        return
      }
      setUserEmail(data.session.user.email ?? null)
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

  async function createSession() {
    const name = window.prompt('Project name?')?.trim()
    if (!name) return
    const r = await authFetch('/api/sessions', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
    const data = await r.json()
    const s: Session = { id: data.id, name: data.name, created_at: data.created_at }
    const defaultThread: Thread | undefined = data.default_thread
    setSessions((prev) => [s, ...prev])
    setExpanded((prev) => new Set(prev).add(s.id))
    if (defaultThread) {
      setThreadsMap((prev) => ({ ...prev, [s.id]: [defaultThread] }))
      selectThread(s.id, defaultThread.id)
    } else {
      setThreadsMap((prev) => ({ ...prev, [s.id]: [] }))
    }
  }

  async function createThread(sid: string) {
    const name = window.prompt('Thread name?')?.trim()
    if (!name) return
    const r = await authFetch(`/api/sessions/${sid}/threads`, {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
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

  async function send() {
    if (!input.trim() || !activeSession || !activeThread || sending) return
    const sid = activeSession
    const tid = activeThread
    const msg = input.trim()
    setInput('')
    setSending(true)
    setMessages((prev) => [...prev, { role: 'user', content: msg }])

    const baselineAssistantCount = messages.filter(
      (m) => m.role === 'assistant' && m.content?.trim(),
    ).length

    authFetch('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ session_id: sid, thread_id: tid, message: msg }),
    }).catch(() => {})

    const deadline = Date.now() + 90_000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500))
      const r = await authFetch(`/api/sessions/${sid}/threads/${tid}/history`)
      if (r.ok) {
        const history: Message[] = await r.json()
        const newCount = history.filter(
          (m) => m.role === 'assistant' && m.content?.trim(),
        ).length
        if (newCount > baselineAssistantCount) {
          setMessages(history)
          setSending(false)
          return
        }
      }
    }
    setSending(false)
  }

  const activeSessionObj = sessions.find((s) => s.id === activeSession)
  const activeThreadObj = activeSession
    ? threadsMap[activeSession]?.find((t) => t.id === activeThread)
    : undefined

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center text-gray-500">
        Loading...
      </div>
    )
  }

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <aside className="w-96 border-r border-border bg-panel flex flex-col">
        <div className="p-4 border-b border-border">
          <button
            onClick={createSession}
            className="w-full bg-accent hover:bg-blue-500 text-white text-base font-medium py-3 rounded-md"
          >
            + New Project
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {sessions.length === 0 && (
            <p className="p-4 text-sm text-gray-500">
              No projects yet. Create one to get started.
            </p>
          )}
          {sessions.map((s) => {
            const isOpen = expanded.has(s.id)
            const threads = threadsMap[s.id] || []
            return (
              <div key={s.id} className="border-b border-border/50">
                <button
                  onClick={() => toggleSession(s.id)}
                  className="w-full px-4 py-3 text-left text-base flex items-center justify-between hover:bg-panelAlt"
                >
                  <span className="truncate">
                    <span className="mr-2 text-gray-500">
                      {isOpen ? '▾' : '▸'}
                    </span>
                    {s.name}
                  </span>
                </button>
                {isOpen && (
                  <div className="pb-2">
                    {threads.map((t) => (
                      <button
                        key={t.id}
                        onClick={() => selectThread(s.id, t.id)}
                        className={`w-full text-left pl-10 pr-3 py-2 text-sm truncate ${
                          activeThread === t.id
                            ? 'bg-accent/20 text-white'
                            : 'text-gray-300 hover:bg-panelAlt'
                        }`}
                      >
                        # {t.name}
                      </button>
                    ))}
                    <button
                      onClick={() => createThread(s.id)}
                      className="w-full text-left pl-10 pr-3 py-2 text-sm text-gray-500 hover:text-white"
                    >
                      + New thread
                    </button>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </aside>

      {/* Chat pane */}
      <main className="flex-1 flex flex-col">
        <header className="px-6 py-3 border-b border-border bg-panel flex items-center justify-between">
          <div>
            {activeSessionObj && activeThreadObj ? (
              <div className="text-sm">
                <span className="text-gray-400">{activeSessionObj.name}</span>
                <span className="text-gray-600 mx-2">/</span>
                <span className="text-white font-medium">
                  {activeThreadObj.name}
                </span>
              </div>
            ) : (
              <div className="text-sm text-gray-500">
                Select a project and thread to start chatting
              </div>
            )}
          </div>
          <div className="flex items-center gap-3 text-sm">
            <span className="text-gray-400">{userEmail}</span>
            <button
              onClick={signOut}
              className="text-gray-400 hover:text-white border border-border rounded-md px-3 py-1"
            >
              Sign out
            </button>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {!activeThread && (
            <div className="h-full flex items-center justify-center text-gray-600 text-base">
              No thread selected
            </div>
          )}
          {messages
            .filter(
              (m) =>
                m.role !== 'tool' &&
                !(m.role === 'assistant' && !m.content?.trim()),
            )
            .map((m, i) => (
              <MessageBubble key={i} msg={m} />
            ))}
          {sending && <TypingIndicator />}
          <div ref={endRef} />
        </div>

        <footer className="p-4 border-t border-border bg-panel">
          <form
            onSubmit={(e) => {
              e.preventDefault()
              send()
            }}
            className="flex gap-2"
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={!activeThread || sending}
              placeholder={
                activeThread ? 'Send a message...' : 'Select a thread first'
              }
              className="flex-1 bg-panelAlt border border-border rounded-md px-4 py-3 text-base outline-none focus:border-accent disabled:opacity-50"
            />
            <button
              type="submit"
              disabled={!activeThread || sending || !input.trim()}
              className="bg-accent hover:bg-blue-500 disabled:opacity-40 text-white text-base font-medium px-5 py-3 rounded-md"
            >
              {sending ? '...' : 'Send'}
            </button>
          </form>
        </footer>
      </main>
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="bg-panelAlt border border-border text-gray-300 px-4 py-3 rounded-lg text-base flex items-center gap-1">
        <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce [animation-delay:-0.3s]" />
        <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce [animation-delay:-0.15s]" />
        <span className="w-2 h-2 rounded-full bg-gray-400 animate-bounce" />
      </div>
    </div>
  )
}

function MessageBubble({ msg }: { msg: Message }) {
  if (msg.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-3xl bg-accent/90 text-white px-4 py-3 rounded-lg text-base leading-relaxed whitespace-pre-wrap">
          {msg.content}
        </div>
      </div>
    )
  }
  if (msg.role === 'tool') {
    return (
      <div className="flex justify-start">
        <div className="max-w-3xl bg-panelAlt border border-border text-gray-400 px-4 py-3 rounded-lg text-sm font-mono">
          <div className="text-gray-500 mb-1">
            🛠️ tool: {msg.tool_name}
          </div>
          <div className="whitespace-pre-wrap">{msg.content}</div>
        </div>
      </div>
    )
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-3xl bg-panelAlt border border-border text-gray-100 px-4 py-3 rounded-lg text-base leading-relaxed markdown-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {msg.content}
        </ReactMarkdown>
      </div>
    </div>
  )
}
