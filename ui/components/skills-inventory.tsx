'use client'

// Skills — claude.ai-style "+" → Skills.
//
// A "skill" is a named set of expert instructions. Toggling it on folds those
// instructions into the agent's system prompt so it handles that kind of task
// better while chatting (no code; uses the agent's existing tools).
//
// Two surfaces share this file:
//   * SkillsComposerFlyout — the compact submenu that opens from the chat
//     composer "+" menu: a toggle per skill + "Manage skills" + "Add skill".
//   * SkillsInventoryPanel  — the full manage page (cards, edit, delete, new).
// Both drive /api/skills through the shared useSkills hook.

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { authFetch } from '@/lib/supabase'

/* ─── types ─── */

export type Skill = {
  ref: string
  slug: string | null
  name: string
  description: string
  instructions: string
  icon: string
  is_builtin: boolean
  is_custom: boolean
  enabled: boolean
}

type DraftSkill = {
  name: string
  description: string
  instructions: string
}

/* ─── shared data hook ─── */

function useSkills() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  async function refresh() {
    try {
      const r = await authFetch('/api/skills')
      if (!r.ok) {
        setError(`Failed to load skills: ${r.status} ${r.statusText}`)
        return
      }
      setSkills((await r.json()) as Skill[])
      setError(null)
    } catch (e: any) {
      setError(e?.message ?? 'Failed to load skills')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Optimistic toggle — flip locally, reconcile on failure.
  async function toggle(ref: string, enabled: boolean) {
    setSkills((prev) =>
      prev.map((s) => (s.ref === ref ? { ...s, enabled } : s)),
    )
    const r = await authFetch(`/api/skills/${encodeURIComponent(ref)}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
    if (!r.ok) {
      setSkills((prev) =>
        prev.map((s) => (s.ref === ref ? { ...s, enabled: !enabled } : s)),
      )
    }
  }

  async function create(draft: DraftSkill): Promise<boolean> {
    const r = await authFetch('/api/skills', {
      method: 'POST',
      body: JSON.stringify({ ...draft, enabled: true }),
    })
    if (r.ok) await refresh()
    return r.ok
  }

  async function update(ref: string, draft: DraftSkill): Promise<boolean> {
    const r = await authFetch(`/api/skills/${encodeURIComponent(ref)}`, {
      method: 'PATCH',
      body: JSON.stringify(draft),
    })
    if (r.ok) await refresh()
    return r.ok
  }

  async function remove(ref: string): Promise<boolean> {
    const r = await authFetch(`/api/skills/${encodeURIComponent(ref)}`, {
      method: 'DELETE',
    })
    if (r.ok) await refresh()
    return r.ok
  }

  const enabledCount = skills.filter((s) => s.enabled).length

  return {
    skills,
    loading,
    error,
    enabledCount,
    refresh,
    toggle,
    create,
    update,
    remove,
  }
}

/* ─── small pieces ─── */

function SkillGlyph({ icon }: { icon: string }) {
  const path =
    icon === 'web'
      ? 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM2 12h20M12 2a15 15 0 0 1 0 20M12 2a15 15 0 0 0 0 20'
      : icon === 'doc'
        ? 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M8 13h8M8 17h8M8 9h2'
        : 'M12 2l2.4 5.6L20 9l-4.5 3.9L17 19l-5-3-5 3 1.5-6.1L4 9l5.6-1.4z'
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={path} />
    </svg>
  )
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

/* ─── add / edit modal ─── */

function SkillEditor({
  initial,
  onClose,
  onSave,
}: {
  initial: Skill | null
  onClose: () => void
  onSave: (draft: DraftSkill) => Promise<boolean>
}) {
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [instructions, setInstructions] = useState(initial?.instructions ?? '')
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function submit() {
    if (!name.trim() || !instructions.trim()) {
      setErr('Name and instructions are required.')
      return
    }
    setSaving(true)
    setErr(null)
    const ok = await onSave({
      name: name.trim(),
      description: description.trim(),
      instructions,
    })
    setSaving(false)
    if (ok) onClose()
    else setErr('Save failed. Try again.')
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-lineStrong bg-ink-200 p-5 shadow-2xl shadow-black/60"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-medium text-fog-50 mb-1">
          {initial ? 'Edit skill' : 'Add skill'}
        </h3>
        <p className="text-[12px] text-fog-400 mb-4">
          The description tells the agent <em>when</em> to use the skill. The
          instructions are folded into its system prompt while the skill is on.
        </p>

        <label className="block text-[12px] text-fog-300 mb-1">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. release-notes-writer"
          className="w-full mb-3 rounded-lg bg-ink-50 border border-lineStrong px-3 py-2 text-sm outline-none focus:border-accent/60"
        />

        <label className="block text-[12px] text-fog-300 mb-1">
          Description <span className="text-fog-500">(when to use it)</span>
        </label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="Use when the user asks for…"
          className="w-full mb-3 rounded-lg bg-ink-50 border border-lineStrong px-3 py-2 text-sm outline-none focus:border-accent/60 resize-none"
        />

        <label className="block text-[12px] text-fog-300 mb-1">
          Instructions
        </label>
        <textarea
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          rows={7}
          placeholder="Step-by-step guidance, rules, and output format…"
          className="w-full mb-3 rounded-lg bg-ink-50 border border-lineStrong px-3 py-2 text-sm outline-none focus:border-accent/60 resize-y font-mono text-[12.5px]"
        />

        {err && <p className="text-[12px] text-red-400 mb-3">{err}</p>}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-sm text-fog-300 hover:bg-soft/[0.06]"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="px-3 py-1.5 rounded-lg text-sm bg-accent text-ink-50 hover:bg-accent/90 disabled:opacity-40"
          >
            {saving ? 'Saving…' : initial ? 'Save changes' : 'Add skill'}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ─── composer submenu (opens from the chat "+" menu) ─── */

export function SkillsComposerFlyout({
  onManage,
  onClose,
}: {
  onManage: () => void
  onClose: () => void
}) {
  const { skills, loading, toggle, create } = useSkills()
  const [adding, setAdding] = useState(false)

  return (
    <div className="w-64 rounded-xl border border-lineStrong bg-ink-200 p-1 text-sm shadow-2xl shadow-black/60">
      <div className="px-3 pt-2 pb-1 text-[11px] uppercase tracking-wide text-fog-500">
        Skills
      </div>

      <div className="max-h-64 overflow-y-auto py-1">
        {loading ? (
          <div className="px-3 py-3 text-[12px] text-fog-400">Loading…</div>
        ) : skills.length === 0 ? (
          <div className="px-3 py-3 text-[12px] text-fog-400">
            No skills yet.
          </div>
        ) : (
          skills.map((s) => (
            <div
              key={s.ref}
              className="w-full px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5"
            >
              <span className="text-fog-300">
                <SkillGlyph icon={s.icon} />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-fog-100 truncate">{s.name}</div>
              </div>
              <Toggle on={s.enabled} onChange={(v) => toggle(s.ref, v)} />
            </div>
          ))
        )}
      </div>

      <div className="border-t border-line mt-1 pt-1">
        <button
          type="button"
          onClick={() => {
            onClose()
            onManage()
          }}
          className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5 text-fog-200"
        >
          <IconGear />
          Manage skills
        </button>
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="w-full text-left px-3 py-2 rounded-md hover:bg-soft/[0.06] flex items-center gap-2.5 text-fog-200"
        >
          <IconPlusSmall />
          Add skill
        </button>
      </div>

      {adding && (
        <SkillEditor
          initial={null}
          onClose={() => {
            setAdding(false)
            onClose()
          }}
          onSave={create}
        />
      )}
    </div>
  )
}

/* ─── full manage panel (embedded in shell + /app/skills route) ─── */

export function SkillsInventoryPanel({
  embedded = false,
}: {
  embedded?: boolean
}) {
  const { skills, loading, error, enabledCount, toggle, create, update, remove } =
    useSkills()
  const [editing, setEditing] = useState<Skill | null>(null)
  const [adding, setAdding] = useState(false)

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="h-12 px-5 border-b border-line flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2 text-sm">
          {!embedded && (
            <Link href="/app" className="text-fog-400 hover:text-fog-100">
              ← Back
            </Link>
          )}
          <span className="font-medium text-fog-50">Skills</span>
          <span className="text-[12px] text-fog-500">
            {enabledCount} active
          </span>
        </div>
        <button
          onClick={() => setAdding(true)}
          className="px-3 py-1.5 rounded-lg text-sm bg-accent text-ink-50 hover:bg-accent/90 flex items-center gap-1.5"
        >
          <IconPlusSmall />
          New skill
        </button>
      </header>

      <div className="flex-1 overflow-y-auto p-5">
        <p className="text-[13px] text-fog-400 max-w-2xl mb-5">
          Skills are reusable instruction bundles. Turn one on and the agent
          folds its guidance into every reply for the situations it describes —
          like enabling a specialist on demand.
        </p>

        {error && (
          <div className="mb-4 text-[13px] text-red-400 border border-red-500/30 bg-red-500/5 rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-fog-400 text-sm">Loading skills…</div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 max-w-3xl">
            {skills.map((s) => (
              <div
                key={s.ref}
                className={`rounded-xl border p-4 transition ${
                  s.enabled
                    ? 'border-accent/40 bg-accent/[0.04]'
                    : 'border-lineStrong bg-ink-200'
                }`}
              >
                <div className="flex items-start gap-3">
                  <span className="w-8 h-8 rounded-lg bg-soft/10 text-accent flex items-center justify-center shrink-0">
                    <SkillGlyph icon={s.icon} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-fog-50 truncate">
                        {s.name}
                      </span>
                      {s.is_builtin && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-soft/10 text-fog-400">
                          built-in
                        </span>
                      )}
                    </div>
                    <p className="text-[12.5px] text-fog-400 mt-1 line-clamp-3">
                      {s.description || 'No description.'}
                    </p>
                  </div>
                  <Toggle on={s.enabled} onChange={(v) => toggle(s.ref, v)} />
                </div>

                {s.is_custom && (
                  <div className="flex justify-end gap-3 mt-3 text-[12px]">
                    <button
                      onClick={() => setEditing(s)}
                      className="text-fog-300 hover:text-fog-50"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => {
                        if (confirm(`Delete the "${s.name}" skill?`)) {
                          remove(s.ref)
                        }
                      }}
                      className="text-red-400/80 hover:text-red-400"
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {adding && (
        <SkillEditor
          initial={null}
          onClose={() => setAdding(false)}
          onSave={create}
        />
      )}
      {editing && (
        <SkillEditor
          initial={editing}
          onClose={() => setEditing(null)}
          onSave={(draft) => update(editing.ref, draft)}
        />
      )}
    </div>
  )
}

/* ─── icons ─── */

function IconGear() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  )
}

function IconPlusSmall() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  )
}
