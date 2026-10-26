/**
 * auth.ts — Keycloak-based auth client.
 *
 * Replaces the old Supabase auth client. Keeps the same public API surface
 * so all call-sites that import from '@/lib/supabase' continue to work via
 * the re-export shim in supabase.ts.
 *
 * Auth flow: Resource Owner Password Credentials (direct grant).
 *  - The login page POSTs email + password to Keycloak's token endpoint.
 *  - Tokens are stored in localStorage (access_token + refresh_token).
 *  - authToken() returns the current access token, auto-refreshing if close to expiry.
 *  - onAuthStateChange() emits events so pages react to sign-in / sign-out.
 */

const KEYCLOAK_URL = process.env.NEXT_PUBLIC_KEYCLOAK_URL ?? 'http://localhost:8080'
const KEYCLOAK_REALM = process.env.NEXT_PUBLIC_KEYCLOAK_REALM ?? 'acm'
const KEYCLOAK_CLIENT_ID = process.env.NEXT_PUBLIC_KEYCLOAK_CLIENT_ID ?? 'acm-frontend'

const TOKEN_ENDPOINT = `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/token`
const LOGOUT_ENDPOINT = `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/logout`

const LS_ACCESS = 'kc_access_token'
const LS_REFRESH = 'kc_refresh_token'
const LS_EXPIRES = 'kc_expires_at'   // epoch ms

// ── Simple event bus for auth state changes ────────────────────────────────

type AuthEvent = 'SIGNED_IN' | 'SIGNED_OUT' | 'TOKEN_REFRESHED'
type AuthStateListener = (event: AuthEvent, session: Session | null) => void

const listeners: AuthStateListener[] = []

function emit(event: AuthEvent, session: Session | null) {
  listeners.forEach((fn) => fn(event, session))
}

// ── Token storage helpers ──────────────────────────────────────────────────

export interface Session {
  access_token: string
  refresh_token: string
  expires_at: number  // epoch ms
  user: { id: string; email?: string }
}

function saveSession(
  access_token: string,
  refresh_token: string,
  expires_in: number,
  sub: string,
  email?: string,
): Session {
  const expires_at = Date.now() + expires_in * 1000
  if (typeof window !== 'undefined') {
    localStorage.setItem(LS_ACCESS, access_token)
    localStorage.setItem(LS_REFRESH, refresh_token)
    localStorage.setItem(LS_EXPIRES, String(expires_at))
  }
  return { access_token, refresh_token, expires_at, user: { id: sub, email } }
}

function clearSession() {
  if (typeof window !== 'undefined') {
    localStorage.removeItem(LS_ACCESS)
    localStorage.removeItem(LS_REFRESH)
    localStorage.removeItem(LS_EXPIRES)
  }
}

function loadSession(): Session | null {
  if (typeof window === 'undefined') return null
  const access_token = localStorage.getItem(LS_ACCESS)
  const refresh_token = localStorage.getItem(LS_REFRESH)
  const expires_at = Number(localStorage.getItem(LS_EXPIRES) ?? '0')
  if (!access_token || !refresh_token) return null
  // Decode subject from JWT payload (no verification — backend verifies)
  try {
    const payload = JSON.parse(atob(access_token.split('.')[1]))
    return { access_token, refresh_token, expires_at, user: { id: payload.sub, email: payload.email } }
  } catch {
    return null
  }
}

// ── Token refresh ──────────────────────────────────────────────────────────

// Keycloak refresh tokens are single-use (refreshTokenMaxReuse: 0). If two
// requests race in with the same stale-but-not-yet-expired access token
// (e.g. Promise.all([authFetch(a), authFetch(b)])), each would otherwise
// call refreshSession() with the same refresh_token — the second call gets
// invalid_grant, wipes the session via clearSession(), and every request
// after that loses its Authorization header and 401s. Cache the in-flight
// promise so concurrent callers share one refresh instead of racing.
let _refreshInFlight: Promise<Session | null> | null = null

async function refreshSession(refresh_token: string): Promise<Session | null> {
  if (_refreshInFlight) return _refreshInFlight
  _refreshInFlight = _doRefresh(refresh_token)
  try {
    return await _refreshInFlight
  } finally {
    _refreshInFlight = null
  }
}

async function _doRefresh(refresh_token: string): Promise<Session | null> {
  try {
    const res = await fetch(TOKEN_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: KEYCLOAK_CLIENT_ID,
        refresh_token,
      }),
    })
    if (!res.ok) { clearSession(); emit('SIGNED_OUT', null); return null }
    const data = await res.json()
    const payload = JSON.parse(atob(data.access_token.split('.')[1]))
    const session = saveSession(data.access_token, data.refresh_token ?? refresh_token, data.expires_in, payload.sub, payload.email)
    emit('TOKEN_REFRESHED', session)
    return session
  } catch {
    clearSession()
    emit('SIGNED_OUT', null)
    return null
  }
}

// ── Auth API ───────────────────────────────────────────────────────────────

