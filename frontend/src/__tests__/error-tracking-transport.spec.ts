import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { getAccessTokenMock, onAuthChangeMock } = vi.hoisted(() => ({
  getAccessTokenMock: vi.fn<() => string | null>(() => 'tok-123'),
  onAuthChangeMock: vi.fn<(cb: (token: string | null) => void) => () => void>(() => () => {}),
}))

vi.mock('../lib/api/client', () => ({
  getAccessToken: getAccessTokenMock,
  onAuthChange: onAuthChangeMock,
}))

const fetchMock = vi.fn()

function keyCalls(): unknown[][] {
  return fetchMock.mock.calls.filter((c) => String(c[0]).includes('/session-key'))
}

function ingestCalls(): unknown[][] {
  return fetchMock.mock.calls.filter((c) => String(c[0]).includes('/ingest'))
}

function evt(message: string): { level: 'error'; message: string } {
  return { level: 'error', message }
}

function defaultFetchImpl(): void {
  fetchMock.mockImplementation(async (url: string) => {
    if (url.includes('/session-key')) {
      return { ok: true, json: async () => ({ key: 'sk-1' }) }
    }
    if (url.includes('/ingest')) {
      return { ok: true }
    }
    throw new Error(`Unexpected URL: ${url}`)
  })
}

/**
 * Mock crypto.subtle with instantly-resolving promises. The real Node
 * webcrypto resolves on threadpool event-loop turns, which stalls the flush
 * chain under fake timers and leaks async work across tests.
 */
function mockCryptoHappy(): void {
  vi.spyOn(crypto.subtle, 'importKey').mockResolvedValue({} as CryptoKey)
  vi.spyOn(crypto.subtle, 'sign').mockResolvedValue(new Uint8Array(32).buffer)
  vi.spyOn(crypto.subtle, 'digest').mockResolvedValue(new Uint8Array(32).buffer)
}

/** Re-imports the transport module with fresh module-level state. */
async function loadTransport() {
  vi.resetModules()
  return import('../lib/error-tracking/transport')
}

/** Yields event-loop turns so the (fully mocked, microtask-only) flush chain completes. */
async function drain(): Promise<void> {
  await vi.advanceTimersByTimeAsync(0)
}

