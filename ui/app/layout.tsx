import './globals.css'
import type { Metadata } from 'next'

export const metadata: Metadata = {
  title: 'FYP Agent',
  description: 'Agent chat UI',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  )
}
