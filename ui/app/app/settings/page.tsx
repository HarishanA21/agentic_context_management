'use client'

import { useEffect, useState, type ReactNode } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

import { supabase } from '@/lib/supabase'

type Card = {
  title: string
  href: string
  description: string
  icon: ReactNode
}

const CARDS: Card[] = [
  {
    title: 'LLM Providers',
    href: '/app/providers',
    description:
      'Connect OpenRouter, OpenAI, Anthropic, AWS Bedrock, Azure, and Google. Set per-session defaults.',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 12c0 4.97-4.03 9-9 9s-9-4.03-9-9 4.03-9 9-9 9 4.03 9 9z" />
        <path d="M3 12h18M12 3a14.7 14.7 0 0 1 0 18M12 3a14.7 14.7 0 0 0 0 18" opacity="0.55" />
      </svg>
    ),
  },
  {
    title: 'MCP Servers',
    href: '/app/mcps',
    description: 'Add and manage Model Context Protocol tool servers.',
    icon: (
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        <path d="M3.27 6.96L12 12.01l8.73-5.05M12 22.08V12" opacity="0.6" />
      </svg>
    ),
  },
]

export default function SettingsPage() {
  const router = useRouter()
  const [ready, setReady] = useState(false)
  const [userEmail, setUserEmail] = useState<string | null>(null)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) {
        router.replace('/login')
        return
      }
      setUserEmail(data.session.user.email ?? null)
      setReady(true)
    })
  }, [router])

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center text-fog-400">
        Loading…
      </div>
    )
  }

  return (
    <div className="flex h-screen bg-ink-50 text-fog-100 overflow-hidden flex-col">
      <header className="border-b border-line bg-ink-100 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            href="/app"
            className="text-fog-300 hover:text-white text-sm flex items-center gap-1"
          >
            ← Back to chat
          </Link>
          <span className="text-fog-500">/</span>
          <h1 className="text-white text-sm font-medium">Settings</h1>
        </div>
        <span className="text-fog-400 text-xs">{userEmail}</span>
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-4xl">
          <p className="text-fog-400 text-sm mb-6">
            Configure providers, tools, and context-management behavior.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {CARDS.map((c) => (
              <Link
                key={c.href}
                href={c.href}
                className="group rounded-xl border border-line bg-ink-100 p-5 transition hover:bg-soft/[0.04] hover:border-lineStrong"
              >
                <div className="flex items-start gap-3">
                  <span className="w-9 h-9 shrink-0 rounded-md bg-soft/10 text-accent flex items-center justify-center">
                    {c.icon}
                  </span>
                  <div className="min-w-0">
                    <h2 className="text-white text-sm font-medium">{c.title}</h2>
                    <p className="text-fog-400 text-xs mt-1 leading-relaxed">
                      {c.description}
                    </p>
                  </div>
                </div>
                <div className="mt-4 text-[12px] text-accent opacity-80 group-hover:opacity-100">
                  Configure →
                </div>
              </Link>
            ))}
          </div>
        </div>
      </main>
    </div>
  )
}
