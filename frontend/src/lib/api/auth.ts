import { getAutoLoginConfig } from '../../config/runtime'

const TOKEN_KEY = 'modulo_access_token'
const REFRESH_TOKEN_KEY = 'modulo_refresh_token'
// FAR-535: persisted marker that the current session came from the /demo
// auto-login. Read by the demo-mode banner; cleared with the session.
const DEMO_SESSION_KEY = 'modulo_demo_session'
// FAR-535 (qa iter 1): persisted tombstone written when a demo session is torn
// down by expiry. clearAccessToken removes the token AND the demo marker
// together, so after a demo-token expiry + reload neither exists and
// first-mount auto-login would silently log the former demo visitor in as the
// instance's auto-login account. The tombstone outlives that clear (and any
// reload) and is consumed by App.vue's mount-time check; it is cleared by any
// NEW successful auth. qa iter 2: an EXPLICIT user logout (AppLayout.logout)
// suppresses the tombstone so the visitor can actually leave the demo — only
// an involuntary end (token expiry, forced clear) re-mints into /demo.
const DEMO_ENDED_KEY = 'modulo_demo_ended'

// S8475: only store well-formed, opaque token strings in browser storage.
// Rejects anything containing control/whitespace chars or exceeding a sane
// length, so tainted/untrusted data can never be persisted as a token.
const TOKEN_PATTERN = /^[A-Za-z0-9._\-]+$/
const MAX_TOKEN_LENGTH = 8192

export function isValidToken(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length > 0 &&
    value.length <= MAX_TOKEN_LENGTH &&
    TOKEN_PATTERN.test(value)
  )
}

function storeToken(key: string, token: string): void {
  if (!isValidToken(token)) {
    console.warn(`[auth] refusing to store invalid token for ${key}`)
    return
  }
  localStorage.setItem(key, token)
}

let _authListeners: Array<(token: string | null) => void> = []
let _refreshingPromise: Promise<boolean> | null = null

export function isDemoSession(): boolean {
  return localStorage.getItem(DEMO_SESSION_KEY) === '1'
}

export function setDemoSession(active: boolean): void {
  if (active) {
    localStorage.setItem(DEMO_SESSION_KEY, '1')
  } else {
    localStorage.removeItem(DEMO_SESSION_KEY)
  }
}

// Persisted signal that a demo session has ended involuntarily (expiry, forced
// clear) and no new authentication has succeeded since. Survives page reloads
// and is shared across tabs — unlike the in-memory flag it replaces (removed in
// qa iter 2), whose per-tab staleness could permit the auto-login escalation
// the tombstone exists to block.
export function wasDemoSessionEnded(): boolean {
  return localStorage.getItem(DEMO_ENDED_KEY) !== null
}

function markDemoSessionEnded(): void {
  if (localStorage.getItem(DEMO_ENDED_KEY) === null) {
    localStorage.setItem(DEMO_ENDED_KEY, String(Date.now()))
  }
}

function notifyListeners(): void {
  const token = localStorage.getItem(TOKEN_KEY)
  const listeners = _authListeners.slice()
  for (const fn of listeners) {
    fn(token)
  }
}

export function onAuthChange(fn: (token: string | null) => void): () => void {
  _authListeners.push(fn)
  fn(localStorage.getItem(TOKEN_KEY))
  return () => {
    _authListeners = _authListeners.filter((f) => f !== fn)
  }
}

export function setAccessToken(token: string): void {
  storeToken(TOKEN_KEY, token)
  // Any new successful authentication (real login, SSO callback, token refresh,
  // or a fresh demo hand-off) supersedes all prior demo state: the demo marker
  // must never survive into a real session (a two-tab race would otherwise
  // leave a real token flagged as demo — demo banner on a real session,
  // private_preview nav hidden, auto-login recovery wrongly suppressed), and
  // the demo-end tombstone only gates auto-login until the next successful
  // auth of any kind. The demo hand-off sets the marker AFTER this call, so
  // the default is "a new token is not a demo session unless the hand-off
  // says so".
  setDemoSession(false)
  localStorage.removeItem(DEMO_ENDED_KEY)
  notifyListeners()
}

export interface ClearAccessTokenOptions {
  /**
   * Whether clearing this session counts as the demo session having ended
   * involuntarily (token expiry, forced clear) and should therefore persist
   * the demo-ended tombstone. Defaults to true. Pass false for an EXPLICIT
   * user logout (AppLayout.logout): the visitor chose to leave, so after the
   * reload they must land on the normal login flow, not be re-minted into
   * /demo (which would burn the mint budget against their will).
   */
  demoEnded?: boolean
}

export function clearAccessToken(options?: ClearAccessTokenOptions): void {
  const demoEnded = options?.demoEnded ?? true
  if (demoEnded && isDemoSession()) {
    // Persist the demo-ended signal BEFORE removing the marker, so it outlives
    // the token clear and any reload (App.vue's mount-time check consumes it).
    markDemoSessionEnded()
  }
  localStorage.removeItem(TOKEN_KEY)
  clearRefreshToken()
  setDemoSession(false)
  notifyListeners()
}

export function getAccessToken(): string | null {
  const token = localStorage.getItem(TOKEN_KEY)
  return isValidToken(token) ? token : null
}

export function setRefreshToken(token: string): void {
  storeToken(REFRESH_TOKEN_KEY, token)
}

export function getRefreshToken(): string | null {
  const token = localStorage.getItem(REFRESH_TOKEN_KEY)
  return isValidToken(token) ? token : null
}

function clearRefreshToken(): void {
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

export async function attemptTokenRefresh(): Promise<boolean> {
  if (_refreshingPromise) return _refreshingPromise

  _refreshingPromise = (async () => {
    const refreshToken = getRefreshToken()
    if (!refreshToken) return false

    try {
      const resp = await fetch('/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!resp.ok) return false
      const data = await resp.json()
      setAccessToken(data.access_token)
      if (data.refresh_token) setRefreshToken(data.refresh_token)
      return true
    } catch (err) {
      console.warn('[auth] Token refresh failed:', err)
      return false
    }
  })()

  try {
    return await _refreshingPromise
  } finally {
    _refreshingPromise = null
  }
}

export function getAuthHeaders(): Record<string, string> {
  const token = getAccessToken()
  if (token) {
    return { Authorization: `Bearer ${token}` }
  }
  return {}
}

export function redirectToLogin(): void {
  // If auto-login is configured, the login attempt may still be in
  // progress — skip the hard redirect and let auto-login complete.
  if (getAutoLoginConfig()) {
    return
  }

  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
  }
}
