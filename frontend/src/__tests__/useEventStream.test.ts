import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { EventBusEvent } from '../types/events'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'test-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer test-token' })),
}))

let pushEvent: (eventType: string, data: Record<string, unknown>) => void
let pushRawSse: (raw: string) => void
let endStream: () => void

beforeEach(() => {
  vi.resetModules()

  const queue: Array<{ done: boolean; value: Uint8Array }> = []
  let resolveNext: ((value: { done: boolean; value: Uint8Array }) => void) | null = null

  const encoder = new TextEncoder()

  const mockReader = {
    read: vi.fn().mockImplementation(() => {
      if (queue.length > 0) {
        return Promise.resolve(queue.shift()!)
      }
      return new Promise((resolve) => {
        resolveNext = resolve
      })
    }),
    cancel: vi.fn().mockResolvedValue(undefined),
  }

  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true,
    body: { getReader: () => mockReader },
  } as unknown as Response)

  pushRawSse = (raw: string) => {
    const encoded = encoder.encode(raw)
    if (resolveNext) {
      const resolve = resolveNext
      resolveNext = null
      resolve({ done: false, value: encoded })
    } else {
      queue.push({ done: false, value: encoded })
    }
  }

  pushEvent = (eventType: string, data: Record<string, unknown>) => {
    pushRawSse(`event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`)
  }

  endStream = () => {
    if (resolveNext) {
      const resolve = resolveNext
      resolveNext = null
      resolve({ done: true, value: new Uint8Array() })
    } else {
      queue.push({ done: true, value: new Uint8Array() })
    }
  }
})

function triggerEvent(data: Record<string, unknown>) {
  pushEvent('resource_changed', data)
}

function tick() {
  return new Promise<void>((resolve) => queueMicrotask(resolve))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('eventBus', () => {
  it('subscribe adds handler and connects', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    const unsub = eventBus.subscribe('run', handler)
    await tick()
    expect(fetch).toHaveBeenCalledWith(
      '/api/v1/events',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer test-token' }),
      }),
    )
    unsub()
  })

  it('SSE message triggers handler', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await tick()
    const event = { type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' }
    triggerEvent(event)
    await vi.waitFor(() => expect(handler).toHaveBeenCalledWith(event))
  })

  it('unsubscribe removes handler', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    const unsub = eventBus.subscribe('run', handler)
    await tick()
    unsub()
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    await tick()
    expect(handler).not.toHaveBeenCalled()
  })

  it('handler is not called for different resource type', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('pipeline', handler)
    await tick()
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    await tick()
    expect(handler).not.toHaveBeenCalled()
  })

  it('connected ref is reactive', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    expect(eventBus.connected).toBe(false)
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await tick()
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    await tick()
    expect(eventBus.connected).toBe(true)
  })

  it('connected becomes false on stream end', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await tick()
    expect(eventBus.connected).toBe(true)
    endStream()
    await tick()
    await tick()
    expect(eventBus.connected).toBe(false)
    // Unsubscribing the last handler disconnects and cancels the reconnect
    // timer scheduled on stream end, so no stray timer leaks into later tests.
    eventBus.unsubscribe('run', handler)
  })

  it('schedules a reconnect when the HTTP response is not ok', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockResolvedValue({ ok: false, body: null } as unknown as Response)
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(eventBus.connected).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    expect(fetch).toHaveBeenCalledTimes(2)
    vi.useRealTimers()
  })

  it('keeps retrying on a non-ok response forever (FAR-250: no attempt cap)', async () => {
    // Regression for the old bug: the 10-attempt cap permanently killed the
    // stream during long outages. A retryable failure must NEVER give up.
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 500, body: null } as unknown as Response)
    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(300_000)
    // Comfortably past the old cap of 11 total attempts (1 + 10 retries).
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(11)
    expect(eventBus.state).toBe('reconnecting')
    expect(eventBus.reconnectRequired).toBe(false)
    vi.useRealTimers()
  })

  it('reconnects a stream that has ended', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await tick()
    expect(eventBus.connected).toBe(true)
    endStream()
    await tick()
    await tick()
    expect(eventBus.connected).toBe(false)
    eventBus.reconnect()
    await tick()
    expect(eventBus.connected).toBe(true)
  })

  it('ignores SSE events other than resource_changed', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    await tick()
    pushEvent('token', { type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    await tick()
    expect(handler).not.toHaveBeenCalled()
  })

  it('ignores malformed SSE payloads and keeps processing later events', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    eventBus.subscribe('run', handler)
    await tick()
    pushRawSse('event: resource_changed\ndata: {not valid json\n\n')
    await vi.waitFor(() => expect(warnSpy).toHaveBeenCalled())
    const event = { type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' }
    triggerEvent(event)
    await vi.waitFor(() => expect(handler).toHaveBeenCalledWith(event))
  })
})

