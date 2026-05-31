'use client'

/**
 * Context-management profile manager.
 *
 * Two tabs:
 *   - Presets:  read-only built-ins (minimal, code_mode, long_chat,
 *               power_research, cheap_long). One-click "Use this".
 *   - Yours:    user-saved profiles with edit + delete + set-default.
 *
 * "New profile" opens a form with one collapsible section per technique;
 * each section toggles the technique on/off and exposes its knobs with
 * defaults pre-filled. The full Profile JSON is also shown (read-only)
 * so power users can see exactly what they're submitting.
 */

import { useEffect, useMemo, useState } from 'react'
import { authFetch } from '@/lib/supabase'

type Profile = {
  id: string
  user_id: string | null
  name: string
  body: any
  is_default: boolean
  built_in: boolean
  summary: string | null
  created_at: string | null
  updated_at: string | null
}

type Props = {
  onPickProfile: (id: string) => void
  onListChanged: () => void
}

const DEFAULT_BODY: any = {
  tool_surface: 'tool_calling',
  context_management: {
    tool_result_trimming: { enabled: false, trigger_tokens: 20000, keep_recent: 4 },
    summarization: { enabled: false, trigger_tokens: 50000, keep_recent: 6 },
    memory: { enabled: false, scope: 'thread', auto_view_at_start: true },
    subagent: { enabled: false, max_depth: 1, token_budget: 20000, parallel_limit: 3, inherit_memory: false },
    jit_tools: { enabled: false },
    sliding_window: { enabled: false, keep_recent: 12 },
  },
}

