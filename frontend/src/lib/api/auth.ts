import { getAutoLoginConfig } from '../../config/runtime'
import { clearFlagCache } from '../../config/flagCache'

const TOKEN_KEY = 'modulo_access_token'
// FAR-1197: the refresh token is no longer persisted in localStorage. It rides
// the httpOnly, Secure, SameSite=strict `modulo_refresh` cookie set by the
// backend, so script-visible storage can never hold a long-lived token again.
// The key string below only exists to scrub the legacy value written by
// pre-FAR-1197 sessions.
const LEGACY_REFRESH_TOKEN_KEY = 'modulo_refresh_token'
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
const TOKEN_PATTERN = /^[A-Za-z0-9._-]+$/
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

// One-time migration: sessions that pre-date FAR-1197 may still have a
// long-lived refresh token sitting in script-visible localStorage. Wipe it on
// first load of the new module so no stale copy survives the upgrade.
if (typeof localStorage !== 'undefined' && localStorage.getItem(LEGACY_REFRESH_TOKEN_KEY) !== null) {
  localStorage.removeItem(LEGACY_REFRESH_TOKEN_KEY)
}

let _authListeners: Array<(token: string | null) => void> = []
let _refreshingPromise: Promise<boolean> | null = null

// Stable per-tab identifier so other tabs can tell refresh hints apart when
// adopting rotated tokens across tabs (pattern: useUiCommandExecutor TAB_ID).
const TAB_ID =
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : Date.now().toString(36)

let _authChannel: BroadcastChannel | null = null
let _authChannelInitialized = false

function getAuthChannel(): BroadcastChannel | null {
  if (_authChannelInitialized) return _authChannel
  _authChannelInitialized = true
  if (typeof BroadcastChannel === 'undefined') return null
  _authChannel = new BroadcastChannel('modulo-auth')
  _authChannel.addEventListener('message', (e: MessageEvent) => {
    const data = e.data || {}
    if (data.type !== 'refresh') return
    // FAR-1197: the refresh token rides the httpOnly `modulo_refresh` cookie,
    // which the browser shares across tabs automatically — there is no longer
    // a localStorage token for a sibling tab to adopt. The message stays as a
    // wake-up signal for tabs listening on auth state changes.
  })
  return _authChannel
}

function broadcastRefreshAdopted(): void {
  const channel = getAuthChannel()
  if (!channel) return
  channel.postMessage({ type: 'refresh', tabId: TAB_ID, ts: Date.now() })
}

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

/**
 * Clear the access token and associated session state.
 *
 * By default (expiry / forced clear), if the session was a demo session, the
 * demo-ended tombstone is persisted so auto-login is not re-triggered. For an
 * EXPLICIT user logout, use `clearAccessTokenForLogout` instead.
 */
export function clearAccessToken(options?: { demoEnded?: boolean }): void {
  const demoEnded = options?.demoEnded ?? true
  if (demoEnded && isDemoSession()) {
    markDemoSessionEnded()
  }
  localStorage.removeItem(TOKEN_KEY)
  // Also wipe any legacy pre-FAR-1197 refresh token still lingering in storage.
  localStorage.removeItem(LEGACY_REFRESH_TOKEN_KEY)
  // FAR-1237 review finding: the persisted flag map is per-session chrome, so
  // it must not outlive the session. On a shared device the next user would
  // otherwise first-paint the previous user's flag-resolved layout (and, if
  // their own flags request failed, keep it indefinitely). Cleared here so
  // BOTH explicit logout and involuntary clears (expiry, 401 recovery) scrub
  // it — the single choke point every teardown path already funnels through.
  clearFlagCache()
  setDemoSession(false)
  notifyListeners()
}

/**
 * Explicit user-initiated logout. Never writes the demo-ended tombstone so
 * the visitor can actually leave the demo and land on the normal login flow.
 */
export function clearAccessTokenForLogout(): void {
  clearAccessToken({ demoEnded: false })
}

export function getAccessToken(): string | null {
  const token = localStorage.getItem(TOKEN_KEY)
  return isValidToken(token) ? token : null
}

// Read a single cookie value (document.cookie is script-readable for
// non-httpOnly cookies — the CSRF double-submit cookie is exposed on purpose).
function readCookie(name: string): string | null {
  const match = document.cookie.match(`^(?:.*; )?${name}=([^;]*).*$`)
  if (!match || !match[1]) return null
  try {
    return decodeURIComponent(match[1])
  } catch {
    // A malformed percent-escape in the cookie value (e.g. `%E0%A4`) makes
    // decodeURIComponent throw URIError. Treat it as "no usable cookie" so a
    // corrupt XSRF-TOKEN can never crash the refresh path.
    return null
  }
}

// Check whether the navigator.locks API is available (Web Locks are supported
// in all modern browsers but absent in some test environments and older WebViews).
function hasWebLocks(): boolean {
  return (
    typeof navigator !== 'undefined' && 'locks' in navigator && typeof navigator.locks?.request === 'function'
  )
}

// FAR-1197: the refresh endpoint is bodyless — the token rides the httpOnly
// `modulo_refresh` cookie, and the route enforces a double-submit CSRF check
// (XSRF-TOKEN cookie vs X-CSRF-Token header). The httpOnly cookie is attached
// automatically by the browser; we only need to echo the CSRF cookie in the
// header. Sibling tabs share the cookie jar, so a rotation by one tab is
// instantly visible to the others — no cross-tab retry loop is needed.
async function doRefresh(): Promise<boolean> {
  try {
    const csrfToken = readCookie('XSRF-TOKEN')
    const resp = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: csrfToken ? { 'X-CSRF-Token': csrfToken } : {},
    })
    if (resp.ok) {
      const data = await resp.json()
      setAccessToken(data.access_token)
      broadcastRefreshAdopted()
      return true
    }
    // 401 (dead session / stolen family) and any other failure fall through to
    // the fatal-session path.
    return false
  } catch (err) {
    console.warn('[auth] Token refresh failed:', err)
    return false
  }
}
export async function attemptTokenRefresh(): Promise<boolean> {
  if (_refreshingPromise) return _refreshingPromise

  _refreshingPromise = (async () => {
    if (hasWebLocks()) {
      // Web Locks serialise cross-tab refresh attempts. The lock is released
      // when the callback settles, allowing the next queued tab to run.
      return navigator.locks!.request(
        'modulo-auth-refresh',
        { mode: 'exclusive' },
        () => doRefresh(),
      )
    }
    return doRefresh()
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

/**
 * Force-exit to /login after an auth-fatal event (failed token refresh,
 * token cleared). Unlike redirectToLogin(), this ALWAYS redirects — even
 * when auto-login config exists. Auto-login only suppresses bounces while
 * a login attempt may be in flight from app startup; a dead session is a
 * different situation and must not silently no-op.
 */
export function exitToLogin(): void {
  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
  }
}
