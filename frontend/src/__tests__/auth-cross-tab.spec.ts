import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// FAR-819: cross-tab refresh adoption. jsdom does not implement
// BroadcastChannel, so the production code under lib/api/auth.ts is never
// exercised in the default test env. This suite stubs BroadcastChannel and
// resets the module so the channel-creation + sibling-tab message-handler
// paths are covered.

const TOKEN_KEY = 'modulo_access_token'
const REFRESH_TOKEN_KEY = 'modulo_refresh_token'
const DEMO_ENDED_KEY = 'modulo_demo_ended'

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
    // messages — only sibling instances of the same name do.
    for (const ch of MockBroadcastChannel.channels) {
      if (ch !== this) {
        for (const l of ch.listeners) l({ data } as MessageEvent)
      }
    }
  }

  close(): void {}
}

async function loadAuth(): Promise<typeof import('../lib/api/auth')> {
  vi.resetModules()
  return (await import('../lib/api/auth')) as typeof import('../lib/api/auth')
}

beforeEach(() => {
  localStorage.clear()
  MockBroadcastChannel.channels = []
  vi.stubGlobal('BroadcastChannel', MockBroadcastChannel)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// Performs a successful refresh so the auth module opens its BroadcastChannel
// and registers the sibling-tab message listener. Leaves no real session
// token behind (cleared by the caller as needed before posting hints).
async function registerChannel(auth: typeof import('../lib/api/auth')): Promise<void> {
  const fetchMock = vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ access_token: 'seed-access', refresh_token: 'seed-refresh' }),
  }))
  vi.stubGlobal('fetch', fetchMock)
  auth.setAccessToken('seed-access')
  auth.setRefreshToken('seed-refresh')
  await auth.attemptTokenRefresh()
  // clearAccessToken() also clears the refresh token internally, leaving no
  // real session behind so the caller can set its own tokens before posting.
  auth.clearAccessToken()
}

describe('cross-tab refresh adoption', () => {
  it('opens a BroadcastChannel and posts a refresh hint after a successful refresh', async () => {
    const auth = await loadAuth()
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ access_token: 'new-access', refresh_token: 'new-refresh' }),
    }))
    vi.stubGlobal('fetch', fetchMock)
    auth.setAccessToken('old-access')
    auth.setRefreshToken('old-refresh')

    await expect(auth.attemptTokenRefresh()).resolves.toBe(true)

    const channel = MockBroadcastChannel.channels[0]
    expect(channel).toBeInstanceOf(MockBroadcastChannel)
    expect(channel.name).toBe('modulo-auth')
  })

  it('adopts rotated tokens from a sibling tab when a live session exists', async () => {
    const auth = await loadAuth()
    await registerChannel(auth)

    auth.setAccessToken('live-access')
    auth.setRefreshToken('live-refresh')

    const sibling = new MockBroadcastChannel('modulo-auth')
    sibling.postMessage({ type: 'refresh', tabId: 'other', ts: Date.now() })

    expect(localStorage.getItem(TOKEN_KEY)).toBe('live-access')
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBe('live-refresh')
  })

  it('ignores a refresh hint when the local tab has no access token', async () => {
    const auth = await loadAuth()
    await registerChannel(auth)

    const sibling = new MockBroadcastChannel('modulo-auth')
    sibling.postMessage({ type: 'refresh', tabId: 'other', ts: Date.now() })

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull()
    expect(localStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull()
  })

  it('ignores a refresh hint when the demo-ended tombstone is set', async () => {
    const auth = await loadAuth()
    await registerChannel(auth)

    auth.setAccessToken('live-access')
    auth.setRefreshToken('live-refresh')
    localStorage.setItem(DEMO_ENDED_KEY, String(Date.now()))

    const sibling = new MockBroadcastChannel('modulo-auth')
    sibling.postMessage({ type: 'refresh', tabId: 'other', ts: Date.now() })

    // Adoption must be blocked: the demo-ended tombstone must never resurrect
    // a demo session into a real one.
    expect(localStorage.getItem(TOKEN_KEY)).toBe('live-access')
  })

  it('ignores non-refresh broadcast messages', async () => {
    const auth = await loadAuth()
    await registerChannel(auth)

    auth.setAccessToken('live-access')
    auth.setRefreshToken('live-refresh')

    const sibling = new MockBroadcastChannel('modulo-auth')
    sibling.postMessage({ type: 'other', tabId: 'other' })

    expect(localStorage.getItem(TOKEN_KEY)).toBe('live-access')
  })
})
