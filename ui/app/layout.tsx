import './globals.css'
import type { Metadata } from 'next'
import { Inter, Instrument_Serif } from 'next/font/google'

const sans = Inter({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-sans',
  display: 'swap',
})

const serif = Instrument_Serif({
  subsets: ['latin'],
  weight: ['400'],
  style: ['normal', 'italic'],
  variable: '--font-serif',
  display: 'swap',
})

export const metadata: Metadata = {
  title: 'Agent — An agent that keeps your context',
  description:
    'A cloud-based agent platform with persistent sessions, threaded conversations, and tool-aware reasoning.',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${serif.variable}`}>
      <body className="bg-ink-50 text-fog-100">{children}</body>
    </html>
  )
}
