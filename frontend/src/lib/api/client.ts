import createClient from 'openapi-fetch'
import type { paths } from './schema'
import { toProblemDetail } from './formatError'
import {
  getAuthHeaders,
  attemptTokenRefresh,
  clearAccessToken,
  redirectToLogin,
  wasDemoSessionEnded,
} from './auth'

export {
  getAccessToken,
  clearAccessToken,
  setAccessToken,
  setRefreshToken,
  onAuthChange,
  getAuthHeaders,
  isDemoSession,
  setDemoSession,
  wasDemoSessionEnded,
} from './auth'

// Decides the app's initial auth state at startup (used by App.vue). An
// existing stored token means the user is already authenticated; auto-login
// only runs when no session exists yet. Auto-login config is deliberately not
// consulted here so a configured auto-login can never force a false-negative
// start for a user who already has a valid token.
export function getInitialAuthState(hasToken: boolean): boolean {
  return hasToken
}

// Decides whether the auto-login flow must re-run after an auth-state change
// (used by App.vue). Recovery only triggers on the authenticated→cleared
// transition — an expired stored token whose refresh failed — and only when
// auto-login is configured. A fresh unauthenticated start (first mount) or a
// successful re-authentication must never re-run it, otherwise the login
// endpoint would be hammered in a loop.
// FAR-535: a cleared DEMO session is additionally never recovered — the demo
// session must die with its short-lived token instead of escalating into the
// instance's silent auto-login account. qa iter 2: the gate reads the PERSISTED
// demo-ended tombstone (lib/api/auth.ts), not an in-memory flag — the tombstone
// is written before the marker is removed in clearAccessToken (same in-session
// semantics) but is shared across tabs, so a second tab's stale state can no
// longer permit the escalation. It resets on any new successful auth.
export function shouldReRunAutoLogin(wasAuthenticated: boolean, hasToken: boolean, hasAutoLogin: boolean): boolean {
  if (wasDemoSessionEnded()) return false
  return wasAuthenticated && !hasToken && hasAutoLogin
}

export const api = createClient<paths>({
  baseUrl: '',
  headers: getAuthHeaders(),
})

// openapi-fetch doesn't support dynamic headers, so we wrap the methods
// to inject the auth token on every request.
const _origGet = api.GET
const _origPost = api.POST
const _origPut = api.PUT
const _origPatch = api.PATCH
const _origDelete = api.DELETE

function withAuth(fn: (...args: any[]) => any) {
  return async (...args: any[]) => {
    const [url, options] = args
    const headers = { ...getAuthHeaders(), ...options?.headers }
    let resp = await fn(url, { ...options, headers })
    if (resp.response?.status === 401) {
      const refreshed = await attemptTokenRefresh()
      if (refreshed) {
        const newHeaders = { ...getAuthHeaders(), ...options?.headers }
        resp = await fn(url, { ...options, headers: newHeaders })
      }
      if (!refreshed || resp.response?.status === 401) {
        clearAccessToken()
        redirectToLogin()
        return { response: undefined, data: undefined, error: undefined } as any
      }
    }
    if (resp.error && typeof resp.error === 'object') {
      resp.error = toProblemDetail(resp.error) as any
    }
    return resp
  }
}

api.GET = withAuth(_origGet) as any
api.POST = withAuth(_origPost) as any
api.PUT = withAuth(_origPut) as any
api.PATCH = withAuth(_origPatch) as any
api.DELETE = withAuth(_origDelete) as any

export type { paths, components } from './schema'
