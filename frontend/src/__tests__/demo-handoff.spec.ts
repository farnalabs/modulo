import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Restore the REAL vue-router (the shared vitest setup mocks it globally) so
// the actual beforeEach guard in ../router/index.ts runs — same override
// pattern as app-bootstrap.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

// qa iter 2: NO module mock — the guard runs the REAL resolveDemoEntry → REAL
// runDemoHandOff, so each test stubs fetch to control the /api/v1/auth/demo
// outcome and the storage effects happen through the true integration path
// (tear down any stored session exactly like logout, store the short-lived
// demo token, set the demo marker).

import router from '../router'
import { resolveDemoEntry, runDemoHandOff } from '../lib/api/demo'
import {
  clearAccessToken,
  getAccessToken,
  isDemoSession,
  setAccessToken,
  setDemoSession,
  wasDemoSessionEnded,
} from '../lib/api/auth'
import { shouldReRunAutoLogin } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'

// Fetch stubs for the demo hand-off endpoint: ok mints a demo token, fail
// returns a non-ok response (demo environment disabled/server error).
function stubDemoHandOffFetch(outcome: 'ok' | 'fail'): ReturnType<typeof vi.fn> {
  const fetchMock =
    outcome === 'ok'
      ? vi.fn(() =>
          Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({ access_token: 'demo-jwt-token' }),
          }),
        )
      : vi.fn(() => Promise.resolve({ ok: false, status: 503, json: async () => ({}) }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  // The dashboard route's manifest entry carries required_tier, so the router
  // guard reads the plan store; activate a fresh pinia per test with the plan
  // already loaded so the guard never attempts a network fetch.
  usePlanStore().loaded = true
})

describe('/demo route hand-off', () => {
  it('performs the hand-off for a tokenless visitor and lands on the dashboard', async () => {
    const fetchMock = stubDemoHandOffFetch('ok')

    await router.push('/demo')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('demo-jwt-token')
    expect(isDemoSession()).toBe(true)
  }, 60_000)

  it('redirects to /login without storing a token when the demo endpoint is unavailable', async () => {
    stubDemoHandOffFetch('fail')

    await router.push('/demo')

    expect(router.currentRoute.value.name).toBe('login')
    expect(getAccessToken()).toBeNull()
    expect(isDemoSession()).toBe(false)
  }, 60_000)

  it('keeps an existing demo session and does not re-mint when navigating to /demo', async () => {
    // qa iter 1: a live demo session must never be torn down by revisiting
    // /demo (Back/Forward must not re-POST and burn the 10/hour mint budget).
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    const fetchMock = stubDemoHandOffFetch('ok')

    await router.push('/demo')

    expect(fetchMock).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('demo-jwt-token')
    expect(isDemoSession()).toBe(true)
  }, 60_000)

  it('keeps a real session intact when navigating to /demo', async () => {
    // qa iter 1: visiting /demo with a REAL session must not log the user out.
    setAccessToken('real-jwt-token')
    const fetchMock = stubDemoHandOffFetch('ok')

    await router.push('/demo')

    expect(fetchMock).not.toHaveBeenCalled()
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(getAccessToken()).toBe('real-jwt-token')
    expect(isDemoSession()).toBe(false)
  }, 60_000)
})

describe('demo session recovery gate (tombstone-only, qa iter 2)', () => {
  it('never re-runs auto-login after a demo session clears', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()

    expect(shouldReRunAutoLogin(true, false, true)).toBe(false)
  })

  it('re-enables auto-login recovery once a fresh login supersedes the demo end', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()
    setAccessToken('fresh-login-token')
    clearAccessToken()

    expect(shouldReRunAutoLogin(true, false, true)).toBe(true)
  })
})

describe('demo-ended tombstone (qa iter 1)', () => {
  it('persists a tombstone when a demo session is cleared, surviving the marker removal', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)

    clearAccessToken()

    // The marker is gone but the tombstone outlives the clear — this is the
    // reload path where previously neither token nor marker existed and
    // first-mount auto-login silently escalated the visitor.
    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBeNull()
    expect(wasDemoSessionEnded()).toBe(true)
    expect(localStorage.getItem('modulo_demo_ended')).not.toBeNull()
  })

  it('does not write a tombstone when a non-demo session is cleared', () => {
    setAccessToken('real-jwt-token')

    clearAccessToken()

    expect(wasDemoSessionEnded()).toBe(false)
  })

  it('clears the tombstone and the demo marker on any new successful auth', () => {
    // Simulate the ended-demo state, then a fresh (real) login.
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()
    expect(wasDemoSessionEnded()).toBe(true)

    setAccessToken('fresh-login-token')

    expect(wasDemoSessionEnded()).toBe(false)
    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBe('fresh-login-token')
  })

  it('clears a stale demo marker when a real login stores a token (two-tab race)', () => {
    // qa iter 1: a real token must never carry the demo flag — previously only
    // clearAccessToken removed the marker, so a two-tab race left a real admin
    // token flagged as demo (demo banner, hidden private_preview nav).
    setDemoSession(true)
    setAccessToken('real-jwt-token')

    expect(isDemoSession()).toBe(false)
    expect(getAccessToken()).toBe('real-jwt-token')
  })
})

