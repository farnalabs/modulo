import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  attemptTokenRefresh,
  clearAccessToken,
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
