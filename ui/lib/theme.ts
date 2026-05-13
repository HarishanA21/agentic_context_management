'use client'

// Tiny theme manager. The initial value is set by an inline script in
// layout.tsx before hydration, so this hook only has to *read* what's
// already on <html> and persist changes when the user toggles.

import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'agent-theme'

function readTheme(): Theme {
  if (typeof document === 'undefined') return 'dark'
  const attr = document.documentElement.getAttribute('data-theme')
  return attr === 'light' ? 'light' : 'dark'
}

export function useTheme(): [Theme, (next: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>('dark')

  useEffect(() => {
    setThemeState(readTheme())
  }, [])

  const setTheme = (next: Theme) => {
    setThemeState(next)
    if (typeof document !== 'undefined') {
      document.documentElement.setAttribute('data-theme', next)
      document.documentElement.style.colorScheme = next
    }
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // ignore — storage is best-effort
    }
  }

  return [theme, setTheme]
}
