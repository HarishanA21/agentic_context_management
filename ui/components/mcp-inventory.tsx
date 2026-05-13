'use client'

// MCP inventory page.
// Slice A: catalog + read-only listing.
// Slice B: enable / disable / per-MCP env-var modal + agent rebuild on save.

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { authFetch } from '@/lib/supabase'
import { useTheme } from '@/lib/theme'
import { MCPIcon } from '@/components/mcp-icons'

/* ─── types ─── */

type EnvField = {
  name: string
  label: string
  help?: string
  required: boolean
  secret: boolean
}

type StdioConfig = {
  command: string
  args: string[]
  env_schema: EnvField[]
}

type HttpConfig = {
  url_template: string
  url_help?: string
  auth?: string
  auth_header?: string
  auth_value_prefix?: string
}

type Transport = 'stdio' | 'streamable_http' | 'sse' | 'http'

type CatalogEntry = {
  slug: string
  name: string
  description: string
  publisher: string
  homepage?: string
  docs_url?: string
  icon: string
  tags: string[]
  category: string
  default_transport: Transport
  transports: Partial<Record<Transport, StdioConfig | HttpConfig>>
  auth: 'none' | 'bearer' | 'api_key_header' | 'api_key_env' | 'oauth'
  needs_outbound_network?: boolean
  warnings?: string[]
}

type ServerRow = {
  id: string
  catalog_slug: string | null
  is_custom: boolean
  name: string
  enabled: boolean
  transport: Transport
  command: string | null
  args_json: string[] | null
  endpoint_url: string | null
  auth_kind: string | null
  auth_header: string | null
  has_secret: boolean
  tools_json: any[] | null
  last_connected_at: string | null
  last_error: string | null
}

const TRANSPORT_LABEL: Record<string, string> = {
  stdio: 'stdio',
  streamable_http: 'streamable HTTP',
  sse: 'SSE',
  http: 'HTTP',
}

const CATEGORY_LABEL: Record<string, string> = {
  tools: 'Tools',
  data: 'Data',
  knowledge: 'Knowledge',
  dev: 'Dev',
  comms: 'Communication',
}

/* ─── panel ─── */

