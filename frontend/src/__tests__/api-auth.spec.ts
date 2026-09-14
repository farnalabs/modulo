import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  attemptTokenRefresh,
  clearAccessToken,
  exitToLogin,
  getAccessToken,
  getAuthHeaders,
  getRefreshToken,
  onAuthChange,
  redirectToLogin,
  setAccessToken,
  setRefreshToken,
} from '../lib/api/auth'

const TOKEN_KEY = 'modulo_access_token'
const REFRESH_TOKEN_KEY = 'modulo_refresh_token'

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

  it('clearAccessToken removes both tokens and notifies with null', () => {
    setAccessToken('abc')
    setRefreshToken('ref')
    const listener = vi.fn()
    onAuthChange(listener)
    listener.mockClear()

    clearAccessToken()

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull()
    expect(listener).toHaveBeenCalledWith(null)
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
  it('returns false without issuing a request when no refresh token is stored', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('returns false and keeps tokens when the refresh endpoint is non-ok', async () => {
    setAccessToken('old-access')
    setRefreshToken('old-refresh')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) })),
    )

    await expect(attemptTokenRefresh()).resolves.toBe(false)
    expect(getAccessToken()).toBe('old-access')
    expect(getRefreshToken()).toBe('old-refresh')
  })

  it('returns false on network failure', async () => {
    setRefreshToken('ref')
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

  it('stores the refreshed access token and returns true', async () => {
    setRefreshToken('ref')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'new-access', refresh_token: 'new-refresh' }),
      })),
    )

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(getAccessToken()).toBe('new-access')
    expect(getRefreshToken()).toBe('new-refresh')
  })

  it('does not rotate the refresh token when the response omits one', async () => {
    setRefreshToken('ref')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'new-access' }),
      })),
    )

    await attemptTokenRefresh()
    expect(getRefreshToken()).toBe('ref')
  })

  it('deduplicates concurrent refresh attempts into a single request', async () => {
    setRefreshToken('ref')
    const fetchMock = vi.fn(async () => ({
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

  it('adopts a fresh token without POSTing when a sibling already rotated', async () => {
    // Scenario: the first refresh attempt returns 409 stale_refresh_token.
    // A sibling tab rotated the token to 'fresh-refresh' while we waited.
    // On the retry loop, the pre-POST re-read of localStorage finds the newer
    // token and adopts it without issuing a second POST.
    setRefreshToken('old-refresh')
    const fetchMock = vi.fn(async () => {
      // Simulate the sibling rotating the token between the 409 and the retry
      setRefreshToken('fresh-refresh')
      return {
        ok: false,
        status: 409,
        json: async () => ({ code: 'stale_refresh_token', detail: 'token already used' }),
      }
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(attemptTokenRefresh()).resolves.toBe(true)
    // The pre-POST re-read found 'fresh-refresh' which differs from the entry
    // token 'old-refresh' — adopt was triggered without a second POST.
    expect(getRefreshToken()).toBe('fresh-refresh')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('handles 409 stale_refresh_token by re-reading storage and retrying', async () => {
    // First call returns 409 with stale_refresh_token code. A sibling tab
    // rotated the token in the meantime, so the retry (re-reading localStorage)
    // finds the fresh token and succeeds without a second POST.
    setRefreshToken('stale-refresh')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 409,
        json: async () => ({ code: 'stale_refresh_token', detail: 'token already used' }),
      })
    vi.stubGlobal('fetch', fetchMock)

    // Simulate the sibling rotating the token between the 409 and the retry.
    // Since we're not using fake timers, the setTimeout backoff is real but
    // short (150ms). We need to swap the token right after the first fetch
    // resolves. Use a proxy approach: the first fetch mock swaps the token.
    fetchMock.mockImplementationOnce(async () => {
      // Sibling rotated while we waited
      setRefreshToken('sibling-refresh')
      return {
        ok: false,
        status: 409,
        json: async () => ({ code: 'stale_refresh_token', detail: 'token already used' }),
      }
    })

    // The retry re-reads localStorage and finds 'sibling-refresh' which differs
    // from the entry token — adopt path triggers.
    await expect(attemptTokenRefresh()).resolves.toBe(true)
    expect(getRefreshToken()).toBe('sibling-refresh')
  })

  it('returns false after exhausting stale retries without a fresh token', async () => {
    setRefreshToken('stale-refresh')
    vi.useFakeTimers()
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 409,
      json: async () => ({ code: 'stale_refresh_token', detail: 'token already used' }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const promise = attemptTokenRefresh()
    // Advance past all retry delays (150 + 300 + 600 = 1050ms)
    await vi.advanceTimersByTimeAsync(2000)
    await expect(promise).resolves.toBe(false)
    // Three attempts total (initial + 3 retries that exhaust the budget).
    expect(fetchMock).toHaveBeenCalledTimes(4)
    vi.useRealTimers()
  })

  it('persists the rotated refresh token before the refresh promise resolves', async () => {
    setRefreshToken('old-refresh')
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'new-access', refresh_token: 'new-refresh' }),
      })),
    )

    let persistedAtResolve = false
    const pending = attemptTokenRefresh().then((ok) => {
      // Runs on the first microtask AFTER the refresh promise settles — the
      // rotated tokens must already be in storage at that instant, proving they
      // were persisted before the promise resolved (not after an external await).
      persistedAtResolve = getAccessToken() === 'new-access' && getRefreshToken() === 'new-refresh'
      return ok
    })

    await expect(pending).resolves.toBe(true)
    expect(persistedAtResolve).toBe(true)
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
