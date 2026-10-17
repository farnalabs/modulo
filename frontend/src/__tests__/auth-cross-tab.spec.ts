import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// FAR-1197: cross-tab refresh is now automatic — the httpOnly `modulo_refresh`
// cookie is shared by the browser's cookie jar, so there is no localStorage
// token for a sibling tab to "adopt". BroadcastChannel machinery stays as a
// wake-up signal. jsdom does not implement BroadcastChannel, so this suite
// stubs the channel to cover the creation + post-hint paths.
//
// FAR-1852: the wake-up signal is now WIRed — a received `refresh` hint
// re-fires this tab's onAuthChange listeners (the fresh access token is
// already visible in shared localStorage), so the SSE stream re-arm (FAR-1842)
// fires in EVERY open tab, not only the one that refreshed.

// FAR-1197 legacy scrub key — the module wipes any pre-upgrade refresh token
// from script-visible storage on first load.
const LEGACY_REFRESH_TOKEN_KEY = 'modulo_refresh_token'
const TOKEN_KEY = 'modulo_access_token'

class MockBroadcastChannel {
  static channels: MockBroadcastChannel[] = []
  name: string
  listeners: Array<(e: MessageEvent) => void> = []
  posted: unknown[] = []

  constructor(name: string) {
    this.name = name
    MockBroadcastChannel.channels.push(this)
  }

  addEventListener(_type: 'message', cb: (e: MessageEvent) => void): void {
    this.listeners.push(cb)
  }

  removeEventListener(): void {}

  postMessage(data: unknown): void {
    this.posted.push(data)
    // Per the BroadcastChannel spec, a channel does NOT receive its own
    // messages — deliver to every OTHER registered channel.
    for (const ch of MockBroadcastChannel.channels) {
      if (ch === this) continue
      for (const listener of ch.listeners) listener({ data } as MessageEvent)
    }
  }
}

async function loadAuth(): Promise<typeof import('../lib/api/auth')> {
  vi.resetModules()
  return (await import('../lib/api/auth')) as typeof import('../lib/api/auth')
}