export const kcAuth = {
  /**
   * Sign in with email + password via Keycloak direct grant.
   * Mimics `supabase.auth.signInWithPassword`.
   */
  async signInWithPassword({ email, password }: { email: string; password: string }): Promise<{ error: Error | null }> {
    try {
      const res = await fetch(TOKEN_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'password',
          client_id: KEYCLOAK_CLIENT_ID,
          username: email,
          password,
          scope: 'openid email profile',
        }),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        return { error: new Error(err.error_description ?? 'Invalid credentials') }
      }
      const data = await res.json()
      const payload = JSON.parse(atob(data.access_token.split('.')[1]))
      const session = saveSession(data.access_token, data.refresh_token, data.expires_in, payload.sub, payload.email)
      emit('SIGNED_IN', session)
      return { error: null }
    } catch (e: any) {
      return { error: e }
    }
  },

  /**
   * Register a new user via Keycloak Registration API.
   * Mimics `supabase.auth.signUp`.
   * Note: Keycloak's open registration must be enabled in the realm
   * (registrationAllowed: true — already set in realm-export.json).
   */
  async signUp({ email, password }: { email: string; password: string }): Promise<{ data: { session: Session | null }; error: Error | null }> {
    const REGISTER_URL = `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/registrations`
    try {
      // Keycloak doesn't have a direct REST registration endpoint for resource-owner
      // flow without Admin API. We use the Admin REST API (requires admin credentials)
      // or fall back to signing in after redirect-based registration.
      // For dev simplicity: create via Admin REST API.
      const adminToken = await getAdminToken()
      if (!adminToken) throw new Error('Unable to reach Keycloak admin. Is Keycloak running?')

      const createRes = await fetch(
        `${KEYCLOAK_URL}/admin/realms/${KEYCLOAK_REALM}/users`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${adminToken}`,
          },
          body: JSON.stringify({
            username: email,
            email,
            enabled: true,
            emailVerified: true,
            credentials: [{ type: 'password', value: password, temporary: false }],
          }),
        },
      )
      if (!createRes.ok) {
        if (createRes.status === 409) return { data: { session: null }, error: new Error('An account with this email already exists.') }
        const text = await createRes.text()
        return { data: { session: null }, error: new Error(text || 'Registration failed') }
      }
      // Auto sign-in after registration
      const signInResult = await kcAuth.signInWithPassword({ email, password })
      if (signInResult.error) return { data: { session: null }, error: signInResult.error }
      const session = loadSession()
      return { data: { session }, error: null }
    } catch (e: any) {
      return { data: { session: null }, error: e }
    }
  },

  /** Get the current session, refreshing if needed. Mimics `supabase.auth.getSession`. */
  async getSession(): Promise<{ data: { session: Session | null } }> {
    let session = loadSession()
    if (!session) return { data: { session: null } }
    // Refresh if token expires within the next 60 seconds
    if (session.expires_at - Date.now() < 60_000) {
      session = await refreshSession(session.refresh_token)
    }
    return { data: { session } }
  },

  /** Sign out locally and revoke the refresh token. Mimics `supabase.auth.signOut`. */
  async signOut(): Promise<void> {
    const session = loadSession()
    if (session) {
      // Revoke refresh token (best-effort)
      fetch(LOGOUT_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          client_id: KEYCLOAK_CLIENT_ID,
          refresh_token: session.refresh_token,
        }),
      }).catch(() => {})
    }
    clearSession()
    emit('SIGNED_OUT', null)
  },

  /**
   * Subscribe to auth state changes. Returns an object with an
   * `unsubscribe` method, mirroring the Supabase `onAuthStateChange` API
   * shape used as `sub.unsubscribe()` in the codebase.
   */
  onAuthStateChange(cb: AuthStateListener): { data: { subscription: { unsubscribe: () => void } } } {
    listeners.push(cb)
    return {
      data: {
        subscription: {
          unsubscribe() {
            const idx = listeners.indexOf(cb)
            if (idx !== -1) listeners.splice(idx, 1)
          },
        },
      },
    }
  },
}

// ── Admin token helper (used only for signUp) ─────────────────────────────

const ADMIN_CLIENT_ID = 'admin-cli'
let _adminToken: string | null = null
let _adminTokenExpiry = 0

async function getAdminToken(): Promise<string | null> {
  if (_adminToken && Date.now() < _adminTokenExpiry - 5000) return _adminToken
  try {
    const res = await fetch(
      `${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'password',
          client_id: ADMIN_CLIENT_ID,
          username: process.env.NEXT_PUBLIC_KEYCLOAK_ADMIN ?? 'admin',
          password: process.env.NEXT_PUBLIC_KEYCLOAK_ADMIN_PASSWORD ?? 'admin',
        }),
      },
    )
    if (!res.ok) return null
    const data = await res.json()
    _adminToken = data.access_token
    _adminTokenExpiry = Date.now() + data.expires_in * 1000
    return _adminToken
  } catch {
    return null
  }
}

// ── Public helpers (same API as old supabase.ts) ───────────────────────────

/** Return the current access token (JWT), or null if signed out. */
export async function authToken(): Promise<string | null> {
  const { data: { session } } = await kcAuth.getSession()
  return session?.access_token ?? null
}

/** Authenticated fetch — attaches Bearer token. Same API as old supabase.ts. */
export async function authFetch(input: string, init: RequestInit = {}) {
  const { data: { session } } = await kcAuth.getSession()
  const token = session?.access_token
  const isFormData = typeof FormData !== 'undefined' && init.body instanceof FormData
  const headers: Record<string, string> = {
    ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
    ...((init.headers as Record<string, string>) || {}),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
  return fetch(input, { ...init, headers })
}