export function ContextProfilesPanel({ onPickProfile, onListChanged }: Props) {
  const [tab, setTab] = useState<'presets' | 'yours'>('presets')
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editorOpen, setEditorOpen] = useState<{ profile?: Profile } | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const r = await authFetch('/api/context/profiles')
      if (!r.ok) {
        setError(`Failed to load: ${r.status} ${r.statusText}`)
        return
      }
      const data = await r.json()
      setProfiles(Array.isArray(data?.profiles) ? data.profiles : [])
    } catch (e: any) {
      setError(e?.message ?? 'Network error')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
  }, [])

  useEffect(() => {
    if (!toast) return
    const t = setTimeout(() => setToast(null), 2200)
    return () => clearTimeout(t)
  }, [toast])

  async function setDefault(p: Profile) {
    const r = await authFetch(`/api/context/profiles/${p.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ is_default: true }),
    })
    if (!r.ok) {
      setToast(`Set default failed: ${r.status}`)
      return
    }
    setToast(`Default → ${p.name}`)
    await refresh()
    onListChanged()
  }

  async function deleteProfile(p: Profile) {
    if (!confirm(`Delete profile '${p.name}'? This cannot be undone.`)) return
    const r = await authFetch(`/api/context/profiles/${p.id}`, { method: 'DELETE' })
    if (!r.ok) {
      const body = await r.json().catch(() => ({}))
      setToast(`Delete failed: ${body?.detail ?? r.statusText}`)
      return
    }
    setToast(`Deleted ${p.name}`)
    await refresh()
    onListChanged()
  }

  const builtins = useMemo(() => profiles.filter((p) => p.built_in), [profiles])
  const owned = useMemo(() => profiles.filter((p) => !p.built_in), [profiles])

  return (
    <div className="flex flex-col h-full min-h-0 bg-ink-50 text-fog-100">
      <header className="h-12 px-5 border-b border-line flex items-center justify-between shrink-0">
        <span className="text-sm text-fog-50 font-medium">Context Profiles</span>
        <button
          onClick={() => setEditorOpen({})}
          className="text-xs px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 transition font-medium"
        >
          + New profile
        </button>
      </header>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-5xl px-6 py-8">
          <div className="mb-6">
            <h1 className="serif text-3xl tracking-tighter text-fog-50">
              Choose how the agent manages context
            </h1>
            <p className="text-sm text-fog-400 mt-1.5 max-w-3xl">
              A profile bundles the tool surface (classic tool-calling vs.
              TypeScript Code Mode) with the context-management techniques
              (trim, summarise, memory, sub-agent, sliding window). Pick a
              built-in preset or build your own.
            </p>
          </div>

          <div className="flex items-center gap-1 mb-4 border-b border-line">
            <TabButton
              active={tab === 'presets'}
              onClick={() => setTab('presets')}
              label="Presets"
              count={builtins.length}
            />
            <TabButton
              active={tab === 'yours'}
              onClick={() => setTab('yours')}
              label="Yours"
              count={owned.length}
            />
          </div>

          {loading && <div className="text-fog-400 text-sm">Loading…</div>}
          {error && (
            <div className="text-red-400 text-sm bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
              {error}
            </div>
          )}

          {!loading && !error && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {(tab === 'presets' ? builtins : owned).map((p) => (
                <ProfileCard
                  key={p.id}
                  profile={p}
                  onUse={() => {
                    onPickProfile(p.id)
                    setToast(`Active → ${p.name}`)
                  }}
                  onSetDefault={p.built_in ? undefined : () => setDefault(p)}
                  onEdit={p.built_in ? undefined : () => setEditorOpen({ profile: p })}
                  onDelete={p.built_in ? undefined : () => deleteProfile(p)}
                />
              ))}
            </div>
          )}
          {!loading && !error && tab === 'yours' && owned.length === 0 && (
            <div className="surface px-6 py-10 text-center">
              <p className="text-sm text-fog-300">
                You haven't built any custom profiles yet.
              </p>
              <button
                onClick={() => setEditorOpen({})}
                className="mt-3 text-xs px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 transition"
              >
                Start one from defaults
              </button>
            </div>
          )}
        </div>
      </div>

      {editorOpen && (
        <ProfileEditor
          profile={editorOpen.profile}
          onClose={() => setEditorOpen(null)}
          onSaved={async (msg) => {
            setEditorOpen(null)
            setToast(msg)
            await refresh()
            onListChanged()
          }}
        />
      )}

      {toast && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-[60] px-4 py-2 rounded-full bg-accent text-ink-50 text-sm shadow-2xl">
          {toast}
        </div>
      )}
    </div>
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
      className={`px-3 py-2 text-sm border-b-2 -mb-px transition ${
        active
          ? 'border-accent text-fog-50'
          : 'border-transparent text-fog-400 hover:text-fog-200'
      }`}
    >
      {label}
      <span className="ml-1.5 text-[11px] text-fog-500">({count})</span>
    </button>
  )
}

function ProfileCard({
  profile,
  onUse,
  onSetDefault,
  onEdit,
  onDelete,
}: {
  profile: Profile
  onUse: () => void
  onSetDefault?: () => void
  onEdit?: () => void
  onDelete?: () => void
}) {
  const cm = profile.body?.context_management ?? {}
  const on: string[] = []
  if (cm.tool_result_trimming?.enabled) on.push('trim')
  if (cm.summarization?.enabled) on.push('summarise')
  if (cm.memory?.enabled) on.push('memory')
  if (cm.subagent?.enabled) on.push('subagent')
  if (cm.jit_tools?.enabled) on.push('jit')
  if (cm.sliding_window?.enabled) on.push('sliding')
  return (
    <div className="surface p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-fog-50">
          {profile.name}
          {profile.is_default && (
            <span className="text-[10px] text-amber-400 ml-2">★ default</span>
          )}
          {profile.built_in && (
            <span className="text-[10px] text-fog-500 ml-2">built-in</span>
          )}
        </h3>
      </div>
      <div className="text-[12px] text-fog-300">{profile.summary || '—'}</div>
      <div className="text-[11px] font-mono text-fog-400">
        <span className="text-fog-500">tool_surface:</span>{' '}
        <span className="text-fog-100">{profile.body?.tool_surface}</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {on.length === 0 ? (
          <span className="chip text-[11px] py-0.5 text-fog-500">no extras</span>
        ) : (
          on.map((flag) => (
            <span key={flag} className="chip text-[11px] py-0.5">
              {flag}
            </span>
          ))
        )}
      </div>
      <div className="flex flex-wrap gap-2 mt-1">
        <button
          onClick={onUse}
          className="text-xs px-3 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 transition"
        >
          Use this
        </button>
        {onSetDefault && (
          <button
            onClick={onSetDefault}
            className="text-xs px-3 py-1.5 rounded-md text-fog-200 hover:bg-soft/[0.08]"
          >
            Set default
          </button>
        )}
        {onEdit && (
          <button
            onClick={onEdit}
            className="text-xs px-3 py-1.5 rounded-md text-fog-200 hover:bg-soft/[0.08]"
          >
            Edit
          </button>
        )}
        {onDelete && (
          <button
            onClick={onDelete}
            className="text-xs px-3 py-1.5 rounded-md text-red-300 hover:bg-red-500/10"
          >
            Delete
          </button>
        )}
      </div>
    </div>
  )
}

/* ─── editor ─── */

function ProfileEditor({
  profile,
  onClose,
  onSaved,
}: {
  profile?: Profile
  onClose: () => void
  onSaved: (msg: string) => void
}) {
  const isEdit = !!profile
  const [name, setName] = useState(profile?.name ?? '')
  const [body, setBody] = useState<any>(
    JSON.parse(JSON.stringify(profile?.body ?? DEFAULT_BODY)),
  )
  const [isDefault, setIsDefault] = useState(profile?.is_default ?? false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  function patchBody(path: string[], value: any) {
    setBody((prev: any) => {
      const next = JSON.parse(JSON.stringify(prev))
      let cur = next
      for (let i = 0; i < path.length - 1; i++) {
        cur[path[i]] = cur[path[i]] ?? {}
        cur = cur[path[i]]
      }
      cur[path[path.length - 1]] = value
      return next
    })
  }

  async function save() {
    setError(null)
    setSaving(true)
    try {
      if (!name.trim()) {
        setError('Name is required.')
        return
      }
      const url = isEdit
        ? `/api/context/profiles/${profile!.id}`
        : '/api/context/profiles'
      const method = isEdit ? 'PATCH' : 'POST'
      const payload: any = isEdit
        ? { name, body, is_default: isDefault }
        : { name, body, is_default: isDefault }
      const r = await authFetch(url, {
        method,
        body: JSON.stringify(payload),
      })
      if (!r.ok) {
        const b = await r.json().catch(() => ({}))
        setError(b?.detail ?? `${r.status} ${r.statusText}`)
        return
      }
      onSaved(isEdit ? `Updated ${name}` : `Created ${name}`)
    } catch (e: any) {
      setError(e?.message ?? 'Network error')
    } finally {
      setSaving(false)
    }
  }

  const cm = body?.context_management ?? {}

  return (
    <div
      className="fixed inset-0 z-[70] flex justify-end bg-black/40"
      onClick={onClose}
    >
      <aside
        onClick={(e) => e.stopPropagation()}
        className="w-[min(720px,92vw)] h-full bg-ink-200 border-l border-lineStrong flex flex-col shadow-2xl"
      >
        <header className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-sm text-fog-50 font-medium">
            {isEdit ? `Edit profile · ${profile!.name}` : 'New profile'}
          </div>
          <button
            onClick={onClose}
            className="w-7 h-7 rounded hover:bg-soft/[0.06] text-fog-300 flex items-center justify-center"
            aria-label="Close"
          >
            ✕
          </button>
        </header>

        <div className="flex-1 overflow-auto px-5 py-4 space-y-5 text-sm">
          <div>
            <label className="text-[11px] uppercase tracking-widest text-fog-500 block mb-1">
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. my-long-debug"
              className="w-full bg-ink-300 border border-line rounded-md px-3 py-1.5 text-sm text-fog-100"
            />
          </div>

          <div>
            <label className="text-[11px] uppercase tracking-widest text-fog-500 block mb-1">
              Tool surface
            </label>
            <select
              value={body?.tool_surface}
              onChange={(e) => patchBody(['tool_surface'], e.target.value)}
              className="bg-ink-300 border border-line rounded-md px-3 py-1.5 text-sm text-fog-100"
            >
              <option value="tool_calling">tool_calling (classic ReAct)</option>
              <option value="ts_code_mode">ts_code_mode (TypeScript Code Mode)</option>
            </select>
          </div>

          <TechniqueSection
            label="Tool-result trimming"
            note="Replace old tool outputs with a placeholder past a threshold. Cheap, mechanical."
            enabled={!!cm.tool_result_trimming?.enabled}
            onToggle={(v) => patchBody(['context_management', 'tool_result_trimming', 'enabled'], v)}
          >
            <NumberKnob
              label="Trigger tokens"
              value={cm.tool_result_trimming?.trigger_tokens ?? 20000}
              onChange={(v) => patchBody(['context_management', 'tool_result_trimming', 'trigger_tokens'], v)}
            />
            <NumberKnob
              label="Keep recent tool calls"
              value={cm.tool_result_trimming?.keep_recent ?? 4}
              onChange={(v) => patchBody(['context_management', 'tool_result_trimming', 'keep_recent'], v)}
            />
          </TechniqueSection>

          <TechniqueSection
            label="Summarisation"
            note="LLM-compacts old turns when the total goes past a threshold."
            enabled={!!cm.summarization?.enabled}
            onToggle={(v) => patchBody(['context_management', 'summarization', 'enabled'], v)}
          >
            <NumberKnob
              label="Trigger tokens"
              value={cm.summarization?.trigger_tokens ?? 50000}
              onChange={(v) => patchBody(['context_management', 'summarization', 'trigger_tokens'], v)}
            />
            <NumberKnob
              label="Keep recent messages"
              value={cm.summarization?.keep_recent ?? 6}
              onChange={(v) => patchBody(['context_management', 'summarization', 'keep_recent'], v)}
            />
            <TextKnob
              label="Summariser model (optional)"
              value={cm.summarization?.summariser_model ?? ''}
              onChange={(v) => patchBody(['context_management', 'summarization', 'summariser_model'], v || null)}
              placeholder="leave empty to reuse chat model"
            />
          </TechniqueSection>

          <TechniqueSection
            label="Memory tool"
            note="Notes stored outside the context window. Survives summarisation + restarts."
            enabled={!!cm.memory?.enabled}
            onToggle={(v) => patchBody(['context_management', 'memory', 'enabled'], v)}
          >
            <SelectKnob
              label="Scope"
              value={cm.memory?.scope ?? 'thread'}
              options={['thread', 'user']}
              onChange={(v) => patchBody(['context_management', 'memory', 'scope'], v)}
            />
            <BoolKnob
              label="Auto-view at turn start"
              value={cm.memory?.auto_view_at_start ?? true}
              onChange={(v) => patchBody(['context_management', 'memory', 'auto_view_at_start'], v)}
            />
          </TechniqueSection>

          <TechniqueSection
            label="Sub-agent delegation"
            note="Spawn a fresh-context helper that returns a short summary. Can 5-10× the token bill if misused."
            enabled={!!cm.subagent?.enabled}
            onToggle={(v) => patchBody(['context_management', 'subagent', 'enabled'], v)}
          >
            <NumberKnob
              label="Max depth"
              value={cm.subagent?.max_depth ?? 1}
              onChange={(v) => patchBody(['context_management', 'subagent', 'max_depth'], v)}
            />
            <NumberKnob
              label="Token budget per spawn"
              value={cm.subagent?.token_budget ?? 20000}
              onChange={(v) => patchBody(['context_management', 'subagent', 'token_budget'], v)}
            />
          </TechniqueSection>

          <TechniqueSection
            label="Just-in-time tools"
            note="head/tail/grep/find primitives (already on baseline). This flag is informational."
            enabled={!!cm.jit_tools?.enabled}
            onToggle={(v) => patchBody(['context_management', 'jit_tools', 'enabled'], v)}
          />

          <TechniqueSection
            label="Sliding window"
            note="Drop middle messages if the count exceeds N. Safety net for tiny-context models."
            enabled={!!cm.sliding_window?.enabled}
            onToggle={(v) => patchBody(['context_management', 'sliding_window', 'enabled'], v)}
          >
            <NumberKnob
              label="Keep recent messages"
              value={cm.sliding_window?.keep_recent ?? 12}
              onChange={(v) => patchBody(['context_management', 'sliding_window', 'keep_recent'], v)}
            />
          </TechniqueSection>

          <BoolKnob
            label="Make this my default profile"
            value={isDefault}
            onChange={setIsDefault}
          />

          <details className="text-[11px] text-fog-500">
            <summary className="cursor-pointer hover:text-fog-300">Raw JSON</summary>
            <pre className="mt-2 bg-ink-300 border border-line rounded-md p-3 overflow-auto text-fog-100 max-h-72">
              {JSON.stringify(body, null, 2)}
            </pre>
          </details>

          {error && (
            <div className="text-red-300 text-xs bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
              {error}
            </div>
          )}
        </div>

        <footer className="px-4 py-3 border-t border-line flex justify-end gap-2">
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-md text-fog-300 hover:bg-soft/[0.06]"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving}
            className="text-xs px-4 py-1.5 rounded-md bg-accent text-ink-50 hover:bg-accent/90 disabled:opacity-50"
          >
            {saving ? 'Saving…' : isEdit ? 'Save changes' : 'Create profile'}
          </button>
        </footer>
      </aside>
    </div>
  )
}

function TechniqueSection({
  label,
  note,
  enabled,
  onToggle,
  children,
}: {
  label: string
  note: string
  enabled: boolean
  onToggle: (v: boolean) => void
  children?: React.ReactNode
}) {
  return (
    <div className="border border-line rounded-md p-3 bg-ink-300">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm text-fog-100">{label}</div>
          <div className="text-[11px] text-fog-500 mt-0.5">{note}</div>
        </div>
        <label className="inline-flex items-center gap-2 cursor-pointer shrink-0">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => onToggle(e.target.checked)}
          />
          <span className="text-[11px] text-fog-400">
            {enabled ? 'on' : 'off'}
          </span>
        </label>
      </div>
      {enabled && children && (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">{children}</div>
      )}
    </div>
  )
}

function NumberKnob({
  label,
  value,
  onChange,
}: {
  label: string
  value: number
  onChange: (n: number) => void
}) {
  return (
    <label className="flex flex-col text-[11px] text-fog-400">
      {label}
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(parseInt(e.target.value || '0', 10) || 0)}
        className="mt-0.5 bg-ink-200 border border-line rounded px-2 py-1 text-fog-100"
      />
    </label>
  )
}

function TextKnob({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string
  value: string
  onChange: (s: string) => void
  placeholder?: string
}) {
  return (
    <label className="flex flex-col text-[11px] text-fog-400">
      {label}
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className="mt-0.5 bg-ink-200 border border-line rounded px-2 py-1 text-fog-100"
      />
    </label>
  )
}

function BoolKnob({
  label,
  value,
  onChange,
}: {
  label: string
  value: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <label className="inline-flex items-center gap-2 text-[11px] text-fog-400">
      <input
        type="checkbox"
        checked={value}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  )
}

function SelectKnob({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  onChange: (v: string) => void
}) {
  return (
    <label className="flex flex-col text-[11px] text-fog-400">
      {label}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-0.5 bg-ink-200 border border-line rounded px-2 py-1 text-fog-100"
      >
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  )
}
