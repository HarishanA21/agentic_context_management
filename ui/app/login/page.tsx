'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { supabase } from '@/lib/supabase'

export default function LoginPage() {
  const router = useRouter()
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) router.replace('/')
    })
  }, [router])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    setInfo(null)
    setLoading(true)
    try {
      if (mode === 'signin') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
        router.replace('/')
      } else {
        const { data, error } = await supabase.auth.signUp({ email, password })
        if (error) throw error
        if (data.session) {
          router.replace('/')
        } else {
          setInfo('Check your email to confirm your account, then sign in.')
        }
      }
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-md bg-panel border border-border rounded-xl p-8 shadow-xl">
        <h1 className="text-2xl font-semibold mb-1">FYP Agent</h1>
        <p className="text-gray-400 text-sm mb-6">
          {mode === 'signin' ? 'Sign in to your account' : 'Create a new account'}
        </p>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-sm text-gray-400 mb-1">Email</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-panelAlt border border-border rounded-md px-3 py-2 outline-none focus:border-accent"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-400 mb-1">Password</label>
            <input
              type="password"
              required
              minLength={6}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-panelAlt border border-border rounded-md px-3 py-2 outline-none focus:border-accent"
            />
          </div>

          {error && <p className="text-red-400 text-sm">{error}</p>}
          {info && <p className="text-green-400 text-sm">{info}</p>}

          <button
            type="submit"
            disabled={loading}
            className="w-full bg-accent hover:bg-blue-500 disabled:opacity-50 text-white font-medium py-2.5 rounded-md"
          >
            {loading ? '...' : mode === 'signin' ? 'Sign in' : 'Sign up'}
          </button>
        </form>

        <button
          onClick={() => {
            setMode(mode === 'signin' ? 'signup' : 'signin')
            setError(null)
            setInfo(null)
          }}
          className="w-full mt-4 text-sm text-gray-400 hover:text-white"
        >
          {mode === 'signin'
            ? "Don't have an account? Sign up"
            : 'Already have an account? Sign in'}
        </button>
      </div>
    </div>
  )
}
