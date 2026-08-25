import { getAutoLoginConfig } from '../../config/runtime'

const TOKEN_KEY = 'modulo_access_token'
const REFRESH_TOKEN_KEY = 'modulo_refresh_token'

// S8475: only store well-formed, opaque token strings in browser storage.
// Rejects anything containing control/whitespace chars or exceeding a sane
// length, so tainted/untrusted data can never be persisted as a token.
const TOKEN_PATTERN = /^[A-Za-z0-9._\-]+$/
const MAX_TOKEN_LENGTH = 8192

function isValidToken(value: unknown): value is string {
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
  notifyListeners()
}

export function clearAccessToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  clearRefreshToken()
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