describe('classified reconnect (FAR-250)', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it.each([401, 403, 429])('stops retrying on %i and surfaces the reconnect banner', async (status: number) => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.mocked(fetch).mockResolvedValue({ ok: false, status, body: null } as unknown as Response)

    const { eventBus } = await import('../composables/useEventStream')
    const unsub = eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)

    expect(fetch).toHaveBeenCalledTimes(1)
    expect(eventBus.state).toBe('auth_failed')
    expect(eventBus.reconnectRequired).toBe(true)
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('Stopping stream'))

    // No retries ever — a fresh token / freed connection cap is required.
    await vi.advanceTimersByTimeAsync(600_000)
    expect(fetch).toHaveBeenCalledTimes(1)

    // New subscribers must NOT auto-restart while auth-stopped.
    eventBus.subscribe('other', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    unsub()
  })

  it('reconnect() restarts the stream after a 4xx stop with a fresh token', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 401, body: null } as unknown as Response)

    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(eventBus.reconnectRequired).toBe(true)

    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      body: { getReader: () => ({ read: () => new Promise(() => {}), cancel: vi.fn().mockResolvedValue(undefined) }) },
    } as unknown as Response)
    eventBus.reconnect()
    await vi.advanceTimersByTimeAsync(0)

    expect(fetch).toHaveBeenCalledTimes(2)
    expect(eventBus.state).toBe('connected')
    expect(eventBus.reconnectRequired).toBe(false)
  })

  it('keeps retrying after network errors forever', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    vi.mocked(fetch).mockRejectedValue(new TypeError('fetch failed'))

    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(300_000)
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(11)
    expect(eventBus.state).toBe('reconnecting')
  })

  it('uses half-jitter backoff: first retry lands at exp/2 when random()=0', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 500, body: null } as unknown as Response)

    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(499)
    expect(fetch).toHaveBeenCalledTimes(1) // not yet — delay floor is 500ms
    await vi.advanceTimersByTimeAsync(1)
    expect(fetch).toHaveBeenCalledTimes(2) // fires at exactly 500ms
  })

  it('uses half-jitter backoff: first retry waits the full exp when random()=1', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0.9999999)
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 500, body: null } as unknown as Response)

    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(fetch).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(600)
    expect(fetch).toHaveBeenCalledTimes(1) // floor 500, ceiling 1000 — not yet at 600 with high jitter
    await vi.advanceTimersByTimeAsync(400)
    expect(fetch).toHaveBeenCalledTimes(2) // by 1000ms ceiling
  })

  it('fires the debounced onReconnect backfill exactly once after a successful reconnect', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)

    const { eventBus } = await import('../composables/useEventStream')
    const backfill = vi.fn()
    const unsub = eventBus.subscribe('run', vi.fn())
    const offBackfill = eventBus.onReconnect(backfill)
    await vi.advanceTimersByTimeAsync(0)
    expect(eventBus.connected).toBe(true)
    // First successful connect is NOT a reconnect — no backfill yet.
    expect(backfill).not.toHaveBeenCalled()

    endStream() // server closes -> retry path (500ms with random=0)
    await vi.advanceTimersByTimeAsync(500)
    expect(eventBus.connected).toBe(true) // reconnected (retry fired at t+500)

    // Debounce window: not fired before 1500ms after the reconnect...
    await vi.advanceTimersByTimeAsync(1499)
    expect(backfill).not.toHaveBeenCalled()
    // ...fired exactly once after it.
    await vi.advanceTimersByTimeAsync(1)
    expect(backfill).toHaveBeenCalledTimes(1)
    // A second reconnect schedules its own debounced batch — one call per
    // reconnect window (the debounce coalesces bursts inside a window).
    endStream()
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(1500)
    expect(backfill).toHaveBeenCalledTimes(2)

    offBackfill()
    unsub()
  })

  it('shares ONE stream across subscribers (module singleton)', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const unsubA = eventBus.subscribe('run', vi.fn())
    await tick()
    const unsubB = eventBus.subscribe('pipeline', vi.fn())
    await tick()
    expect(fetch).toHaveBeenCalledTimes(1) // second subscriber did not open a second stream
    unsubA()
    unsubB()
  })
})

describe('dispatchToStore', () => {
  it('routes events to registered store handlers by type', async () => {
    const { dispatchToStore } = await import('../composables/useEventStream')
    const { registerHandler } = await import('../stores/syncRegistry')
    const handler = vi.fn()
    registerHandler('run', handler)

    const event: EventBusEvent = { type: 'run', id: 'r-1', action: 'created', version: 1, org_id: 'org-1', timestamp: '2024-01-01T00:00:00Z' }
    dispatchToStore(event)
    expect(handler).toHaveBeenCalledWith(event)
  })

  it('does not dispatch to wrong type', async () => {
    const { dispatchToStore } = await import('../composables/useEventStream')
    const { registerHandler } = await import('../stores/syncRegistry')
    const handler = vi.fn()
    registerHandler('pipeline', handler)

    dispatchToStore({ type: 'run', id: 'r-1', action: 'deleted', version: 1, org_id: 'org-1' })
    expect(handler).not.toHaveBeenCalled()
  })
})

