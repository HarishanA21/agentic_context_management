'use client'

// Plugins manage page — mirrors the Skills page (components/skills-inventory).
//
// A plugin adds a REAL tool to the agent (backend/plugins_catalog.py). Turning
// one on enables it for the user; the agent's next turn gets that tool in its
// toolbox. Plugins are code-defined (no user-authored ones), so this page just
// lists the catalog with an on/off switch each.

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { authFetch } from '@/lib/supabase'

type Plugin = {
  slug: string
  name: string
  publisher: string
  description: string
  icon: string
  tools: string[]
  enabled: boolean
}

function usePlugins() {
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      const r = await authFetch('/api/plugins')
      if (!r.ok) {
        setError(`Failed to load plugins: ${r.status} ${r.statusText}`)
        return
      }
      setPlugins((await r.json()) as Plugin[])
      setError(null)
    } catch (e: any) {
      setError(e?.message ?? 'Failed to load plugins')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Optimistic toggle — flip locally, reconcile on failure.
  async function toggle(slug: string, enabled: boolean) {
    setPlugins((prev) =>
      prev.map((p) => (p.slug === slug ? { ...p, enabled } : p)),
    )
    const r = await authFetch(`/api/plugins/${encodeURIComponent(slug)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
    if (!r.ok) {
      setPlugins((prev) =>
        prev.map((p) => (p.slug === slug ? { ...p, enabled: !enabled } : p)),
      )
    }
  }

  const enabledCount = plugins.filter((p) => p.enabled).length
  return { plugins, loading, error, enabledCount, toggle }
}

function Toggle({
  on,
  onChange,
}: {
  on: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={(e) => {
        e.stopPropagation()
        onChange(!on)
      }}
      className={`relative w-9 h-5 rounded-full shrink-0 transition ${
        on ? 'bg-accent' : 'bg-soft/20'
      }`}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
          on ? 'translate-x-4' : 'translate-x-0'
        }`}
      />
    </button>
  )
}

function PluginGlyph({ icon }: { icon: string }) {
  const path =
    icon === 'web'
      ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20'
      : icon === 'ruler'
        ? 'M3 17L17 3l4 4L7 21zM7 11l2 2M11 7l2 2M15 11l2 2'
        : icon === 'clock'
          ? 'M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z'
          : icon === 'hash'
            ? 'M4 9h16M4 15h16M10 3L8 21M16 3l-2 18'
            : icon === 'terminal'
              ? 'M4 4h16v16H4zM7 9l3 3-3 3M13 15h4'
              : 'M16 18l6-6-6-6M8 6l-6 6 6 6'
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d={path} />
    </svg>
  )
}

export function PluginsInventoryPanel({
  embedded = false,
}: {
  embedded?: boolean
}) {
  const { plugins, loading, error, enabledCount, toggle } = usePlugins()

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="h-12 px-5 border-b border-line flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2 text-sm">
          {!embedded && (
            <Link href="/app" className="text-fog-400 hover:text-fog-100">
              ← Back
            </Link>
          )}
          <span className="font-medium text-fog-50">Plugins</span>
          <span className="text-[12px] text-fog-500">{enabledCount} installed</span>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto p-5">
        <p className="text-[13px] text-fog-400 max-w-2xl mb-5">
          Plugins add real tools to the agent. Turn one on and the agent can
          call its tool while chatting — fetching a web page, reformatting JSON,
          converting units, and so on.
        </p>

        {error && (
          <div className="mb-4 text-[13px] text-red-400 border border-red-500/30 bg-red-500/5 rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-fog-400 text-sm">Loading plugins…</div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 max-w-3xl">
            {plugins.map((p) => (
              <div
                key={p.slug}
                className={`rounded-xl border p-4 transition ${
                  p.enabled
                    ? 'border-accent/40 bg-accent/[0.04]'
                    : 'border-lineStrong bg-ink-200'
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="w-8 h-8 rounded-lg bg-soft/10 text-accent flex items-center justify-center shrink-0">
                    <PluginGlyph icon={p.icon} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-fog-50 truncate">
                        {p.name}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-soft/10 text-fog-400">
                        {p.publisher}
                      </span>
                    </div>
                    <p className="text-[12.5px] text-fog-400 mt-1 line-clamp-3">
                      {p.description}
                    </p>
                  </div>
                  <Toggle on={p.enabled} onChange={(v) => toggle(p.slug, v)} />
                </div>

                {p.tools?.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mt-3 pl-11">
                    {p.tools.map((t) => (
                      <span
                        key={t}
                        className="text-[11px] px-1.5 py-0.5 rounded bg-soft/10 text-fog-300 font-mono"
                      >
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
