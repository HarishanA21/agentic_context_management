import type { Config } from 'tailwindcss'

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        panel: '#141416',
        panelAlt: '#1b1b1e',
        border: '#2a2a2e',
        accent: '#4f8cff',
      },
    },
  },
  plugins: [],
}

export default config
