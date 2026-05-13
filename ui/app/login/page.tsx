'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
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
      if (data.session) router.replace('/app')
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
        router.replace('/app')
      } else {
        const { data, error } = await supabase.auth.signUp({ email, password })
        if (error) throw error
        if (data.session) {
          router.replace('/app')
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
    <div className="min-h-screen relative bg-ink-50 text-fog-100 flex items-center justify-center px-4 overflow-hidden">
      <div className="pointer-events-none absolute inset-0 bg-radial-fade" />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 opacity-[0.05] bg-grid-faint bg-grid-32 [mask-image:radial-gradient(ellipse_at_center,black,transparent_70%)]"
      />

      {/* Top brand */}
      <Link
        href="/"
        className="absolute top-6 left-6 flex items-center gap-2 group"
      >
        <span className="relative w-6 h-6 inline-flex items-center justify-center">
          <span className="absolute inset-0 rounded-md bg-white/10 group-hover:bg-white/20 transition" />
          <span className="relative w-2 h-2 rounded-sm bg-white" />
        </span>
        <span className="text-sm tracking-tight">agent</span>
      </Link>

      <div className="relative w-full max-w-sm">
        <div className="text-center mb-8">
          <h1 className="serif text-4xl tracking-tighter text-white mb-2">
            {mode === 'signin' ? 'Welcome back.' : 'Create your account.'}
          </h1>
          <p className="text-fog-300 text-sm">
            {mode === 'signin'
              ? 'Sign in to continue your work.'
              : 'Start with a fresh project in seconds.'}
          </p>
        </div>

        <div className="surface p-7 shadow-soft">
          <form onSubmit={submit} className="space-y-4">
            <Field
              label="Email"
              type="email"
              value={email}
              onChange={setEmail}
              placeholder="you@company.com"
            />
            <Field
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="••••••••"
              minLength={6}
            />

            {error && (
              <p className="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-md px-3 py-2">
                {error}
              </p>
            )}
            {info && (
              <p className="text-sm text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded-md px-3 py-2">
                {info}
              </p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-white text-black hover:bg-fog-50 disabled:opacity-50 transition rounded-lg py-2.5 text-sm font-medium"
            >
              {loading ? '…' : mode === 'signin' ? 'Sign in' : 'Create account'}
            </button>
          </form>
        </div>

        <button
          onClick={() => {
            setMode(mode === 'signin' ? 'signup' : 'signin')
            setError(null)
            setInfo(null)
          }}
          className="w-full mt-5 text-sm text-fog-300 hover:text-white transition"
        >
          {mode === 'signin' ? (
            <>
              Don't have an account?{' '}
              <span className="text-white underline underline-offset-4">
                Sign up
              </span>
            </>
          ) : (
            <>
              Already have an account?{' '}
              <span className="text-white underline underline-offset-4">
                Sign in
              </span>
            </>
          )}
        </button>

        <p className="text-center text-xs text-fog-500 mt-8">
          By continuing, you agree to our terms and privacy policy.
        </p>
      </div>
    </div>
  )
}

function Field({
  label,
  type,
  value,
  onChange,
  placeholder,
  minLength,
}: {
  label: string
  type: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  minLength?: number
}) {
  return (
    <label className="block">
      <span className="block text-xs text-fog-400 mb-1.5">{label}</span>
      <input
        type={type}
        required
        minLength={minLength}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-ink-100 border border-line focus:border-lineStrong rounded-lg px-3 py-2.5 text-[15px] outline-none transition placeholder:text-fog-500"
      />
    </label>
  )
}