beforeEach(() => {
  vi.clearAllMocks()
  getAccessTokenMock.mockReturnValue('tok-123')
  onAuthChangeMock.mockReturnValue(() => {})
  defaultFetchImpl()
  window.fetch = fetchMock as unknown as typeof fetch
  mockCryptoHappy()
  vi.spyOn(console, 'warn').mockImplementation(() => undefined)
})

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('transport happy path', () => {
  it('flushes a full batch with session key, auth and HMAC signature headers', async () => {
    const transport = await loadTransport()
    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`error-${i}`))
    }

    await vi.waitFor(() => expect(ingestCalls()).toHaveLength(1))

    expect(keyCalls()).toHaveLength(1)
    const [, opts] = ingestCalls()[0] as [string, { method: string; headers: Record<string, string>; body: string }]
    expect(opts.method).toBe('POST')
    expect(opts.headers['Content-Type']).toBe('application/json')
    expect(opts.headers.Authorization).toBe('Bearer tok-123')
    expect(opts.headers['X-Modulo-Error-Token']).toMatch(/^[0-9a-f]{64}$/)
    const body = JSON.parse(opts.body) as { events: Array<{ message: string }> }
    expect(body.events.map((e) => e.message)).toEqual(
      Array.from({ length: 10 }, (_, i) => `error-${i}`),
    )
  })

  it('omits the Authorization header when no access token is present', async () => {
    getAccessTokenMock.mockReturnValue(null)
    const transport = await loadTransport()
    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`e${i}`))
    }
    await vi.waitFor(() => expect(ingestCalls()).toHaveLength(1))
    const [, opts] = ingestCalls()[0] as [string, { headers: Record<string, string> }]
    expect(opts.headers.Authorization).toBeUndefined()
  })

  it('debounces a single event on a 5s timer instead of flushing immediately', async () => {
    vi.useFakeTimers()
    const transport = await loadTransport()
    transport.enqueueError(evt('only one'))
    expect(ingestCalls()).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(4999)
    expect(ingestCalls()).toHaveLength(0)
    await vi.advanceTimersByTimeAsync(1)
    expect(ingestCalls()).toHaveLength(1)
  })

  it('does nothing when error tracking is disabled', async () => {
    ;(window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__ = true
    const transport = await loadTransport()
    transport.enqueueError(evt('ignored'))
    await transport.flush()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('transport failure handling', () => {
  it('requeues and retries after the session-key request fails', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/session-key')) return { ok: false, status: 403 }
      if (url.includes('/ingest')) return { ok: true }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const transport = await loadTransport()
    transport.enqueueError(evt('doomed first attempt'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(ingestCalls()).toHaveLength(0)

    defaultFetchImpl()
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(ingestCalls()).toHaveLength(1)
    const [, opts] = ingestCalls()[0] as [string, { body: string }]
    expect(JSON.parse(opts.body).events[0].message).toBe('doomed first attempt')
  })

  it('warns and retries when the session-key request throws', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/session-key')) throw new Error('key endpoint down')
      if (url.includes('/ingest')) return { ok: true }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const transport = await loadTransport()
    transport.enqueueError(evt('retry me'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] Failed to fetch session key:',
      expect.any(Error),
    )
    expect(ingestCalls()).toHaveLength(0)

    defaultFetchImpl()
    await vi.advanceTimersByTimeAsync(1000)
    await vi.advanceTimersByTimeAsync(1000)
    expect(ingestCalls()).toHaveLength(1)
  })

  it('retries a 5xx ingest with backoff and drops after the third retry', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/session-key')) return { ok: true, json: async () => ({ key: 'sk-1' }) }
      if (url.includes('/ingest')) return { ok: false, status: 500 }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const transport = await loadTransport()
    transport.enqueueError(evt('persistent failure'))
    // 1st flush + 3 backoff retries (1s, 5s, 30s), then the 4th requeue drops.
    await vi.advanceTimersByTimeAsync(90000)

    expect(ingestCalls()).toHaveLength(4)
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('Dropping event after'),
      3,
      'persistent failure',
    )
  })

  it('drops events permanently on a 4xx response', async () => {
    vi.useFakeTimers()
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/session-key')) return { ok: true, json: async () => ({ key: 'sk-1' }) }
      if (url.includes('/ingest')) return { ok: false, status: 422 }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const transport = await loadTransport()
    transport.enqueueError(evt('bad payload'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(ingestCalls()).toHaveLength(1)
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] Dropping %d events due to %d response',
      1,
      422,
      ['bad payload'],
    )
    await vi.advanceTimersByTimeAsync(60000)
    expect(ingestCalls()).toHaveLength(1)
  })

  it('requeues events when the ingest fetch throws', async () => {
    vi.useFakeTimers()
    let ingestAttempts = 0
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/session-key')) return { ok: true, json: async () => ({ key: 'sk-1' }) }
      if (url.includes('/ingest')) {
        ingestAttempts += 1
        throw new Error('network unreachable')
      }
      throw new Error(`Unexpected URL: ${url}`)
    })
    const transport = await loadTransport()
    transport.enqueueError(evt('offline'))
    await vi.advanceTimersByTimeAsync(90000)
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] Ingest fetch failed, queuing batch for retry:',
      expect.any(Error),
    )
    expect(ingestAttempts).toBe(4)
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('Dropping event after'),
      3,
      'offline',
    )
  })
})

describe('transport rate limiting', () => {
  it('requeues (and eventually drops) a flush beyond 10 requests per minute', async () => {
    vi.useFakeTimers()
    const transport = await loadTransport()

    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`m${i}`))
    }
    await drain()
    expect(ingestCalls()).toHaveLength(1)

    // Nine more successful flushes exhaust the 10-per-window budget.
    for (let i = 0; i < 9; i++) {
      transport.enqueueError(evt(`x${i}`))
      await transport.flush()
    }
    expect(ingestCalls()).toHaveLength(10)

    // The 11th flush inside the same window is rate-limited and requeued.
    transport.enqueueError(evt('rate-limited final'))
    await transport.flush()
    expect(ingestCalls()).toHaveLength(10)

    // Retry flushes stay rate-limited while the 60s window is still fresh.
    // The +100ns epsilon on each advance guards against boundary-exact timers
    // not firing (strictly-less-than scheduling in the fake timer clock).
    await vi.advanceTimersByTimeAsync(7100)
    expect(ingestCalls()).toHaveLength(10)
    await vi.advanceTimersByTimeAsync(5100)
    expect(ingestCalls()).toHaveLength(10)
    await vi.advanceTimersByTimeAsync(30100)
    expect(ingestCalls()).toHaveLength(10)

    // Once the window slides past the burst, the requeued batch goes through.
    // Advance in coarse steps until the flush lands (the exact due time sits a
    // couple of backoff generations out; behaviour, not the tick, is the point).
    for (let i = 0; i < 100; i++) {
      await vi.advanceTimersByTimeAsync(1000)
      if (ingestCalls().length > 10) break
    }
    expect(ingestCalls()).toHaveLength(11)
  })
})

