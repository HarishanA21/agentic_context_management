import type { Config } from 'tailwindcss'

// Surfaces use the `ink-*` scale (darkest → lightest in dark mode).
// Text uses the `fog-*` scale (lightest → mid → faint in dark mode).
// Both are CSS-variable-backed so flipping `data-theme="light"` on <html>
// remaps every existing utility class without touching call sites.
const inkVars = (k: string) => `rgb(var(--ink-${k}) / <alpha-value>)`
const fogVars = (k: string) => `rgb(var(--fog-${k}) / <alpha-value>)`

const config: Config = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        ink: {
          0: inkVars('0'),
          50: inkVars('50'),
          100: inkVars('100'),
          200: inkVars('200'),
          300: inkVars('300'),
          400: inkVars('400'),
          500: inkVars('500'),
          600: inkVars('600'),
        },
        fog: {
          50: fogVars('50'),
          100: fogVars('100'),
          200: fogVars('200'),
          300: fogVars('300'),
          400: fogVars('400'),
          500: fogVars('500'),
        },
        line: 'rgb(var(--line-rgb) / var(--line-a, 0.08))',
        lineStrong: 'rgb(var(--line-rgb) / var(--line-strong-a, 0.14))',
        // `soft` follows the line color (white in dark, black in light) so
        // `bg-soft/[0.06]`, `hover:bg-soft/[0.08]`, etc. give the same
        // "subtle highlight over surface" feel in both themes.
        soft: 'rgb(var(--line-rgb) / <alpha-value>)',
        // legacy aliases used by older code
        panel: inkVars('100'),
        panelAlt: inkVars('200'),
        border: inkVars('400'),
        accent: 'rgb(var(--accent) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['var(--font-sans)', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        serif: ['var(--font-serif)', 'ui-serif', 'Georgia', 'serif'],
        mono: ['ui-monospace', 'Menlo', 'Consolas', 'monospace'],
      },
      letterSpacing: {
        tightest: '-0.04em',
        tighter2: '-0.025em',
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(255,255,255,0.06), 0 30px 80px -20px rgba(0,0,0,0.8)',
        soft: '0 1px 0 rgba(255,255,255,0.04) inset, 0 12px 40px -12px rgba(0,0,0,0.6)',
      },
      backgroundImage: {
        'radial-fade':
          'radial-gradient(ellipse 80% 50% at 50% -10%, rgba(255,255,255,0.08), transparent 60%)',
        'grid-faint':
          'linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px)',
      },
      backgroundSize: {
        'grid-32': '32px 32px',
      },
    },
  },
  plugins: [],
}

export default config