describe('demo auth state-machine invariants (qa iter 2)', () => {
  it('setAccessToken clears both the demo marker and the demo-ended tombstone', () => {
    setDemoSession(true)
    localStorage.setItem('modulo_demo_ended', String(Date.now()))

    setAccessToken('real-jwt-token')

    expect(isDemoSession()).toBe(false)
    expect(wasDemoSessionEnded()).toBe(false)
  })

  it('clearAccessToken (default) writes the tombstone iff the demo marker was set', () => {
    // Expiry path: a NON-demo session clear persists nothing.
    setAccessToken('real-jwt-token')
    clearAccessToken()
    expect(wasDemoSessionEnded()).toBe(false)

    // Expiry path: a demo session clear persists the tombstone.
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken()
    expect(wasDemoSessionEnded()).toBe(true)
  })

  it('clearAccessToken({ demoEnded: false }) writes neither the tombstone nor the marker', () => {
    // Explicit logout path (AppLayout.logout): the visitor chose to leave —
    // nothing may re-mint them into /demo after the reload.
    setAccessToken('demo-jwt-token')
    setDemoSession(true)

    clearAccessToken({ demoEnded: false })

    expect(getAccessToken()).toBeNull()
    expect(isDemoSession()).toBe(false)
    expect(wasDemoSessionEnded()).toBe(false)
    expect(localStorage.getItem('modulo_demo_ended')).toBeNull()
  })

  it('explicit logout after a demo session re-enables the normal login flow (no tombstone, recovery allowed)', () => {
    // After an explicit demo logout the state is indistinguishable from a
    // fresh visitor: no token, no marker, no tombstone. Whatever the instance
    // configures for that state (auto-login or /login) is the normal flow.
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken({ demoEnded: false })

    expect(wasDemoSessionEnded()).toBe(false)
    expect(isDemoSession()).toBe(false)
    // Contrast with the expiry path: the tombstone (not the flag) is what
    // blocks auto-login recovery for an involuntarily ended demo session.
    expect(shouldReRunAutoLogin(true, false, true)).toBe(true)
  })

  it('expiry clear after a demo session blocks auto-login recovery via the tombstone', () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    clearAccessToken() // default: expiry semantics

    expect(wasDemoSessionEnded()).toBe(true)
    expect(shouldReRunAutoLogin(true, false, true)).toBe(false)
  })
})

describe('resolveDemoEntry (qa iter 2 shared demo-entry resolver)', () => {
  it("returns 'dashboard' for a live demo session without running the hand-off", async () => {
    setAccessToken('demo-jwt-token')
    setDemoSession(true)
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await resolveDemoEntry()).toBe('dashboard')
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("returns 'dashboard' for a real session with no POST to the demo endpoint", async () => {
    setAccessToken('real-jwt-token')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await resolveDemoEntry()).toBe('dashboard')
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("runs the hand-off exactly once for a tokenless visitor and returns 'dashboard'", async () => {
    const fetchMock = stubDemoHandOffFetch('ok')
    try {
      expect(await resolveDemoEntry()).toBe('dashboard')
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/demo')
      expect(getAccessToken()).toBe('demo-jwt-token')
      expect(isDemoSession()).toBe(true)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("returns 'login' when the hand-off fails", async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    stubDemoHandOffFetch('fail')
    try {
      expect(await resolveDemoEntry()).toBe('login')
      expect(getAccessToken()).toBeNull()
      expect(isDemoSession()).toBe(false)
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })
})

describe('runDemoHandOff implementation (qa iter 1 hardening)', () => {
  // No module mock anywhere in this file, so the imported runDemoHandOff IS
  // the real implementation; these tests stub fetch to exercise the hardening
  // directly.
  it('stores the token, sets the demo marker after the token, and succeeds', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'demo-jwt-token' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await runDemoHandOff()).toBe(true)
      expect(getAccessToken()).toBe('demo-jwt-token')
      expect(isDemoSession()).toBe(true)
      expect(wasDemoSessionEnded()).toBe(false)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('returns false on a non-ok response and reports only the status', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() => Promise.resolve({ ok: false, status: 503, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await runDemoHandOff()).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(isDemoSession()).toBe(false)
      expect(warnSpy).toHaveBeenCalledWith('[demo] demo hand-off failed with status 503')
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('treats a malformed access_token as a hand-off failure, not a false success', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ access_token: 'not a valid token' }),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await runDemoHandOff()).toBe(false)
      // storeToken pattern-checks too — nothing may be persisted.
      expect(getAccessToken()).toBeNull()
      expect(isDemoSession()).toBe(false)
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('returns false when the request fails (network error)', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    vi.stubGlobal('fetch', fetchMock)
    try {
      expect(await runDemoHandOff()).toBe(false)
      expect(getAccessToken()).toBeNull()
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
    }
  })

  it('aborts a hung hand-off at the timeout and returns false', async () => {
    vi.useFakeTimers()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const fetchMock = vi.fn((_url: unknown, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('The operation was aborted.', 'AbortError')),
        )
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    try {
      const pending = runDemoHandOff()

      await vi.advanceTimersByTimeAsync(15_000)

      expect(await pending).toBe(false)
      expect(getAccessToken()).toBeNull()
      expect(warnSpy).toHaveBeenCalledWith('[demo] demo hand-off timed out')
    } finally {
      vi.unstubAllGlobals()
      warnSpy.mockRestore()
      vi.useRealTimers()
    }
  })
})