describe('transport lifecycle', () => {
  it('re-fetches the session key after an auth-state change', async () => {
    // Holder object defeats TS narrowing (the callback is assigned inside
    // the mock implementation, which control-flow analysis cannot see).
    const authChangeRef: { cb: ((token: string | null) => void) | null } = { cb: null }
    onAuthChangeMock.mockImplementation((cb: (token: string | null) => void) => {
      authChangeRef.cb = cb
      return () => {
        authChangeRef.cb = null
      }
    })
    const transport = await loadTransport()
    transport.initTransport(onAuthChangeMock)
    transport.enqueueError(evt('before auth change'))
    await transport.flush()
    expect(keyCalls()).toHaveLength(1)

    authChangeRef.cb?.(null)

    transport.enqueueError(evt('after auth change'))
    await transport.flush()
    expect(keyCalls()).toHaveLength(2)
    expect(ingestCalls()).toHaveLength(2)
  })

  it('disposeTransport clears timers, pending events and the auth subscription', async () => {
    vi.useFakeTimers()
    const unsub = vi.fn()
    onAuthChangeMock.mockReturnValue(unsub)
    const transport = await loadTransport()
    transport.initTransport(onAuthChangeMock)

    transport.enqueueError(evt('never flushed'))
    expect(vi.getTimerCount()).toBe(1)

    transport.disposeTransport()
    expect(vi.getTimerCount()).toBe(0)
    expect(unsub).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(10000)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('an in-flight flush aborts after disposeTransport bumps the generation', async () => {
    vi.useFakeTimers()
    // Holder object defeats TS narrowing (assigned inside the fetch mock).
    const pendingKeyFetch: { resolve: ((value: unknown) => void) | null } = { resolve: null }
    fetchMock.mockImplementation((url: string) => {
      if (url.includes('/session-key')) {
        return new Promise((resolve) => {
          pendingKeyFetch.resolve = resolve
        })
      }
      if (url.includes('/ingest')) return { ok: true }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    const transport = await loadTransport()
    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`e${i}`))
    }
    transport.disposeTransport()
    pendingKeyFetch.resolve?.({ ok: true, json: async () => ({ key: 'sk-late' }) })
    await vi.advanceTimersByTimeAsync(10)
    expect(ingestCalls()).toHaveLength(0)
  })
})

describe('transport signing fallback', () => {
  it('falls back to SHA-256 when HMAC key import fails', async () => {
    vi.spyOn(crypto.subtle, 'importKey').mockRejectedValue(new Error('hmac unavailable'))
    const transport = await loadTransport()
    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`e${i}`))
    }
    await vi.waitFor(() => expect(ingestCalls()).toHaveLength(1))
    const [, opts] = ingestCalls()[0] as [string, { headers: Record<string, string> }]
    expect(opts.headers['X-Modulo-Error-Token']).toMatch(/^[0-9a-f]{64}$/)
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] HMAC sign failed, falling back to SHA-256:',
      expect.any(Error),
    )
  })

  it('omits the signature header when every signing strategy fails', async () => {
    vi.spyOn(crypto.subtle, 'importKey').mockRejectedValue(new Error('hmac unavailable'))
    vi.spyOn(crypto.subtle, 'digest').mockRejectedValue(new Error('digest unavailable'))
    const transport = await loadTransport()
    for (let i = 0; i < 10; i++) {
      transport.enqueueError(evt(`e${i}`))
    }
    await vi.waitFor(() => expect(ingestCalls()).toHaveLength(1))
    const [, opts] = ingestCalls()[0] as [string, { headers: Record<string, string> }]
    expect(opts.headers['X-Modulo-Error-Token']).toBeUndefined()
    expect(console.warn).toHaveBeenCalledWith(
      '[error-tracking] SHA-256 digest fallback also failed:',
      expect.any(Error),
    )
  })
})
