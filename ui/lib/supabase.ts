/**
 * supabase.ts — Compatibility shim.
 *
 * All imports of `@/lib/supabase` continue to work. The actual Keycloak
 * auth implementation lives in `@/lib/auth.ts`.
 *
 * `supabase.auth` is a thin adapter so existing call-sites like:
 *   supabase.auth.getSession()
 *   supabase.auth.onAuthStateChange(...)
 *   supabase.auth.signOut()
 *   supabase.auth.signInWithPassword(...)
 *   supabase.auth.signUp(...)
 * all continue to work transparently.
 */

export { authToken, authFetch } from './auth'
import { kcAuth } from './auth'

/** Drop-in replacement for the Supabase client object. */
export const supabase = {
  auth: kcAuth,
}
