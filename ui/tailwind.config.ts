import type { Config } from 'tailwindcss'

const config: Config = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        ink: {
          0: '#000000',
          50: '#0a0a0a',
          100: '#0f0f10',
          200: '#141416',
          300: '#1a1a1d',
          400: '#1f1f23',
          500: '#2a2a2e',
          600: '#3a3a3e',
        },
        fog: {
          50: '#f5f5f5',
          100: '#e6e6e6',
          200: '#bdbdbd',
          300: '#8a8a8a',
          400: '#6b6b6b',
          500: '#4a4a4a',
        },
        line: 'rgba(255,255,255,0.08)',
        lineStrong: 'rgba(255,255,255,0.14)',
        // legacy aliases used by older code
        panel: '#0f0f10',
        panelAlt: '#141416',
        border: '#1f1f23',
        accent: '#ffffff',
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
