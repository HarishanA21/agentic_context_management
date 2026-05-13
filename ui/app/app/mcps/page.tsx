'use client'

// Standalone /app/mcps route — kept so direct bookmarks still resolve.
// The same MCPInventoryPanel also renders inline from the sidebar in
// app/app/page.tsx; both surfaces share one component.

import { MCPInventoryPanel } from '@/components/mcp-inventory'

export default function MCPsPage() {
  return (
    <div className="flex h-screen bg-ink-50 text-fog-100 overflow-hidden">
      <main className="flex-1 flex flex-col min-w-0">
        <MCPInventoryPanel embedded={false} />
      </main>
    </div>
  )
}