describe('createSyncAdapter', () => {
  it('calls fetch for updated event even when entity is dirty', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>(['dirty-1'])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'dirty-1', action: 'updated', version: 1, org_id: 'org-1', timestamp: '2024-01-01T00:00:01Z' }
    handleSyncEvent(event)
    expect(fetch).toHaveBeenCalledWith('dirty-1')
    expect(remove).not.toHaveBeenCalled()
  })

  it('calls fetch for non-dirty updated event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>([])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'clean-1', action: 'updated', version: 1, org_id: 'org-1', timestamp: '2024-01-01T00:00:02Z' }
    handleSyncEvent(event)
    expect(fetch).toHaveBeenCalledWith('clean-1')
  })

  it('calls remove for deleted event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>([])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'del-1', action: 'deleted', version: 1, org_id: 'org-1', timestamp: '2024-01-01T00:00:03Z' }
    handleSyncEvent(event)
    expect(remove).toHaveBeenCalledWith('del-1')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('calls fetch for a created event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const handleSyncEvent = createSyncAdapter({ dirtyIds: new Set<string>([]), fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'new-1', action: 'created', version: 1, org_id: 'org-1', timestamp: '2024-01-01T00:00:04Z' }
    handleSyncEvent(event)
    expect(fetch).toHaveBeenCalledWith('new-1')
    expect(remove).not.toHaveBeenCalled()
  })

  it('logs and swallows a rejected fetch so the bus is not poisoned', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fetch = vi.fn().mockRejectedValue(new Error('fetch boom'))
    const remove = vi.fn()
    const handleSyncEvent = createSyncAdapter({ dirtyIds: new Set<string>([]), fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'bad-1', action: 'updated', version: 1, org_id: 'org-1' }
    await expect(Promise.resolve(handleSyncEvent(event))).resolves.toBeUndefined()
    expect(fetch).toHaveBeenCalledWith('bad-1')
    expect(errorSpy).toHaveBeenCalledWith('[SyncAdapter] fetch error', expect.any(Error))
    errorSpy.mockRestore()
  })

  it('logs and swallows a remove error for a deleted event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn(() => {
      throw new Error('remove boom')
    })
    const handleSyncEvent = createSyncAdapter({ dirtyIds: new Set<string>([]), fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'gone-1', action: 'deleted', version: 1, org_id: 'org-1' }
    expect(() => handleSyncEvent(event)).not.toThrow()
    expect(errorSpy).toHaveBeenCalledWith('[SyncAdapter] remove error', expect.any(Error))
    errorSpy.mockRestore()
  })
})

describe('useDirtyTracker', () => {
  it('marks and checks dirty state', async () => {
    const { useDirtyTracker } = await import('../composables/useSyncStore')
    const tracker = useDirtyTracker()
    expect(tracker.isDirty('item-1')).toBe(false)
    tracker.markDirty('item-1')
    expect(tracker.isDirty('item-1')).toBe(true)
    tracker.markClean('item-1')
    expect(tracker.isDirty('item-1')).toBe(false)
  })

  it('tracks dirty state per id independently', async () => {
    const { useDirtyTracker } = await import('../composables/useSyncStore')
    const tracker = useDirtyTracker()
    tracker.markDirty('a')
    expect(tracker.isDirty('a')).toBe(true)
    expect(tracker.isDirty('b')).toBe(false)
    tracker.markClean('a')
    expect(tracker.isDirty('b')).toBe(false)
    tracker.markDirty('b')
    expect(tracker.isDirty('b')).toBe(true)
  })

  it('deduplicates repeated markDirty calls', async () => {
    const { useDirtyTracker } = await import('../composables/useSyncStore')
    const tracker = useDirtyTracker()
    tracker.markDirty('item-1')
    tracker.markDirty('item-1')
    expect(tracker.dirtyIds.value.size).toBe(1)
  })

  it('markClean on a non-dirty id is a no-op', async () => {
    const { useDirtyTracker } = await import('../composables/useSyncStore')
    const tracker = useDirtyTracker()
    tracker.markClean('never-dirty')
    expect(tracker.isDirty('never-dirty')).toBe(false)
    expect(tracker.dirtyIds.value.size).toBe(0)
  })

  it('triggerRef notifies reactive effects on markDirty and markClean', async () => {
    const { effect } = await import('vue')
    const { useDirtyTracker } = await import('../composables/useSyncStore')
    const tracker = useDirtyTracker()
    const spy = vi.fn()
    effect(() => {
      void tracker.dirtyIds.value
      spy()
    })
    expect(spy).toHaveBeenCalledTimes(1)

    tracker.markDirty('item-1')
    expect(spy).toHaveBeenCalledTimes(2)

    tracker.markClean('item-1')
    expect(spy).toHaveBeenCalledTimes(3)
  })
})