beforeEach(() => {
  localStorage.clear()
  MockBroadcastChannel.channels = []
  vi.stubGlobal('BroadcastChannel', MockBroadcastChannel)
  Object.defineProperty(document, 'cookie', {
    configurable: true,
    get: () => 'XSRF-TOKEN=csrf-value; modulo_refresh=ref-cookie',
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('cross-tab refresh (cookie transport)', () => {
  it('opens a BroadcastChannel and posts a refresh hint after a successful refresh', async () => {
    const auth = await loadAuth()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    auth.setAccessToken('old-access')

    await expect(auth.attemptTokenRefresh()).resolves.toBe(true)

    const channel = MockBroadcastChannel.channels[0]
    expect(channel).toBeInstanceOf(MockBroadcastChannel)
    expect(channel.name).toBe('modulo-auth')
  })

  it('sends no JSON body — the token rides the httpOnly cookie automatically', async () => {
    const auth = await loadAuth()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    auth.setAccessToken('old-access')

    await auth.attemptTokenRefresh()
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'X-CSRF-Token': 'csrf-value' },
    })
  })

  it('a sibling hint is a wake-up only: it must not mutate local storage', async () => {
    const auth = await loadAuth()
    // Simulate the seed refresh registering the sibling listener.
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'seed-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    auth.setAccessToken('replaced-by-hint')
    await auth.attemptTokenRefresh()

    const sibling = new MockBroadcastChannel('modulo-auth')
    sibling.postMessage({ type: 'refresh', tabId: 'other', ts: Date.now() })

    // Nothing is adopted or cleared: the cookie is shared automatically, so a
    // hint must not write anything into this tab's storage.
    expect(localStorage.getItem(TOKEN_KEY)).toBe('seed-access')
  })

  it('scrubs the legacy localStorage refresh token on module load', async () => {
    localStorage.setItem(LEGACY_REFRESH_TOKEN_KEY, 'pre-far-1197-token')
    expect(localStorage.getItem(LEGACY_REFRESH_TOKEN_KEY)).toBe('pre-far-1197-token')

    await loadAuth()

    expect(localStorage.getItem(LEGACY_REFRESH_TOKEN_KEY)).toBeNull()
  })
})

describe('cross-tab auth listeners (FAR-1852)', () => {
  it('opens the modulo-auth channel when an auth listener registers', async () => {
    const auth = await loadAuth()
    const unsubscribe = auth.onAuthChange(vi.fn())

    expect(MockBroadcastChannel.channels).toHaveLength(1)
    expect(MockBroadcastChannel.channels[0].name).toBe('modulo-auth')
    unsubscribe()
  })

  it('re-fires auth listeners when a sibling tab broadcasts a refresh', async () => {
    const auth = await loadAuth()
    const listener = vi.fn()
    const unsubscribe = auth.onAuthChange(listener)
    listener.mockClear()

    // The sibling tab's refresh already rotated the token in shared
    // localStorage; the broadcast only has to wake THIS tab's listeners.
    localStorage.setItem(TOKEN_KEY, 'fresh-token')
    new MockBroadcastChannel('modulo-auth').postMessage({
      type: 'refresh',
      tabId: 'sibling',
      ts: 1,
    })

    expect(listener).toHaveBeenCalledWith('fresh-token')
    unsubscribe()
  })

  it('ignores broadcast messages that are not refresh hints', async () => {
    const auth = await loadAuth()
    const listener = vi.fn()
    const unsubscribe = auth.onAuthChange(listener)
    listener.mockClear()

    new MockBroadcastChannel('modulo-auth').postMessage({ type: 'unrelated' })

    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })

  it('does not throw when BroadcastChannel is unavailable (fail-safe)', async () => {
    vi.stubGlobal('BroadcastChannel', undefined)
    const auth = await loadAuth()

    expect(() => auth.onAuthChange(vi.fn())).not.toThrow()
    expect(() => auth.setAccessToken('tok')).not.toThrow()
    expect(localStorage.getItem(TOKEN_KEY)).toBe('tok')
  })

  it('the refresh hint broadcast carries type=refresh with a tabId', async () => {
    const auth = await loadAuth()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    auth.onAuthChange(vi.fn()) // opens the channel (receive side)

    await auth.attemptTokenRefresh()

    const channel = MockBroadcastChannel.channels[0]
    expect(channel.posted).toContainEqual(
      expect.objectContaining({ type: 'refresh', tabId: expect.any(String) }),
    )
  })
})

describe('SSE stream re-arm across tabs (FAR-1852)', () => {
  type EventBusModule = typeof import('../composables/useEventStream')

  /**
   * Drive the REAL useEventStream + REAL auth module to a terminal
   * auth_failed stop: the SSE fetch 401s and the in-band refresh attempt
   * fails. Returns the live eventBus plus a counter of SSE connection
   * attempts, so tests can assert the cross-tab re-arm without re-importing.
   */
  async function reachAuthFailed(): Promise<{
    eventBus: EventBusModule['eventBus']
    auth: typeof import('../lib/api/auth')
    sseAttempts: () => number
  }> {
    vi.resetModules()
    let sseAttempts = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url === '/api/v1/auth/refresh') {
          return { ok: false, status: 401, json: async () => ({}) } as Response
        }
        sseAttempts++
        if (sseAttempts === 1) {
          return { ok: false, status: 401, body: null } as Response
        }
        // Re-armed attempt: a healthy stream that stays open.
        return {
          ok: true,
          body: {
            getReader: () => ({
              read: () => new Promise(() => {}),
              cancel: vi.fn().mockResolvedValue(undefined),
            }),
          },
        } as unknown as Response
      }),
    )

    const { eventBus } = await import('../composables/useEventStream')
    // Same module registry as useEventStream's import — its module-scope
    // onAuthChange re-arm is registered on THIS auth instance.
    const auth = await import('../lib/api/auth')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)

    expect(eventBus.reconnectRequired).toBe(true)
    return { eventBus, auth, sseAttempts: () => sseAttempts }
  }

  it('re-arms the stream when a sibling tab refreshes the token', async () => {
    vi.useFakeTimers()
    const { eventBus, sseAttempts } = await reachAuthFailed()
    expect(sseAttempts()).toBe(1)

    // Sibling tab refreshed: shared storage holds the fresh token and the
    // broadcast wakes this tab's auth listeners, which re-arm the stream.
    localStorage.setItem(TOKEN_KEY, 'fresh-token')
    new MockBroadcastChannel('modulo-auth').postMessage({
      type: 'refresh',
      tabId: 'sibling',
      ts: 1,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(sseAttempts()).toBe(2)
    expect(eventBus.state).toBe('connected')
    expect(eventBus.reconnectRequired).toBe(false)
  })

  it('does NOT re-arm the stream on logout / token clear', async () => {
    vi.useFakeTimers()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { eventBus, auth, sseAttempts } = await reachAuthFailed()

    auth.clearAccessToken() // notifies local listeners with null — must not restart
    await vi.advanceTimersByTimeAsync(0)

    expect(sseAttempts()).toBe(1)
    expect(eventBus.reconnectRequired).toBe(true)
  })
})
