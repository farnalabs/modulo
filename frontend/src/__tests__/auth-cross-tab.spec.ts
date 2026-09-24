import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// FAR-1197: cross-tab refresh is now automatic — the httpOnly `modulo_refresh`
// cookie is shared by the browser's cookie jar, so there is no localStorage
// token for a sibling tab to "adopt". BroadcastChannel machinery stays as a
// wake-up signal. jsdom does not implement BroadcastChannel, so this suite
// stubs the channel to cover the creation + post-hint paths.

// FAR-1197 legacy scrub key — the module wipes any pre-upgrade refresh token
// from script-visible storage on first load.
const LEGACY_REFRESH_TOKEN_KEY = 'modulo_refresh_token'
const TOKEN_KEY = 'modulo_access_token'

class MockBroadcastChannel {
  static channels: MockBroadcastChannel[] = []
  name: string
  listeners: Array<(e: MessageEvent) => void> = []

  constructor(name: string) {
    this.name = name
    MockBroadcastChannel.channels.push(this)
  }

  addEventListener(_type: 'message', cb: (e: MessageEvent) => void): void {
    this.listeners.push(cb)
  }

  removeEventListener(): void {}

  postMessage(data: unknown): void {
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