describe('reconnect backfill + reset (FAR-250 coverage)', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('resets the backfill debounce when a second reconnect lands inside the window', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const { eventBus } = await import('../composables/useEventStream')
    const backfill = vi.fn()
    const unsub = eventBus.subscribe('run', vi.fn())
    const off = eventBus.onReconnect(backfill)
    await vi.advanceTimersByTimeAsync(0)

    endStream()
    await vi.advanceTimersByTimeAsync(500) // reconnect 1 -> schedules the backfill
    endStream()
    await vi.advanceTimersByTimeAsync(500) // reconnect 2 inside the window -> clears + reschedules
    await vi.advanceTimersByTimeAsync(1500)

    expect(backfill).toHaveBeenCalledTimes(1) // coalesced into a single batch
    off()
    unsub()
  })

  it('logs and swallows an error thrown by a reconnect backfill subscriber', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { eventBus } = await import('../composables/useEventStream')
    const unsub = eventBus.subscribe('run', vi.fn())
    const off = eventBus.onReconnect(() => {
      throw new Error('backfill boom')
    })
    await vi.advanceTimersByTimeAsync(0)

    endStream()
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(1500)

    expect(errorSpy).toHaveBeenCalledWith('[EventBus] Reconnect backfill handler error', expect.any(Error))
    off()
    unsub()
  })

  it('retries when fetch rejects with an AbortError (zombie/dead stream)', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const abortErr = new Error('aborted')
    abortErr.name = 'AbortError'
    vi.mocked(fetch).mockRejectedValue(abortErr)

    const { eventBus } = await import('../composables/useEventStream')
    eventBus.subscribe('run', vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    expect(eventBus.state).toBe('reconnecting')

    await vi.advanceTimersByTimeAsync(500)
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(1)
  })

  it('does not schedule a reconnect when an abort lands after an explicit disconnect', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const abortErr = new Error('aborted')
    abortErr.name = 'AbortError'
    vi.mocked(fetch).mockRejectedValue(abortErr)

    const { eventBus } = await import('../composables/useEventStream')
    const unsub = eventBus.subscribe('run', vi.fn())
    // Last handler leaves before the rejected fetch settles: disconnect() sets
    // _disconnecting, so the AbortError must NOT schedule another attempt.
    unsub()
    await vi.advanceTimersByTimeAsync(0)

    expect(eventBus.state).toBe('idle')
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
  })

  it('does not schedule a reconnect when the stream ends after an explicit disconnect', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const unsub = eventBus.subscribe('run', vi.fn())
    await tick() // stream connected, reader.read() pending
    unsub() // disconnect(): _disconnecting = true, state -> idle
    endStream() // server closes the stream -> the try block completes normally
    await tick()
    await tick()

    expect(eventBus.state).toBe('idle')
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
  })

  it('disconnect() clears a pending backfill timer', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const { eventBus } = await import('../composables/useEventStream')
    const backfill = vi.fn()
    const unsub = eventBus.subscribe('run', vi.fn())
    const off = eventBus.onReconnect(backfill)
    await vi.advanceTimersByTimeAsync(0)

    endStream()
    await vi.advanceTimersByTimeAsync(500) // reconnect -> schedules the backfill timer
    unsub() // last handler leaves -> disconnect() clears the pending timer
    off()
    await vi.advanceTimersByTimeAsync(2000)

    expect(backfill).not.toHaveBeenCalled()
    expect(eventBus.state).toBe('idle')
  })

  it('useEventStream() returns the shared connected/connectionState refs', async () => {
    const { useEventStream } = await import('../composables/useEventStream')
    const result = useEventStream()
    expect(result.connected.value).toBe(false)
    expect(result.connectionState.value).toBe('idle')
  })

  it('resetEventStreamState() clears handlers, the backfill timer, and connection state', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const { eventBus, resetEventStreamState } = await import('../composables/useEventStream')
    const unsub = eventBus.subscribe('run', vi.fn())
    eventBus.onReconnect(vi.fn())
    await vi.advanceTimersByTimeAsync(0)
    endStream()
    await vi.advanceTimersByTimeAsync(500) // reconnect leaves a pending backfill timer

    resetEventStreamState() // clears the pending backfill timer
    expect(eventBus.state).toBe('idle')

    resetEventStreamState() // second call: no pending timer
    unsub()
  })
})
