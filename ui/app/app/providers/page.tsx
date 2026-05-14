'use client'

import { useEffect, useMemo, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

import { authFetch, supabase } from '@/lib/supabase'

// ── Types matching the backend API ─────────────────────────────────────────

type CredentialField = {
  name: string
  label: string
  secret: boolean
  required: boolean
  placeholder: string
  help_text: string
  options: string[]
}

type CatalogEntry = {
  slug: string
  label: string
  description: string
  supports_model_listing: boolean
  suggested_models: string[]
  credential_fields: CredentialField[]
}

type Provider = {
  id: string
  slug: string
  label: string
  model_id: string
  has_credentials: boolean
  is_default: boolean
  last_error: string | null
  last_tested_at: string | null
  created_at: string | null
  updated_at: string | null
}

type ModalMode =
  | { kind: 'closed' }
  | { kind: 'add'; entry: CatalogEntry }
  | { kind: 'edit'; entry: CatalogEntry; provider: Provider }

// ── Page ───────────────────────────────────────────────────────────────────

export default function ProvidersPage() {
  const router = useRouter()
  const [ready, setReady] = useState(false)
  const [userEmail, setUserEmail] = useState<string | null>(null)

  const [catalog, setCatalog] = useState<CatalogEntry[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [modal, setModal] = useState<ModalMode>({ kind: 'closed' })
  const [toast, setToast] = useState<{ text: string; tone: 'ok' | 'err' } | null>(null)

  // ── auth + initial load ──────────────────────────────────────────────────
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

  useEffect(() => {
    if (!ready) return
    void loadAll()
  }, [ready])

  async function loadAll() {
    setLoading(true)
    setError(null)
    try {
      const [cRes, pRes] = await Promise.all([
        authFetch('/api/providers/catalog'),
        authFetch('/api/providers'),
      ])
      if (!cRes.ok) throw new Error(`Catalog: ${cRes.status}`)
      if (!pRes.ok) throw new Error(`Providers: ${pRes.status}`)
      const cData: CatalogEntry[] = await cRes.json()
      const pData: Provider[] = await pRes.json()
      setCatalog(cData)
      setProviders(pData)
    } catch (e: any) {
      setError(e?.message ?? 'Failed to load')
    } finally {
      setLoading(false)
    }
  }

  function flash(text: string, tone: 'ok' | 'err' = 'ok') {
    setToast({ text, tone })
    setTimeout(() => setToast(null), 3500)
  }

  // ── Actions ──────────────────────────────────────────────────────────────

  async function setDefault(id: string) {
    const r = await authFetch(`/api/providers/${id}/default`, { method: 'POST' })
    if (!r.ok) {
      flash('Failed to set default', 'err')
      return
    }
    flash('Default updated')
    void loadAll()
  }

  async function testProvider(id: string) {
    flash('Testing…', 'ok')
    const r = await authFetch(`/api/providers/${id}/test`, { method: 'POST' })
    const data = await r.json().catch(() => ({}))
    if (!r.ok) {
      flash(data?.detail ?? `Test failed (${r.status})`, 'err')
    } else if (data?.ok) {
      flash('Credentials OK')
    } else {
      flash(data?.error ?? 'Credentials failed', 'err')
    }
    void loadAll()
  }

  async function deleteProvider(id: string, label: string) {
    if (!confirm(`Delete provider "${label}"? This cannot be undone.`)) return
    const r = await authFetch(`/api/providers/${id}`, { method: 'DELETE' })
    if (!r.ok) {
      flash('Delete failed', 'err')
      return
    }
    flash('Deleted')
    void loadAll()
  }

  function openAdd(entry: CatalogEntry) {
    setModal({ kind: 'add', entry })
  }

  function openEdit(provider: Provider) {
    const entry = catalog.find((c) => c.slug === provider.slug)
    if (!entry) {
      flash(`Provider type ${provider.slug} is no longer supported`, 'err')
      return
    }
    setModal({ kind: 'edit', entry, provider })
  }

  async function saveProvider(payload: {
    slug: string
    label: string
    model_id: string
    credentials: Record<string, string> | null
    is_default: boolean
    id?: string
  }): Promise<{ ok: boolean; error?: string }> {
    const body: any = {
      label: payload.label,
      model_id: payload.model_id,
      verify: true,
    }
    if (payload.credentials !== null) body.credentials = payload.credentials

    let r: Response
    if (payload.id) {
      r = await authFetch(`/api/providers/${payload.id}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      })
    } else {
      body.slug = payload.slug
      body.is_default = payload.is_default
      body.credentials = payload.credentials ?? {}
      r = await authFetch('/api/providers', {
        method: 'POST',
        body: JSON.stringify(body),
      })
    }

    if (!r.ok) {
      const j = await r.json().catch(() => ({}))
      return { ok: false, error: j?.detail ?? `${r.status} ${r.statusText}` }
    }
    // If editing AND user toggled is_default on, flip it after save.
    if (payload.id && payload.is_default) {
      await authFetch(`/api/providers/${payload.id}/default`, { method: 'POST' })
    }
    return { ok: true }
  }

  // ── Render ───────────────────────────────────────────────────────────────

  const groupedConfigured = useMemo(() => {
    const out: Record<string, Provider[]> = {}
    for (const p of providers) {
      out[p.slug] = out[p.slug] || []
      out[p.slug].push(p)
    }
    return out
  }, [providers])

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
          <h1 className="text-white text-sm font-medium">LLM Providers</h1>
        </div>
        <span className="text-fog-400 text-xs">{userEmail}</span>
      </header>

      <main className="flex-1 overflow-auto px-6 py-8">
        <div className="mx-auto max-w-4xl">
          {error && (
            <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-300">
              {error}
            </div>
          )}
          {loading && (
            <p className="text-fog-400 text-sm">Loading providers…</p>
          )}

          {!loading && (
            <>
              <section className="mb-10">
                <h2 className="text-[12px] uppercase tracking-widest text-fog-400 mb-3">
                  Your configured providers
                </h2>
                {providers.length === 0 ? (
                  <p className="text-fog-500 text-sm">
                    None yet — pick one below to add.
                  </p>
                ) : (
                  <div className="space-y-2">
                    {providers.map((p) => (
                      <ProviderRow
                        key={p.id}
                        provider={p}
                        catalogLabel={
                          catalog.find((c) => c.slug === p.slug)?.label ?? p.slug
                        }
                        onEdit={() => openEdit(p)}
                        onTest={() => testProvider(p.id)}
                        onSetDefault={() => setDefault(p.id)}
                        onDelete={() => deleteProvider(p.id, p.label)}
                      />
                    ))}
                  </div>
                )}
              </section>

              <section>
                <h2 className="text-[12px] uppercase tracking-widest text-fog-400 mb-3">
                  Available providers
                </h2>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  {catalog.map((entry) => (
                    <CatalogCard
                      key={entry.slug}
                      entry={entry}
                      configuredCount={(groupedConfigured[entry.slug] || []).length}
                      onAdd={() => openAdd(entry)}
                    />
                  ))}
                </div>
              </section>
            </>
          )}
        </div>
      </main>

      {modal.kind !== 'closed' && (
        <ProviderModal
          mode={modal}
          onClose={() => setModal({ kind: 'closed' })}
          onSaved={(message) => {
            setModal({ kind: 'closed' })
            flash(message)
            void loadAll()
          }}
          save={saveProvider}
        />
      )}

      {toast && (
        <div
          className={`fixed bottom-6 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-full text-sm shadow-2xl ${
            toast.tone === 'ok'
              ? 'bg-emerald-500/90 text-white'
              : 'bg-red-500/90 text-white'
          }`}
        >
          {toast.text}
        </div>
      )}
    </div>
  )
}

// ── Configured row ────────────────────────────────────────────────────────

function ProviderRow({
  provider,
  catalogLabel,
  onEdit,
  onTest,
  onSetDefault,
  onDelete,
}: {
  provider: Provider
  catalogLabel: string
  onEdit: () => void
  onTest: () => void
  onSetDefault: () => void
  onDelete: () => void
}) {
  return (
    <div className="rounded-xl border border-line bg-ink-100 p-4 flex items-center gap-4">
      <button
        onClick={onSetDefault}
        title={
          provider.is_default
            ? 'Already the default'
            : 'Set as default for new chats'
        }
        className={`shrink-0 w-9 h-9 rounded-lg border ${
          provider.is_default
            ? 'border-yellow-500/60 bg-yellow-500/10 text-yellow-300'
            : 'border-line text-fog-400 hover:border-lineStrong'
        } flex items-center justify-center text-lg`}
      >
        {provider.is_default ? '★' : '☆'}
      </button>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-1">
          <span className="text-white font-medium truncate">{provider.label}</span>
          <span className="text-[10px] uppercase tracking-wider text-fog-500 bg-white/[0.04] rounded px-1.5 py-0.5">
            {catalogLabel}
          </span>
          {!provider.has_credentials && (
            <span className="text-[10px] uppercase tracking-wider text-red-300 bg-red-500/10 rounded px-1.5 py-0.5">
              no credentials
            </span>
          )}
        </div>
        <div className="text-fog-300 text-[12px] font-mono truncate">
          {provider.model_id}
        </div>
        {provider.last_error && (
          <div className="mt-1 text-[12px] text-red-300 truncate">
            {provider.last_error}
          </div>
        )}
      </div>

      <div className="flex items-center gap-1 shrink-0">
        <button
          onClick={onTest}
          className="px-2.5 py-1 rounded text-xs text-fog-300 hover:bg-white/[0.06] hover:text-white"
        >
          Test
        </button>
        <button
          onClick={onEdit}
          className="px-2.5 py-1 rounded text-xs text-fog-300 hover:bg-white/[0.06] hover:text-white"
        >
          Edit
        </button>
        <button
          onClick={onDelete}
          className="px-2.5 py-1 rounded text-xs text-fog-300 hover:bg-red-500/15 hover:text-red-300"
        >
          Delete
        </button>
      </div>
    </div>
  )
}

// ── Catalog card ──────────────────────────────────────────────────────────

function CatalogCard({
  entry,
  configuredCount,
  onAdd,
}: {
  entry: CatalogEntry
  configuredCount: number
  onAdd: () => void
}) {
  return (
    <div className="rounded-xl border border-line bg-ink-100 p-4 flex flex-col">
      <div className="flex items-start justify-between gap-3 mb-2">
        <div>
          <h3 className="text-white font-medium">{entry.label}</h3>
          {configuredCount > 0 && (
            <p className="text-[11px] text-fog-500 mt-0.5">
              {configuredCount} configured
            </p>
          )}
        </div>
        <button
          onClick={onAdd}
          className="shrink-0 px-3 py-1.5 rounded-lg bg-white/[0.06] text-fog-100 hover:bg-white/[0.1] text-xs font-medium"
        >
          + Add
        </button>
      </div>
      <p className="text-fog-400 text-[12.5px] leading-relaxed">
        {entry.description}
      </p>
    </div>
  )
}

// ── Add / Edit modal ──────────────────────────────────────────────────────

function ProviderModal({
  mode,
  onClose,
  onSaved,
  save,
}: {
  mode: Exclude<ModalMode, { kind: 'closed' }>
  onClose: () => void
  onSaved: (message: string) => void
  save: (payload: {
    slug: string
    label: string
    model_id: string
    credentials: Record<string, string> | null
    is_default: boolean
    id?: string
  }) => Promise<{ ok: boolean; error?: string }>
}) {
  const isEdit = mode.kind === 'edit'
  const entry = mode.entry
  const existing = isEdit ? mode.provider : null

  const [label, setLabel] = useState(existing?.label ?? `My ${entry.label}`)
  const [modelId, setModelId] = useState(existing?.model_id ?? '')
  const [creds, setCreds] = useState<Record<string, string>>({})
  const [replaceCreds, setReplaceCreds] = useState(!isEdit)
  const [isDefault, setIsDefault] = useState(existing?.is_default ?? false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Seed with the catalog's curated shortlist so the dropdown is useful
  // immediately. "Fetch available" can override with the provider's live list.
  const [availableModels, setAvailableModels] = useState<string[]>(
    entry.suggested_models ?? [],
  )
  const [fetchingModels, setFetchingModels] = useState(false)
  const [fetchError, setFetchError] = useState<string | null>(null)

  // Required credentials are present when every required field is non-empty.
  const credsLookSufficient =
    entry.credential_fields.every(
      (f) => !f.required || (creds[f.name] || '').trim(),
    )

  // Can we fetch models?
  //   - In edit mode without "Replace credentials" ticked → use stored
  //     credentials via POST /providers/{id}/models (no creds in request body).
  //   - Otherwise → use POST /providers/discover-models with the typed creds.
  const useStoredCreds =
    isEdit && existing != null && !replaceCreds && existing.has_credentials
  const canFetchModels =
    entry.supports_model_listing &&
    (useStoredCreds || (replaceCreds && credsLookSufficient))

  // Tracks whether the current `availableModels` came from a live fetch
  // (vs the catalog's curated shortlist seed). Used only for the caption.
  const [fetchedLive, setFetchedLive] = useState(false)

  async function handleFetchModels() {
    setFetchingModels(true)
    setFetchError(null)
    try {
      let r: Response
      if (useStoredCreds) {
        r = await authFetch(`/api/providers/${existing!.id}/models`, {
          method: 'POST',
        })
      } else {
        r = await authFetch('/api/providers/discover-models', {
          method: 'POST',
          body: JSON.stringify({ slug: entry.slug, credentials: creds }),
        })
      }
      const data = await r.json().catch(() => ({}))
      if (!r.ok) {
        setFetchError(data?.detail ?? `${r.status} ${r.statusText}`)
        return
      }
      const list: string[] = Array.isArray(data?.models) ? data.models : []
      if (list.length > 0) {
        setAvailableModels(list)
        setFetchedLive(true)
      }
      if (data?.error) {
        setFetchError(data.error)
      } else if (list.length === 0) {
        setFetchError(
          'Provider returned an empty list. You can still type a model ID.',
        )
      }
    } catch (e: any) {
      setFetchError(e?.message ?? 'Fetch failed')
    } finally {
      setFetchingModels(false)
    }
  }

  async function handleSubmit() {
    setErr(null)
    setBusy(true)
    const result = await save({
      slug: entry.slug,
      label: label.trim(),
      model_id: modelId.trim(),
      credentials: replaceCreds ? creds : null,
      is_default: isDefault,
      id: existing?.id,
    })
    setBusy(false)
    if (!result.ok) {
      setErr(result.error ?? 'Save failed')
      return
    }
    onSaved(isEdit ? 'Saved' : 'Added')
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[min(560px,94vw)] max-h-[90vh] overflow-auto rounded-2xl border border-lineStrong bg-ink-200 p-6 shadow-2xl shadow-black/60"
      >
        <h3 className="text-white text-base font-medium mb-1">
          {isEdit ? `Edit ${entry.label} provider` : `Add ${entry.label} provider`}
        </h3>
        <p className="text-fog-400 text-[12.5px] leading-relaxed mb-5">
          {entry.description}
        </p>

        {/* Label */}
        <FormField label="Nickname" hint="What you'll see in the chat picker.">
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={`My ${entry.label}`}
            className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40"
          />
        </FormField>

        {/* Model ID */}
        <FormField
          label="Model ID"
          hint={
            entry.slug === 'azure'
              ? 'Your Azure deployment name (NOT the underlying model name).'
              : 'e.g. gpt-4o-mini, claude-haiku-4-5, gemini-2.5-pro'
          }
        >
          <div className="flex items-stretch gap-2">
            <input
              type="text"
              value={modelId}
              onChange={(e) => setModelId(e.target.value)}
              placeholder="model-id"
              list={availableModels.length ? 'available-models' : undefined}
              className="flex-1 bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40 font-mono"
            />
            {entry.supports_model_listing && (
              <button
                type="button"
                onClick={handleFetchModels}
                disabled={!canFetchModels || fetchingModels}
                title={
                  canFetchModels
                    ? 'Fetch the live list from the provider'
                    : 'Enter the credentials first'
                }
                className="px-3 py-2 rounded-lg border border-line text-xs text-fog-200 hover:bg-soft/[0.06] hover:text-white disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap"
              >
                {fetchingModels ? 'Fetching…' : 'Fetch available'}
              </button>
            )}
          </div>
          {availableModels.length > 0 && (
            <datalist id="available-models">
              {availableModels.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          )}
          {availableModels.length > 0 && (
            <p className="text-[11.5px] text-emerald-300 mt-1.5">
              {availableModels.length} model{availableModels.length === 1 ? '' : 's'}
              {fetchedLive ? ' (live)' : ' (suggested)'} — pick from the dropdown or type your own.
            </p>
          )}
          {fetchError && (
            <p className="text-[11.5px] text-red-300 mt-1.5">{fetchError}</p>
          )}
        </FormField>

        {/* Credentials */}
        <div className="mb-4">
          <div className="flex items-baseline justify-between mb-2">
            <span className="text-fog-200 text-sm">Credentials</span>
            {isEdit && existing?.has_credentials && (
              <label className="text-[12px] text-fog-400 flex items-center gap-1.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={replaceCreds}
                  onChange={(e) => setReplaceCreds(e.target.checked)}
                  className="accent-white"
                />
                Replace stored credentials
              </label>
            )}
          </div>
          {!replaceCreds && isEdit && existing?.has_credentials && (
            <p className="text-[12px] text-fog-500 mb-2">
              Credentials on file. Tick "Replace" above to change them.
            </p>
          )}
          {replaceCreds && (
            <div className="space-y-3">
              {entry.credential_fields.map((f) => (
                <CredentialInput
                  key={f.name}
                  field={f}
                  value={creds[f.name] ?? ''}
                  onChange={(v) =>
                    setCreds((prev) => ({ ...prev, [f.name]: v }))
                  }
                />
              ))}
            </div>
          )}
        </div>

        {/* Default */}
        {!isEdit && (
          <label className="flex items-center gap-2 text-fog-200 text-sm mb-5 cursor-pointer">
            <input
              type="checkbox"
              checked={isDefault}
              onChange={(e) => setIsDefault(e.target.checked)}
              className="accent-white"
            />
            Set as default for new chats
          </label>
        )}
        {isEdit && (
          <label className="flex items-center gap-2 text-fog-200 text-sm mb-5 cursor-pointer">
            <input
              type="checkbox"
              checked={isDefault}
              onChange={(e) => setIsDefault(e.target.checked)}
              className="accent-white"
            />
            Make default (clears the flag on others)
          </label>
        )}

        {err && (
          <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-[12.5px] text-red-300 whitespace-pre-wrap">
            {err}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 rounded-lg text-sm text-fog-200 hover:bg-white/[0.06] disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={
              busy ||
              !label.trim() ||
              !modelId.trim() ||
              (replaceCreds &&
                entry.credential_fields.some(
                  (f) => f.required && !(creds[f.name] || '').trim(),
                ))
            }
            className="px-4 py-2 rounded-lg text-sm font-medium bg-white text-black hover:bg-white/90 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {busy ? 'Verifying…' : isEdit ? 'Save' : 'Add provider'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Small form helpers ────────────────────────────────────────────────────

function FormField({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="mb-4">
      <label className="block text-sm text-fog-200 mb-1.5">{label}</label>
      {children}
      {hint && <p className="text-[11.5px] text-fog-500 mt-1.5">{hint}</p>}
    </div>
  )
}

function CredentialInput({
  field,
  value,
  onChange,
}: {
  field: CredentialField
  value: string
  onChange: (v: string) => void
}) {
  const isSelect = field.options && field.options.length > 0
  return (
    <div>
      <label className="block text-[12.5px] text-fog-300 mb-1">
        {field.label}
        {!field.required && (
          <span className="text-fog-500 ml-1">(optional)</span>
        )}
      </label>
      {isSelect ? (
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40"
        >
          <option value="">— select —</option>
          {field.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      ) : (
        <input
          type={field.secret ? 'password' : 'text'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          className="w-full bg-ink-300 border border-lineStrong rounded-lg px-3 py-2 text-sm text-white outline-none focus:border-white/40 font-mono"
        />
      )}
      {field.help_text && (
        <p className="text-[11.5px] text-fog-500 mt-1">{field.help_text}</p>
      )}
    </div>
  )
}
