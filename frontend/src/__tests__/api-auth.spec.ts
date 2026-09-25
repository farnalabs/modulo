import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  attemptTokenRefresh,
  clearAccessToken,
  clearAccessTokenForLogout,
  exitToLogin,
  getAccessToken,
  getAuthHeaders,
  onAuthChange,
  redirectToLogin,
  setAccessToken,
  setDemoSession,
} from '../lib/api/auth'
import { flagCacheKey, serializeFlagCache } from '../config/flagCache'

const TOKEN_KEY = 'modulo_access_token'
// FAR-1197 legacy scrub key — no refresh token is persisted anymore.
const LEGACY_REFRESH_TOKEN_KEY = 'modulo_refresh_token'

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  delete window.__MODULO_CONFIG__
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('auth token lifecycle', () => {
  it('onAuthChange invokes the listener immediately with the stored token', () => {
    setAccessToken('abc')
    const listener = vi.fn()
    const unsubscribe = onAuthChange(listener)

    expect(listener).toHaveBeenCalledWith('abc')
    unsubscribe()
  })

  it('onAuthChange invokes the listener with null when no token is stored', () => {
    const listener = vi.fn()
    const unsubscribe = onAuthChange(listener)

    expect(listener).toHaveBeenCalledWith(null)
    unsubscribe()
  })

  it('setAccessToken persists the token and notifies listeners', () => {
    const listener = vi.fn()
    onAuthChange(listener)
    listener.mockClear()

    setAccessToken('new-token')

    expect(localStorage.getItem(TOKEN_KEY)).toBe('new-token')
    expect(listener).toHaveBeenCalledWith('new-token')
  })

  it('clearAccessToken notifies with null and scrubs the legacy refresh key', () => {
    setAccessToken('abc')
    localStorage.setItem(LEGACY_REFRESH_TOKEN_KEY, 'legacy')
    const listener = vi.fn()
    onAuthChange(listener)
    listener.mockClear()

    clearAccessToken()

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(LEGACY_REFRESH_TOKEN_KEY)).toBeNull()
    expect(listener).toHaveBeenCalledWith(null)
  })

  it('clearAccessTokenForLogout notifies without writing the demo tombstone', () => {
    setAccessToken('abc')
    localStorage.setItem(LEGACY_REFRESH_TOKEN_KEY, 'legacy')
    // Simulate a demo session so we can assert the logout path does NOT mark it ended.
    setDemoSession(true)
    const listener = vi.fn()
    onAuthChange(listener)
    listener.mockClear()

    clearAccessTokenForLogout()

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(LEGACY_REFRESH_TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem('modulo_demo_ended')).toBeNull()
    expect(localStorage.getItem('modulo_demo_session')).toBeNull()
    expect(listener).toHaveBeenCalledWith(null)
  })

  it('clearAccessToken scrubs the persisted flag cache across all org buckets', () => {
    setAccessToken('abc')
    localStorage.setItem(flagCacheKey('org-a'), serializeFlagCache({ mobile_sidebar_rail: true }))
    localStorage.setItem(flagCacheKey(null), serializeFlagCache({ mobile_sidebar_rail: true }))

    clearAccessToken()

    expect(localStorage.getItem(flagCacheKey('org-a'))).toBeNull()
    expect(localStorage.getItem(flagCacheKey(null))).toBeNull()
  })

  it('clearAccessTokenForLogout scrubs the persisted flag cache (shared-device leak)', () => {
    setAccessToken('abc')
    localStorage.setItem(flagCacheKey('org-a'), serializeFlagCache({ mobile_sidebar_rail: true }))

    clearAccessTokenForLogout()

    expect(localStorage.getItem(flagCacheKey('org-a'))).toBeNull()
  })

  it('unsubscribe stops future notifications', () => {
    const listener = vi.fn()
    const unsubscribe = onAuthChange(listener)
    unsubscribe()
    listener.mockClear()

    setAccessToken('x')

    expect(listener).not.toHaveBeenCalled()
  })

  it('getAuthHeaders returns a Bearer header when a token exists', () => {
    setAccessToken('tok-123')
    expect(getAuthHeaders()).toEqual({ Authorization: 'Bearer tok-123' })
  })

  it('getAuthHeaders returns no headers without a token', () => {
    expect(getAuthHeaders()).toEqual({})
  })
})

