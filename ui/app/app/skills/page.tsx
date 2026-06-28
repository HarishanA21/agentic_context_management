'use client'

// Standalone /app/skills route — kept so direct bookmarks resolve.
// The same SkillsInventoryPanel also renders embedded from the sidebar in
// app/app/page.tsx; both surfaces share one component.

import { SkillsInventoryPanel } from '@/components/skills-inventory'

export default function SkillsPage() {
  return (
    <div className="flex h-screen bg-ink-50 text-fog-100 overflow-hidden">
      <main className="flex-1 flex flex-col min-w-0">
        <SkillsInventoryPanel embedded={false} />
      </main>
    </div>
  )
}
