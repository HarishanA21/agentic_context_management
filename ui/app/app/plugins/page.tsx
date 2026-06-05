'use client'

// Standalone /app/plugins route — kept so direct bookmarks resolve.
// The same PluginsInventoryPanel also renders embedded from the sidebar in
// app/app/page.tsx; both surfaces share one component.

import { PluginsInventoryPanel } from '@/components/plugins-inventory'

export default function PluginsPage() {
  return (
    <div className="flex h-screen bg-ink-50 text-fog-100 overflow-hidden">
      <main className="flex-1 flex flex-col min-w-0">
        <PluginsInventoryPanel embedded={false} />
      </main>
    </div>
  )
}
