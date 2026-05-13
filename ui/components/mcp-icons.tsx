// Per-MCP icons.
//
// For publishers with a simple-icons entry we pull the official path data
// + brand colour from components/mcp-brand-icons.ts (regenerate via
// scripts/gen-mcp-icons.py). For everything else — generic catalog
// entries (filesystem, fetch, time, …) and brands simple-icons doesn't
// cover (Playwright, Outline, Slack as a brand) — we hand-roll a
// Lucide-style outline icon below.
//
// All renderers are 24×24-viewBox so they nest cleanly in the same
// container.

import { BRAND_ICONS } from './mcp-brand-icons'

type IconRenderer = (props: { size: number }) => JSX.Element

// ── Generic / hand-rolled icons ─────────────────────────────────────────

const generic: Record<string, IconRenderer> = {
  filesystem: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <path d="M3 11h18" opacity="0.5" />
    </svg>
  ),
  fetch: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" opacity="0.7" />
    </svg>
  ),
  memory: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 3a3 3 0 0 0-3 3v0a3 3 0 0 0-3 3v3a3 3 0 0 0 3 3v0a3 3 0 0 0 3 3" />
      <path d="M15 3a3 3 0 0 1 3 3v0a3 3 0 0 1 3 3v3a3 3 0 0 1-3 3v0a3 3 0 0 1-3 3" />
      <path d="M9 3v18M15 3v18" opacity="0.4" />
    </svg>
  ),
  sequential: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 6h13M8 12h13M8 18h13" />
      <path d="M3 6h.01M3 12h.01M3 18h.01" />
    </svg>
  ),
  time: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </svg>
  ),
  generic: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
      <path d="M3.27 6.96L12 12.01l8.73-5.05M12 22.08V12" opacity="0.6" />
    </svg>
  ),
  // brands not in simple-icons — drawn from scratch with brand-y colours
  playwright: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M5.5 6.5C5.5 4.6 7.1 3 9 3s3.5 1.6 3.5 3.5S10.9 10 9 10 5.5 8.4 5.5 6.5zm5.5 8.7c-1.6-.4-2.7-1.4-3.2-3l3.2.8v2.2zm6.4-7.7c1.4 0 2.5 1.1 2.5 2.5S18.8 12.5 17.4 12.5s-2.5-1.1-2.5-2.5 1.1-2.5 2.5-2.5zm2 6.6l-3.5-1c-.7 2.3-2.4 3.5-4.4 3.7v2.2c3.8-.1 6.8-2 7.9-4.9z" />
    </svg>
  ),
  outline: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M3 3h18v3H3V3zm0 6h12v3H3V9zm0 6h18v3H3v-3z" />
    </svg>
  ),
  slack: ({ size }) => (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor">
      <path d="M5.04 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.52-2.522h2.52v2.522zm1.268 0a2.527 2.527 0 0 1 2.52-2.522 2.527 2.527 0 0 1 2.52 2.522v6.31A2.528 2.528 0 0 1 8.828 24a2.528 2.528 0 0 1-2.52-2.525v-6.31zM8.828 5.043a2.528 2.528 0 0 1-2.52-2.522A2.528 2.528 0 0 1 8.828 0a2.528 2.528 0 0 1 2.52 2.521v2.522H8.828zm0 1.27a2.527 2.527 0 0 1 2.52 2.521 2.527 2.527 0 0 1-2.52 2.522H2.52A2.527 2.527 0 0 1 0 8.834a2.527 2.527 0 0 1 2.52-2.522h6.308zm10.122 2.521a2.528 2.528 0 0 1 2.52-2.522A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.52 2.522h-2.52V8.834zm-1.268 0a2.527 2.527 0 0 1-2.52 2.522 2.527 2.527 0 0 1-2.52-2.522V2.522A2.527 2.527 0 0 1 15.16 0a2.528 2.528 0 0 1 2.52 2.522v6.31zm-2.52 10.122a2.528 2.528 0 0 1 2.52 2.521A2.528 2.528 0 0 1 15.16 24a2.528 2.528 0 0 1-2.52-2.524v-2.521h2.52zm0-1.27a2.527 2.527 0 0 1-2.52-2.522 2.527 2.527 0 0 1 2.52-2.522h6.308a2.527 2.527 0 0 1 2.52 2.522 2.527 2.527 0 0 1-2.52 2.522H15.16z" />
    </svg>
  ),
}

// Colours for the brands without simple-icons coverage.
const BRAND_COLOR_FALLBACK: Record<string, string> = {
  playwright: '#2EAD33',
  outline: '#0091FF',
  slack: '#4A154B',
}

// ── Public component ───────────────────────────────────────────────────

export function MCPIcon({
  slug,
  size = 18,
}: {
  slug: string
  size?: number
}) {
  const brand = BRAND_ICONS[slug]
  if (brand) {
    return (
      <span
        className="inline-flex items-center justify-center rounded-md"
        style={{
          width: size + 14,
          height: size + 14,
          backgroundColor: hexToRgba(brand.color, 0.12),
          color: brand.color,
        }}
        aria-label={brand.title}
      >
        <svg
          width={size}
          height={size}
          viewBox="0 0 24 24"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          <path d={brand.path} fill="currentColor" />
        </svg>
      </span>
    )
  }

  const Renderer = generic[slug] || generic.generic
  const fallback = BRAND_COLOR_FALLBACK[slug]
  if (fallback) {
    return (
      <span
        className="inline-flex items-center justify-center rounded-md"
        style={{
          width: size + 14,
          height: size + 14,
          backgroundColor: hexToRgba(fallback, 0.12),
          color: fallback,
        }}
      >
        <Renderer size={size} />
      </span>
    )
  }

  // Neutral generic tile — slightly tinted by the slug's first letter so
  // the catalog grid doesn't look like a wall of identical squares.
  const tint = tintFor(slug)
  return (
    <span
      className={`inline-flex items-center justify-center rounded-md ${tint}`}
      style={{ width: size + 14, height: size + 14 }}
    >
      <Renderer size={size} />
    </span>
  )
}

function hexToRgba(hex: string, alpha: number): string {
  const m = hex.replace('#', '')
  const r = parseInt(m.slice(0, 2), 16)
  const g = parseInt(m.slice(2, 4), 16)
  const b = parseInt(m.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

const TINTS: Record<string, string> = {
  filesystem: 'bg-blue-500/12 text-blue-300',
  fetch: 'bg-cyan-500/12 text-cyan-300',
  memory: 'bg-violet-500/12 text-violet-300',
  sequential: 'bg-indigo-500/12 text-indigo-300',
  time: 'bg-amber-500/12 text-amber-300',
  generic: 'bg-soft/10 text-fog-200',
}

function tintFor(slug: string): string {
  return TINTS[slug] || TINTS.generic
}