// Owns all MCP-inventory state/handlers and renders its own header, body,
// modals, and toast. Pass `embedded` from the main app shell so the
// header shows the in-app label instead of the standalone "Back / MCPs"
// breadcrumb used by the /app/mcps route.
export function MCPInventoryPanel({ embedded = false }: { embedded?: boolean }) {
  const [tab, setTab] = useState<'catalog' | 'yours'>('catalog')
  const [catalog, setCatalog] = useState<CatalogEntry[]>([])
  const [servers, setServers] = useState<ServerRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')
  const [toast, setToast] = useState<string | null>(null)
  const [enableEntry, setEnableEntry] = useState<{
    catalog: CatalogEntry
    server: ServerRow | null
  } | null>(null)
  const [customOpen, setCustomOpen] = useState(false)

  async function refresh() {
    try {
      const [catRes, srvRes] = await Promise.all([
        authFetch('/api/mcp/catalog'),
        authFetch('/api/mcp/servers'),
      ])
      if (!catRes.ok) {
        setError(`Failed to load catalog: ${catRes.status} ${catRes.statusText}`)
        return
      }
      if (!srvRes.ok) {
        setError(`Failed to load servers: ${srvRes.status} ${srvRes.statusText}`)
        return
      }
      const catBody = await catRes.json()
      const srvBody = await srvRes.json()
      setCatalog(catBody.entries || [])
      setServers(srvBody || [])
      setError(null)
    } catch (e: any) {
      setError(e?.message ?? 'Network error')
    }
  }

  useEffect(() => {
    setLoading(true)
    refresh().finally(() => setLoading(false))
  }, [])

  // Re-fetch on window focus so a toggle made elsewhere (or via a
  // background tab) shows up immediately when this tab comes forward.
  useEffect(() => {
    if (typeof window === 'undefined') return
    function onFocus() {
      refresh()
    }
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [])

  // Auto-dismiss toasts after a beat.
  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 2200)
    return () => clearTimeout(t)
  }, [toast])

  const enabledBySlug = useMemo(() => {
    const map: Record<string, ServerRow> = {}
    for (const s of servers) {
      if (s.catalog_slug) map[s.catalog_slug] = s
    }
    return map
  }, [servers])

  const catalogCards: Array<{
    catalog: CatalogEntry
    server: ServerRow | null
  }> = useMemo(() => {
    const q = query.trim().toLowerCase()
    return catalog
      .filter((c) => {
        if (categoryFilter !== 'all' && c.category !== categoryFilter) return false
        if (!q) return true
        return (
          c.name.toLowerCase().includes(q) ||
          c.description.toLowerCase().includes(q) ||
          c.publisher.toLowerCase().includes(q) ||
          c.tags.some((t) => t.toLowerCase().includes(q))
        )
      })
      .map((c) => ({ catalog: c, server: enabledBySlug[c.slug] ?? null }))
  }, [catalog, enabledBySlug, query, categoryFilter])

  const yoursCards: Array<{
    catalog: CatalogEntry | null
    server: ServerRow
  }> = useMemo(() => {
    const slugToCatalog: Record<string, CatalogEntry> = {}
    for (const c of catalog) slugToCatalog[c.slug] = c
    return servers.map((s) => ({
      catalog: s.catalog_slug ? slugToCatalog[s.catalog_slug] ?? null : null,
      server: s,
    }))
  }, [catalog, servers])

  const categories = useMemo(() => {
    const set = new Set(catalog.map((c) => c.category))
    return Array.from(set).sort()
  }, [catalog])

  // Toggle wraps the most common case — flipping `enabled`. For
  // first-time enables that need configuration we route through the modal.
  async function quickToggle(card: { catalog: CatalogEntry; server: ServerRow | null }) {
    const { catalog: c, server } = card
    const needsAnyConfig =
      c.auth !== 'none' || c.default_transport !== 'stdio' || Object.keys(c.transports).length > 1
    // First-time enable that needs config → open modal.
    if (!server && needsAnyConfig) {
      setEnableEntry(card)
      return
    }
    try {
      if (!server) {
        // First-time enable, no extra config required (e.g. Filesystem,
        // Fetch). Create the row in enabled state.
        const r = await authFetch('/api/mcp/servers', {
          method: 'POST',
          body: JSON.stringify({ catalog_slug: c.slug, enabled: true }),
        })
        if (!r.ok) {
          const body = await r.json().catch(() => ({}))
          setToast(`Enable failed: ${body?.detail ?? r.statusText}`)
          return
        }
        setToast(`${c.name} enabled`)
      } else {
        const r = await authFetch(`/api/mcp/servers/${server.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ enabled: !server.enabled }),
        })
        if (!r.ok) {
          const body = await r.json().catch(() => ({}))
          setToast(`Update failed: ${body?.detail ?? r.statusText}`)
          return
        }
        setToast(server.enabled ? `${c.name} disabled` : `${c.name} enabled`)
      }
      await refresh()
    } catch (e: any) {
      setToast(`Network error: ${e?.message ?? 'unknown'}`)
    }
  }

  return (
    <div className="flex flex-col h-full min-h-0 bg-ink-50 text-fog-100">
      <header className="h-12 px-5 border-b border-line flex items-center justify-between shrink-0">
        {embedded ? (
          <span className="text-sm text-fog-50 font-medium">MCP Inventory</span>
        ) : (
          <div className="flex items-center gap-3">
            <Link
              href="/app"
              className="text-sm text-fog-300 hover:text-fog-50 transition inline-flex items-center gap-1.5"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M15 18l-6-6 6-6" />
              </svg>
              Back
            </Link>
            <span className="text-fog-500">/</span>
            <span className="text-sm text-fog-50 font-medium">MCPs</span>
          </div>
        )}
        <HeaderActions />
      </header>

        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-5xl px-6 py-8">
            <div className="mb-6">
              <h1 className="serif text-3xl tracking-tighter text-fog-50">
                MCP servers
              </h1>
              <p className="text-sm text-fog-400 mt-1.5 max-w-2xl">
                Browse a curated catalog of Model Context Protocol servers,
                enable the ones you need, or add your own. Tools from
                enabled servers appear in the agent's tool list on the
                next chat turn.
              </p>
            </div>

            <div className="flex items-center gap-1 mb-4 border-b border-line">
              <TabButton
                active={tab === 'catalog'}
                onClick={() => setTab('catalog')}
                label="Catalog"
                count={catalog.length}
              />
              <TabButton
                active={tab === 'yours'}
                onClick={() => setTab('yours')}
                label="Yours"
                count={servers.length}
              />
              <div className="flex-1" />
              <button
                onClick={() => setCustomOpen(true)}
                className="text-xs px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 transition font-medium"
              >
                + Custom MCP
              </button>
            </div>

            {tab === 'catalog' && (
              <>
                <div className="flex flex-wrap items-center gap-2 mb-4">
                  <input
                    type="search"
                    placeholder="Search catalog…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    className="bg-ink-200 border border-line rounded-md px-3 py-1.5 text-sm text-fog-100 placeholder:text-fog-500 outline-none focus:border-lineStrong w-64"
                  />
                  <select
                    value={categoryFilter}
                    onChange={(e) => setCategoryFilter(e.target.value)}
                    className="bg-ink-200 border border-line rounded-md px-3 py-1.5 text-sm text-fog-100 outline-none focus:border-lineStrong"
                  >
                    <option value="all">All categories</option>
                    {categories.map((c) => (
                      <option key={c} value={c}>
                        {CATEGORY_LABEL[c] || c}
                      </option>
                    ))}
                  </select>
                </div>

                {loading && <Loading />}
                {!loading && error && <ErrorBanner text={error} />}
                {!loading && !error && (
                  <CatalogSections
                    cards={catalogCards}
                    onToggle={(card) => quickToggle(card)}
                    onConfigure={(card) => setEnableEntry(card)}
                  />
                )}
              </>
            )}

            {tab === 'yours' && (
              <>
                {loading && <Loading />}
                {!loading && error && <ErrorBanner text={error} />}
                {!loading && !error && yoursCards.length === 0 && (
                  <div className="surface px-6 py-10 text-center">
                    <p className="text-sm text-fog-300">
                      You haven't enabled any MCPs yet.
                    </p>
                    <button
                      onClick={() => setTab('catalog')}
                      className="mt-3 text-xs px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 transition"
                    >
                      Browse catalog
                    </button>
                  </div>
                )}
                {!loading && !error && yoursCards.length > 0 && (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {yoursCards.map(({ catalog, server }) => (
                      <YoursCard
                        key={server.id}
                        entry={catalog}
                        server={server}
                        onRefresh={async () => {
                          const r = await authFetch(
                            `/api/mcp/servers/${server.id}/test`,
                            { method: 'POST' },
                          )
                          const body = await r.json().catch(() => ({}))
                          if (body?.ok) {
                            setToast(
                              `${server.name}: ${body.tools.length} tools`,
                            )
                          } else {
                            setToast(
                              `${server.name} failed: ${body?.error ?? r.statusText}`,
                            )
                          }
                          await refresh()
                        }}
                        onToggle={async () => {
                          const r = await authFetch(
                            `/api/mcp/servers/${server.id}`,
                            {
                              method: 'PATCH',
                              body: JSON.stringify({
                                enabled: !server.enabled,
                              }),
                            },
                          )
                          if (r.ok) {
                            setToast(
                              server.enabled
                                ? `${server.name} disabled`
                                : `${server.name} enabled`,
                            )
                            await refresh()
                          } else {
                            const body = await r.json().catch(() => ({}))
                            setToast(`Update failed: ${body?.detail ?? r.statusText}`)
                          }
                        }}
                        onConfigure={() => {
                          if (catalog) setEnableEntry({ catalog, server })
                        }}
                        onRemove={async () => {
                          if (
                            !confirm(
                              server.is_custom
                                ? `Delete '${server.name}'? This cannot be undone.`
                                : `Disable '${server.name}' and clear its saved credentials?`,
                            )
                          )
                            return
                          const r = await authFetch(
                            `/api/mcp/servers/${server.id}`,
                            { method: 'DELETE' },
                          )
                          if (r.ok) {
                            setToast(
                              server.is_custom ? 'Deleted' : 'Disabled',
                            )
                            await refresh()
                          } else {
                            const body = await r.json().catch(() => ({}))
                            setToast(`Failed: ${body?.detail ?? r.statusText}`)
                          }
                        }}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>

      {enableEntry && (
        <EnableModal
          entry={enableEntry.catalog}
          server={enableEntry.server}
          onClose={() => setEnableEntry(null)}
          onSaved={async (message) => {
            setEnableEntry(null)
            setToast(message)
            await refresh()
          }}
        />
      )}

      {customOpen && (
        <CustomMCPModal
          onClose={() => setCustomOpen(false)}
          onSaved={async (message) => {
            setCustomOpen(false)
            setToast(message)
            await refresh()
            setTab('yours')
          }}
        />
      )}

      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] px-4 py-2 rounded-full bg-accent text-ink-50 text-sm shadow-2xl animate-float-up">
          {toast}
        </div>
      )}
    </div>
  )
}

/* ─── pieces ─── */

function HeaderActions() {
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

function TabButton({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean
  onClick: () => void
  label: string
  count: number
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-2 text-sm flex items-center gap-2 border-b-2 transition ${
        active
          ? 'border-fog-50 text-fog-50'
          : 'border-transparent text-fog-400 hover:text-fog-200'
      }`}
    >
      {label}
      <span className="text-[10px] tabular-nums px-1.5 py-0.5 rounded-full bg-soft/[0.06] text-fog-400">
        {count}
      </span>
    </button>
  )
}

function CatalogCard({
  entry,
  server,
  onToggle,
  onConfigure,
}: {
  entry: CatalogEntry
  server: ServerRow | null
  onToggle: () => void
  onConfigure: () => void
}) {
  const isEnabled = server?.enabled === true
  const transports = Object.keys(entry.transports) as Transport[]
  const isOAuth = entry.auth === 'oauth'
  return (
    <div className="surface p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <div className="shrink-0">
          <MCPIcon slug={entry.icon} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-medium text-fog-50 truncate">
              {entry.name}
            </h3>
            {isEnabled && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                Enabled
              </span>
            )}
            {!isEnabled && server && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-soft/[0.06] text-fog-400 border border-line">
                Paused
              </span>
            )}
            {entry.tags.includes('power') && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">
                Power
              </span>
            )}
          </div>
          <div className="text-[11px] text-fog-500 mt-0.5">
            by {entry.publisher}
          </div>
        </div>
      </div>
      <p className="text-[13px] text-fog-300 leading-relaxed">
        {entry.description}
      </p>
      <div className="flex items-center gap-1.5 flex-wrap">
        {transports.map((t) => (
          <span
            key={t}
            className={`text-[10px] px-1.5 py-0.5 rounded-full border ${
              t === entry.default_transport
                ? 'border-fog-300 text-fog-100'
                : 'border-line text-fog-400'
            }`}
            title={t === entry.default_transport ? 'Default transport' : 'Available'}
          >
            {TRANSPORT_LABEL[t] || t}
          </span>
        ))}
        {entry.needs_outbound_network && (
          <span
            className="text-[10px] px-1.5 py-0.5 rounded-full border border-line text-fog-400 inline-flex items-center gap-1"
            title="Reaches the public internet"
          >
            <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" />
              <path d="M2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20" />
            </svg>
            network
          </span>
        )}
      </div>
      {entry.warnings && entry.warnings.length > 0 && (
        <div className="text-[11px] text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded-md px-2 py-1.5">
          {entry.warnings.join(' · ')}
        </div>
      )}
      <div className="flex items-center justify-between pt-1 border-t border-line">
        <div className="flex items-center gap-2 text-[11px] text-fog-500">
          {entry.homepage && (
            <a
              href={entry.homepage}
              target="_blank"
              rel="noreferrer"
              className="hover:text-fog-200 underline-offset-2 hover:underline"
            >
              Homepage
            </a>
          )}
          {entry.docs_url && (
            <a
              href={entry.docs_url}
              target="_blank"
              rel="noreferrer"
              className="hover:text-fog-200 underline-offset-2 hover:underline"
            >
              Docs
            </a>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {server && (
            <button
              onClick={onConfigure}
              title="Edit configuration"
              className="text-xs px-2 py-1 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 transition"
            >
              Settings
            </button>
          )}
          {isOAuth ? (
            <button
              disabled
              title="OAuth flow coming soon — paste a personal token via Settings"
              className="text-xs px-3 py-1 rounded-md border border-line text-fog-500 opacity-60 cursor-not-allowed"
            >
              Connect…
            </button>
          ) : (
            <button
              onClick={onToggle}
              className={`text-xs px-3 py-1 rounded-md font-medium transition ${
                isEnabled
                  ? 'border border-line text-fog-200 hover:bg-soft/[0.06]'
                  : 'bg-accent text-ink-50 hover:bg-accent/90'
              }`}
            >
              {isEnabled ? 'Disable' : 'Enable'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

function YoursCard({
  entry,
  server,
  onToggle,
  onConfigure,
  onRemove,
  onRefresh,
}: {
  entry: CatalogEntry | null
  server: ServerRow
  onToggle: () => void
  onConfigure: () => void
  onRemove: () => void
  onRefresh: () => void
}) {
  const icon = entry?.icon || 'generic'
  const name = server.name
  return (
    <div className="surface p-4 flex flex-col gap-3">
      <div className="flex items-start gap-3">
        <div className="shrink-0">
          <MCPIcon slug={icon} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-medium text-fog-50 truncate">{name}</h3>
            {server.enabled ? (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                Enabled
              </span>
            ) : (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-soft/[0.06] text-fog-400 border border-line">
                Paused
              </span>
            )}
            {server.is_custom && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-line text-fog-400">
                Custom
              </span>
            )}
            {server.has_secret && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full border border-line text-fog-400">
                Secret saved
              </span>
            )}
          </div>
          <div className="text-[11px] text-fog-500 mt-0.5">
            {TRANSPORT_LABEL[server.transport] || server.transport}
            {server.endpoint_url ? ` · ${truncMiddle(server.endpoint_url, 48)}` : ''}
          </div>
        </div>
      </div>
      {server.last_error && (
        <div className="text-[11px] text-red-300/90 bg-red-500/10 border border-red-500/30 rounded-md px-2 py-1.5">
          Last error: {server.last_error}
        </div>
      )}
      <div className="flex items-center justify-between pt-1 border-t border-line">
        <div className="text-[11px] text-fog-500">
          {server.tools_json
            ? `${server.tools_json.length} tools discovered`
            : 'Tools not yet discovered'}
        </div>
        <div className="flex items-center gap-1.5">
          <button
            onClick={onRemove}
            className="text-xs px-2 py-1 rounded-md border border-line text-fog-400 hover:text-red-300 hover:border-red-400/40 transition"
          >
            {server.is_custom ? 'Delete' : 'Remove'}
          </button>
          <button
            onClick={onRefresh}
            title="Reconnect and refresh discovered tools"
            className="text-xs px-2 py-1 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 transition"
          >
            Refresh
          </button>
          {entry && (
            <button
              onClick={onConfigure}
              className="text-xs px-2 py-1 rounded-md border border-line text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 transition"
            >
              Settings
            </button>
          )}
          <button
            onClick={onToggle}
            className={`text-xs px-3 py-1 rounded-md font-medium transition ${
              server.enabled
                ? 'border border-line text-fog-200 hover:bg-soft/[0.06]'
                : 'bg-accent text-ink-50 hover:bg-accent/90'
            }`}
          >
            {server.enabled ? 'Disable' : 'Enable'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ─── catalog sections (common vs power) ─── */

function CatalogSections({
  cards,
  onToggle,
  onConfigure,
}: {
  cards: { catalog: CatalogEntry; server: ServerRow | null }[]
  onToggle: (card: { catalog: CatalogEntry; server: ServerRow | null }) => void
  onConfigure: (card: {
    catalog: CatalogEntry
    server: ServerRow | null
  }) => void
}) {
  const common = cards.filter((c) => !c.catalog.tags.includes('power'))
  const power = cards.filter((c) => c.catalog.tags.includes('power'))
  const [showPower, setShowPower] = useState(false)

  if (cards.length === 0) {
    return (
      <p className="text-sm text-fog-500 py-6 text-center">
        Nothing matches that filter.
      </p>
    )
  }

  return (
    <div className="space-y-6">
      {common.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {common.map((c) => (
            <CatalogCard
              key={c.catalog.slug}
              entry={c.catalog}
              server={c.server}
              onToggle={() => onToggle(c)}
              onConfigure={() => onConfigure(c)}
            />
          ))}
        </div>
      )}

      {power.length > 0 && (
        <div>
          <button
            onClick={() => setShowPower((v) => !v)}
            className="flex items-center gap-2 text-[11px] uppercase tracking-widest text-fog-400 hover:text-fog-200 transition mb-3"
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              className={`transition ${showPower ? 'rotate-90' : ''}`}
            >
              <path d="M9 18l6-6-6-6" />
            </svg>
            Power tools
            <span className="text-[10px] tabular-nums px-1.5 py-0.5 rounded-full bg-amber-500/15 text-amber-300 border border-amber-500/30">
              {power.length}
            </span>
            <span className="text-[10px] text-fog-500 normal-case tracking-normal">
              {showPower ? 'Hide' : 'Heavy / API-key tools — keep collapsed by default'}
            </span>
          </button>
          {showPower && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {power.map((c) => (
                <CatalogCard
                  key={c.catalog.slug}
                  entry={c.catalog}
                  server={c.server}
                  onToggle={() => onToggle(c)}
                  onConfigure={() => onConfigure(c)}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ─── custom MCP modal ─── */

const STDIO_COMMAND_OPTIONS = ['npx', 'uvx', 'python', 'node'] as const

function CustomMCPModal({
  onClose,
  onSaved,
}: {
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<Transport>('streamable_http')
  const [command, setCommand] = useState<(typeof STDIO_COMMAND_OPTIONS)[number]>(
    'npx',
  )
  const [argsLine, setArgsLine] = useState('')
  const [endpointUrl, setEndpointUrl] = useState('')
  const [authKind, setAuthKind] = useState<
    'none' | 'bearer' | 'api_key_header' | 'api_key_env'
  >('bearer')
  const [authHeader, setAuthHeader] = useState('Authorization')
  const [secret, setSecret] = useState('')
  const [envPairs, setEnvPairs] = useState<{ k: string; v: string }[]>([
    { k: '', v: '' },
  ])
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{
    ok: boolean
    tools: { name: string; description: string }[]
    error: string | null
  } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const isStdio = transport === 'stdio'

  function buildPayload(enabled: boolean) {
    const args = argsLine
      .split(/\s+/)
      .map((s) => s.trim())
      .filter(Boolean)
    const body: any = { name, transport, enabled }
    if (isStdio) {
      body.command = command
      body.args = args
      const envMap: Record<string, string> = {}
      for (const { k, v } of envPairs) {
        const key = k.trim()
        if (!key) continue
        envMap[key] = v
      }
      if (Object.keys(envMap).length > 0) {
        body.auth_kind = 'api_key_env'
        body.secret_env = envMap
      } else {
        body.auth_kind = 'none'
      }
    } else {
      body.endpoint_url = endpointUrl
      body.auth_kind = authKind
      if (authKind === 'api_key_header') body.auth_header = authHeader
      if (authKind === 'bearer' || authKind === 'api_key_header') {
        if (secret) body.secret = secret
      }
    }
    return body
  }

  function validate(): string | null {
    if (!name.trim()) return 'Name is required.'
    if (isStdio) {
      if (!command) return 'Command is required.'
      if (!argsLine.trim()) return 'Provide at least one argument.'
    } else {
      if (!endpointUrl.trim()) return 'Endpoint URL is required.'
      try {
        const u = new URL(endpointUrl)
        if (u.protocol !== 'https:' && u.hostname !== 'localhost') {
          return 'Only https:// URLs are allowed (or http://localhost for dev).'
        }
      } catch {
        return 'Endpoint URL is not a valid URL.'
      }
    }
    return null
  }

  async function save(enabled: boolean) {
    const v = validate()
    if (v) {
      setErr(v)
      return
    }
    setSaving(true)
    setErr(null)
    try {
      const r = await authFetch('/api/mcp/servers', {
        method: 'POST',
        body: JSON.stringify(buildPayload(enabled)),
      })
      if (!r.ok) {
        const b = await r.json().catch(() => ({}))
        throw new Error(b?.detail ?? `${r.status} ${r.statusText}`)
      }
      onSaved(`${name} ${enabled ? 'enabled' : 'saved'}`)
    } catch (e: any) {
      setErr(e?.message ?? 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  async function test() {
    const v = validate()
    if (v) {
      setErr(v)
      return
    }
    setTesting(true)
    setErr(null)
    setTestResult(null)
    try {
      const created = await authFetch('/api/mcp/servers', {
        method: 'POST',
        body: JSON.stringify(buildPayload(false)),
      })
      if (!created.ok) {
        const b = await created.json().catch(() => ({}))
        throw new Error(b?.detail ?? `${created.status} ${created.statusText}`)
      }
      const { id } = await created.json()
      const t = await authFetch(`/api/mcp/servers/${id}/test`, {
        method: 'POST',
      })
      const body = await t.json().catch(() => ({}))
      setTestResult({
        ok: !!body?.ok,
        tools: Array.isArray(body?.tools) ? body.tools : [],
        error: body?.error ?? (t.ok ? null : `${t.status} ${t.statusText}`),
      })
    } catch (e: any) {
      setErr(e?.message ?? 'Test failed')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 backdrop-blur-sm animate-float-up">
      <div className="w-full max-w-lg rounded-2xl border border-lineStrong bg-ink-100 shadow-2xl shadow-black/60 max-h-[90vh] overflow-y-auto">
        <div className="px-5 py-4 border-b border-line flex items-start justify-between">
          <div>
            <h2 className="serif text-xl tracking-tighter text-fog-50">
              Add custom MCP
            </h2>
            <p className="text-xs text-fog-400 mt-0.5">
              Hook up an MCP server we don't ship in the catalog.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-fog-400 hover:text-fog-50 w-7 h-7 rounded-full hover:bg-soft/[0.06] flex items-center justify-center"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="px-5 py-4 space-y-4 text-sm">
          <div>
            <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. My weather MCP"
              className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong"
            />
          </div>

          <div>
            <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
              Transport
            </label>
            <div className="flex gap-1.5 flex-wrap">
              {(
                ['streamable_http', 'sse', 'http', 'stdio'] as Transport[]
              ).map((t) => (
                <button
                  key={t}
                  onClick={() => setTransport(t)}
                  className={`text-xs px-2.5 py-1.5 rounded-md border transition ${
                    transport === t
                      ? 'border-fog-300 text-fog-50 bg-soft/[0.08]'
                      : 'border-line text-fog-400 hover:text-fog-200'
                  }`}
                >
                  {TRANSPORT_LABEL[t] || t}
                </button>
              ))}
            </div>
          </div>

          {isStdio ? (
            <>
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  Command
                </label>
                <select
                  value={command}
                  onChange={(e) => setCommand(e.target.value as any)}
                  className="bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong"
                >
                  {STDIO_COMMAND_OPTIONS.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  Arguments
                </label>
                <input
                  value={argsLine}
                  onChange={(e) => setArgsLine(e.target.value)}
                  placeholder="-y @your-org/your-mcp-server"
                  className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono text-[12px]"
                />
                <p className="text-[11px] text-fog-500 mt-1">
                  Space-separated. Quotes are not interpreted.
                </p>
              </div>
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  Environment variables
                </label>
                <div className="space-y-1.5">
                  {envPairs.map((p, i) => (
                    <div key={i} className="flex gap-1.5">
                      <input
                        value={p.k}
                        onChange={(e) =>
                          setEnvPairs((prev) =>
                            prev.map((q, idx) =>
                              idx === i ? { ...q, k: e.target.value } : q,
                            ),
                          )
                        }
                        placeholder="KEY"
                        className="flex-1 bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono text-[12px]"
                      />
                      <input
                        type="password"
                        value={p.v}
                        onChange={(e) =>
                          setEnvPairs((prev) =>
                            prev.map((q, idx) =>
                              idx === i ? { ...q, v: e.target.value } : q,
                            ),
                          )
                        }
                        placeholder="value"
                        className="flex-1 bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono text-[12px]"
                      />
                      <button
                        onClick={() =>
                          setEnvPairs((prev) => prev.filter((_, idx) => idx !== i))
                        }
                        className="px-2 rounded-md text-fog-400 hover:text-red-300 hover:bg-soft/[0.06]"
                        title="Remove"
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button
                    onClick={() =>
                      setEnvPairs((p) => [...p, { k: '', v: '' }])
                    }
                    className="text-[11px] text-fog-400 hover:text-fog-50"
                  >
                    + Add variable
                  </button>
                </div>
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  Endpoint URL
                </label>
                <input
                  type="url"
                  value={endpointUrl}
                  onChange={(e) => setEndpointUrl(e.target.value)}
                  placeholder="https://…"
                  className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong"
                />
              </div>
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  Auth
                </label>
                <select
                  value={authKind}
                  onChange={(e) => setAuthKind(e.target.value as any)}
                  className="bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong"
                >
                  <option value="none">None</option>
                  <option value="bearer">Bearer token</option>
                  <option value="api_key_header">API key in header</option>
                </select>
              </div>
              {authKind === 'api_key_header' && (
                <div>
                  <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                    Header name
                  </label>
                  <input
                    value={authHeader}
                    onChange={(e) => setAuthHeader(e.target.value)}
                    placeholder="Authorization"
                    className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono text-[12px]"
                  />
                </div>
              )}
              {(authKind === 'bearer' || authKind === 'api_key_header') && (
                <div>
                  <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                    {authKind === 'bearer' ? 'Bearer token' : 'Header value'}
                  </label>
                  <input
                    type="password"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                    className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono"
                  />
                </div>
              )}
            </>
          )}

          {err && (
            <div className="text-[12px] text-red-300/90 bg-red-500/10 border border-red-500/30 rounded-md px-2 py-1.5">
              {err}
            </div>
          )}

          {testResult && (
            <div
              className={`text-[12px] rounded-md px-2 py-2 border ${
                testResult.ok
                  ? 'text-emerald-300/90 bg-emerald-500/10 border-emerald-500/30'
                  : 'text-red-300/90 bg-red-500/10 border-red-500/30'
              }`}
            >
              {testResult.ok ? (
                <>
                  <div className="font-medium mb-1">
                    Connected · {testResult.tools.length} tool
                    {testResult.tools.length === 1 ? '' : 's'} discovered
                  </div>
                  <ul className="space-y-0.5 max-h-40 overflow-y-auto">
                    {testResult.tools.map((t) => (
                      <li key={t.name} className="font-mono text-[11px]">
                        • {t.name}
                        {t.description ? (
                          <span className="text-fog-400">
                            {' — '}
                            {t.description}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <>Failed: {testResult.error || 'unknown error'}</>
              )}
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-line flex items-center justify-between gap-2">
          <button
            onClick={onClose}
            disabled={saving || testing}
            className="text-sm px-3 py-1.5 rounded-md text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 disabled:opacity-40"
          >
            Cancel
          </button>
          <div className="flex gap-2">
            <button
              onClick={test}
              disabled={saving || testing}
              className="text-sm px-3 py-1.5 rounded-md border border-line text-fog-200 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              {testing ? 'Testing…' : 'Test connection'}
            </button>
            <button
              onClick={() => save(true)}
              disabled={saving || testing}
              className="text-sm px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 font-medium disabled:opacity-40"
            >
              {saving ? 'Saving…' : 'Enable'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ─── enable modal ─── */

function EnableModal({
  entry,
  server,
  onClose,
  onSaved,
}: {
  entry: CatalogEntry
  server: ServerRow | null
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const availableTransports = useMemo(
    () => Object.keys(entry.transports) as Transport[],
    [entry],
  )
  const [transport, setTransport] = useState<Transport>(
    (server?.transport as Transport) || entry.default_transport,
  )
  const tcfg = entry.transports[transport] as
    | StdioConfig
    | HttpConfig
    | undefined
  const isStdio = transport === 'stdio'

  // For HTTP-family: which auth_kind does this transport prefer?
  const httpAuth = !isStdio ? (tcfg as HttpConfig)?.auth || entry.auth : 'none'

  const [endpointUrl, setEndpointUrl] = useState<string>(
    server?.endpoint_url || (!isStdio ? (tcfg as HttpConfig)?.url_template || '' : ''),
  )
  const [secret, setSecret] = useState<string>('')
  const [envValues, setEnvValues] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{
    ok: boolean
    tools: { name: string; description: string }[]
    error: string | null
  } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  // Re-sync endpoint URL when the user flips transport in the modal.
  useEffect(() => {
    const next = entry.transports[transport]
    if (!next) return
    if (transport === 'stdio') return
    const url = (next as HttpConfig).url_template
    // Only auto-fill if the user hasn't typed something.
    if (!endpointUrl) setEndpointUrl(url || '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transport])

  async function save(enabled: boolean) {
    setSaving(true)
    setErr(null)
    try {
      const stdio = entry.transports.stdio as StdioConfig | undefined
      const envSchema = stdio?.env_schema || []
      const body: any = {
        catalog_slug: entry.slug,
        transport,
        enabled,
      }
      if (!isStdio) {
        body.endpoint_url = endpointUrl
        body.auth_kind = httpAuth
        const httpCfg = tcfg as HttpConfig | undefined
        if (httpCfg?.auth_header) body.auth_header = httpCfg.auth_header
        if (httpAuth === 'bearer' || httpAuth === 'api_key_header') {
          if (secret) body.secret = secret
        }
      } else if (envSchema.length > 0) {
        body.auth_kind = 'api_key_env'
        body.secret_env = envValues
      }

      if (server) {
        // PATCH the existing row.
        // Drop fields the user didn't touch to avoid blanking secrets.
        const patch: any = {
          transport: body.transport,
          enabled,
        }
        if (!isStdio) {
          patch.endpoint_url = body.endpoint_url
          patch.auth_kind = body.auth_kind
          if (body.auth_header) patch.auth_header = body.auth_header
          if (body.secret) patch.secret = body.secret
        } else if (Object.keys(envValues).length > 0) {
          patch.auth_kind = 'api_key_env'
          patch.secret_env = envValues
        }
        const r = await authFetch(`/api/mcp/servers/${server.id}`, {
          method: 'PATCH',
          body: JSON.stringify(patch),
        })
        if (!r.ok) {
          const b = await r.json().catch(() => ({}))
          throw new Error(b?.detail ?? `${r.status} ${r.statusText}`)
        }
      } else {
        const r = await authFetch('/api/mcp/servers', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        if (!r.ok) {
          const b = await r.json().catch(() => ({}))
          throw new Error(b?.detail ?? `${r.status} ${r.statusText}`)
        }
      }
      onSaved(`${entry.name} ${enabled ? 'enabled' : 'saved'}`)
    } catch (e: any) {
      setErr(e?.message ?? 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  // Save (disabled if new) and then call /test so the user can verify the
  // configuration before committing to "Enable". On success, the saved
  // tools list is shown inline.
  async function testConnection() {
    setTesting(true)
    setTestResult(null)
    setErr(null)
    try {
      const stdio = entry.transports.stdio as StdioConfig | undefined
      const envSchema = stdio?.env_schema || []

      let serverId = server?.id
      if (!serverId) {
        const body: any = {
          catalog_slug: entry.slug,
          transport,
          enabled: false,
        }
        if (!isStdio) {
          body.endpoint_url = endpointUrl
          body.auth_kind = httpAuth
          const httpCfg = tcfg as HttpConfig | undefined
          if (httpCfg?.auth_header) body.auth_header = httpCfg.auth_header
          if (
            (httpAuth === 'bearer' || httpAuth === 'api_key_header') &&
            secret
          ) {
            body.secret = secret
          }
        } else if (envSchema.length > 0) {
          body.auth_kind = 'api_key_env'
          body.secret_env = envValues
        }
        const r = await authFetch('/api/mcp/servers', {
          method: 'POST',
          body: JSON.stringify(body),
        })
        if (!r.ok) {
          const b = await r.json().catch(() => ({}))
          throw new Error(b?.detail ?? `${r.status} ${r.statusText}`)
        }
        const created = await r.json()
        serverId = created.id
      } else if (secret || Object.keys(envValues).length > 0) {
        // Patch in any new secret material first so the test reflects what
        // the user just typed.
        const patch: any = {}
        if (!isStdio) {
          if (secret) {
            patch.auth_kind = httpAuth
            patch.secret = secret
          }
        } else if (Object.keys(envValues).length > 0) {
          patch.auth_kind = 'api_key_env'
          patch.secret_env = envValues
        }
        if (Object.keys(patch).length > 0) {
          await authFetch(`/api/mcp/servers/${serverId}`, {
            method: 'PATCH',
            body: JSON.stringify(patch),
          })
        }
      }

      const r = await authFetch(`/api/mcp/servers/${serverId}/test`, {
        method: 'POST',
      })
      const b = await r.json().catch(() => ({}))
      setTestResult({
        ok: !!b?.ok,
        tools: Array.isArray(b?.tools) ? b.tools : [],
        error: b?.error ?? (r.ok ? null : `${r.status} ${r.statusText}`),
      })
    } catch (e: any) {
      setErr(e?.message ?? 'Test failed')
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 backdrop-blur-sm animate-float-up">
      <div className="w-full max-w-lg rounded-2xl border border-lineStrong bg-ink-100 shadow-2xl shadow-black/60 max-h-[90vh] overflow-y-auto">
        <div className="px-5 py-4 border-b border-line flex items-start justify-between">
          <div className="min-w-0">
            <h2 className="serif text-xl tracking-tighter text-fog-50">
              {server ? 'Edit ' : 'Enable '}
              {entry.name}
            </h2>
            <p className="text-xs text-fog-400 mt-0.5">by {entry.publisher}</p>
          </div>
          <button
            onClick={onClose}
            className="text-fog-400 hover:text-fog-50 w-7 h-7 rounded-full hover:bg-soft/[0.06] flex items-center justify-center"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="px-5 py-4 space-y-4 text-sm">
          <p className="text-[13px] text-fog-300 leading-relaxed">
            {entry.description}
          </p>

          {availableTransports.length > 1 && (
            <div>
              <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                Transport
              </label>
              <div className="flex gap-1.5 flex-wrap">
                {availableTransports.map((t) => (
                  <button
                    key={t}
                    onClick={() => setTransport(t)}
                    className={`text-xs px-2.5 py-1.5 rounded-md border transition ${
                      transport === t
                        ? 'border-fog-300 text-fog-50 bg-soft/[0.08]'
                        : 'border-line text-fog-400 hover:text-fog-200'
                    }`}
                  >
                    {TRANSPORT_LABEL[t] || t}
                  </button>
                ))}
              </div>
            </div>
          )}

          {isStdio && (
            <div className="text-[12px] text-fog-400">
              <div className="text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                Command
              </div>
              <div className="font-mono bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-200 truncate">
                {(tcfg as StdioConfig)?.command || ''}{' '}
                {((tcfg as StdioConfig)?.args || []).join(' ')}
              </div>
            </div>
          )}

          {!isStdio && (
            <div>
              <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                Endpoint URL
              </label>
              <input
                type="url"
                value={endpointUrl}
                onChange={(e) => setEndpointUrl(e.target.value)}
                placeholder="https://…"
                className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong"
              />
              {(tcfg as HttpConfig)?.url_help && (
                <p className="text-[11px] text-fog-500 mt-1">
                  {(tcfg as HttpConfig)?.url_help}
                </p>
              )}
            </div>
          )}

          {!isStdio &&
            (httpAuth === 'bearer' || httpAuth === 'api_key_header') && (
              <div>
                <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                  {httpAuth === 'bearer' ? 'Bearer token' : 'Header value'}
                </label>
                <input
                  type="password"
                  value={secret}
                  onChange={(e) => setSecret(e.target.value)}
                  placeholder={server?.has_secret ? '••••••• (leave empty to keep current)' : ''}
                  className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono"
                />
                <p className="text-[11px] text-fog-500 mt-1">
                  Stored encrypted on the server. Never logged or echoed.
                </p>
              </div>
            )}

          {isStdio &&
            ((entry.transports.stdio as StdioConfig).env_schema || []).map(
              (f) => (
                <div key={f.name}>
                  <label className="block text-[11px] uppercase tracking-widest text-fog-400 mb-1.5">
                    {f.label}
                    {f.required && <span className="text-amber-400">*</span>}
                  </label>
                  <input
                    type={f.secret ? 'password' : 'text'}
                    value={envValues[f.name] || ''}
                    onChange={(e) =>
                      setEnvValues((p) => ({ ...p, [f.name]: e.target.value }))
                    }
                    placeholder={
                      f.secret && server?.has_secret
                        ? '••••••• (leave empty to keep current)'
                        : ''
                    }
                    className="w-full bg-ink-200 border border-line rounded-md px-2.5 py-1.5 text-fog-100 outline-none focus:border-lineStrong font-mono text-[12px]"
                  />
                  {f.help && (
                    <p className="text-[11px] text-fog-500 mt-1">{f.help}</p>
                  )}
                </div>
              ),
            )}

          {entry.warnings && entry.warnings.length > 0 && (
            <div className="text-[11px] text-amber-300/90 bg-amber-500/10 border border-amber-500/30 rounded-md px-2 py-1.5">
              {entry.warnings.join(' · ')}
            </div>
          )}

          {err && (
            <div className="text-[12px] text-red-300/90 bg-red-500/10 border border-red-500/30 rounded-md px-2 py-1.5">
              {err}
            </div>
          )}

          {testResult && (
            <div
              className={`text-[12px] rounded-md px-2 py-2 border ${
                testResult.ok
                  ? 'text-emerald-300/90 bg-emerald-500/10 border-emerald-500/30'
                  : 'text-red-300/90 bg-red-500/10 border-red-500/30'
              }`}
            >
              {testResult.ok ? (
                <>
                  <div className="font-medium mb-1">
                    Connected · {testResult.tools.length} tool
                    {testResult.tools.length === 1 ? '' : 's'} discovered
                  </div>
                  {testResult.tools.length > 0 && (
                    <ul className="space-y-0.5 max-h-40 overflow-y-auto">
                      {testResult.tools.map((t) => (
                        <li key={t.name} className="font-mono text-[11px]">
                          • {t.name}
                          {t.description ? (
                            <span className="text-fog-400">
                              {' — '}
                              {t.description}
                            </span>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  )}
                </>
              ) : (
                <>Failed: {testResult.error || 'unknown error'}</>
              )}
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-line flex items-center justify-between gap-2">
          <button
            onClick={onClose}
            disabled={saving || testing}
            className="text-sm px-3 py-1.5 rounded-md text-fog-300 hover:bg-soft/[0.06] hover:text-fog-50 disabled:opacity-40"
          >
            Cancel
          </button>
          <div className="flex gap-2">
            <button
              onClick={testConnection}
              disabled={saving || testing}
              className="text-sm px-3 py-1.5 rounded-md border border-line text-fog-200 hover:bg-soft/[0.06] disabled:opacity-40"
            >
              {testing ? 'Testing…' : 'Test connection'}
            </button>
            {server && server.enabled && (
              <button
                onClick={() => save(false)}
                disabled={saving || testing}
                className="text-sm px-3 py-1.5 rounded-md border border-line text-fog-200 hover:bg-soft/[0.06] disabled:opacity-40"
              >
                Save & disable
              </button>
            )}
            <button
              onClick={() => save(true)}
              disabled={saving || testing}
              className="text-sm px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 font-medium disabled:opacity-40"
            >
              {saving ? 'Saving…' : server ? 'Save' : 'Enable'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ─── small bits ─── */

function Loading() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="surface p-4 h-40 animate-pulse-soft" />
      ))}
    </div>
  )
}

function ErrorBanner({ text }: { text: string }) {
  return (
    <div className="text-sm text-red-300/90 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
      {text}
    </div>
  )
}

function truncMiddle(s: string, max: number): string {
  if (s.length <= max) return s
  const half = Math.floor((max - 1) / 2)
  return `${s.slice(0, half)}…${s.slice(s.length - half)}`
}

// Old letter-tile placeholder removed — see @/components/mcp-icons.