describe('attemptTokenRefresh', () => {
  function setCsrfCookie(refreshCookieHeader: string): void {
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => `XSRF-TOKEN=csrf-value; ${refreshCookieHeader}`,
    })
  }

  it('POSTs bodyless and sends the CSRF double-submit header from the cookie', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'X-CSRF-Token': 'csrf-value' },
    })
    // The body is GONE: the token never touches script-visible storage again.
    expect(fetchMock.mock.calls[0]![1]!.body).toBeUndefined()
    expect(getAccessToken()).toBe('new-access')
  })

  it('URL-decodes the XSRF cookie value into the header', async () => {
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => 'XSRF-TOKEN=csrf%2Fvalue; modulo_refresh=ref-cookie',
    })
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'X-CSRF-Token': 'csrf/value' },
    })
  })

  it('attaches credentials so the browser sends the httpOnly refresh cookie', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(fetchMock.mock.calls[0]![1]!.credentials).toBe('include')
  })

  it('returns false and keeps the access token when the refresh endpoint is non-ok', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    setAccessToken('old-access')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })),
    )

    await expect(attemptTokenRefresh()).resolves.toBe(false)
    expect(getAccessToken()).toBe('old-access')
  })

  it('returns false on network failure', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down')
      }),
    )
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    await expect(attemptTokenRefresh()).resolves.toBe(false)
    warnSpy.mockRestore()
  })

  it('sends no JSON body at all (the token rides the httpOnly cookie)', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: false,
      status: 401,
      json: async () => ({}),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(false)
    expect(fetchMock.mock.calls[0]![1]!.body).toBeUndefined()
  })

  it('omits the CSRF header when the XSRF cookie is absent', async () => {
    // No XSRF-TOKEN cookie: readCookie returns null, so the request carries an
    // empty header map rather than a stale/undefined X-CSRF-Token value.
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => '',
    })
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: {},
    })
  })

  it('treats a malformed XSRF cookie value as absent instead of throwing', async () => {
    // A corrupted percent-escape makes decodeURIComponent throw URIError; the
    // refresh path must fall back to no CSRF header rather than crash.
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => 'XSRF-TOKEN=%E0%A4; modulo_refresh=ref-cookie',
    })
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: {},
    })
  })

  it('deduplicates concurrent refresh attempts into a single request', async () => {
    setCsrfCookie('modulo_refresh=ref-cookie')
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const [a, b, c] = await Promise.all([
      attemptTokenRefresh(),
      attemptTokenRefresh(),
      attemptTokenRefresh(),
    ])

    expect(a).toBe(true)
    expect(b).toBe(true)
    expect(c).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})


describe('redirectToLogin', () => {
  function fakeLocation(pathname: string, href: string): Location {
    return {
      pathname,
      href,
    } as unknown as Location
  }

  it('redirects to /login when on another route', () => {
    const location = fakeLocation('/dashboard', 'http://localhost/dashboard')
    vi.stubGlobal('location', location)

    redirectToLogin()

    expect(location.href).toBe('/login')
  })

  it('does not redirect when already on /login', () => {
    const location = fakeLocation('/login', 'http://localhost/login')
    vi.stubGlobal('location', location)

    redirectToLogin()

    expect(location.href).toBe('http://localhost/login')
  })

  it('does not redirect when auto-login is configured', () => {
    window.__MODULO_CONFIG__ = { autoLogin: { username: 'demo', password: 'demo' } }
    const location = fakeLocation('/dashboard', 'http://localhost/dashboard')
    vi.stubGlobal('location', location)

    redirectToLogin()

    expect(location.href).toBe('http://localhost/dashboard')
  })
})

describe('exitToLogin', () => {
  function fakeLocation(pathname: string, href: string): Location {
    return {
      pathname,
      href,
    } as unknown as Location
  }

  it('redirects to /login when on another route', () => {
    const location = fakeLocation('/dashboard', 'http://localhost/dashboard')
    vi.stubGlobal('location', location)

    exitToLogin()

    expect(location.href).toBe('/login')
  })

  it('does not redirect when already on /login', () => {
    const location = fakeLocation('/login', 'http://localhost/login')
    vi.stubGlobal('location', location)

    exitToLogin()

    expect(location.href).toBe('http://localhost/login')
  })

  it('redirects to /login even when auto-login is configured', () => {
    window.__MODULO_CONFIG__ = { autoLogin: { username: 'demo', password: 'demo' } }
    const location = fakeLocation('/dashboard', 'http://localhost/dashboard')
    vi.stubGlobal('location', location)

    exitToLogin()

    expect(location.href).toBe('/login')
  })
})
