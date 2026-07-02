import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { EventBusEvent } from '../types/events'

vi.mock('@/lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'test-token'),
}))

let pushEvent: (eventType: string, data: Record<string, unknown>) => void
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

  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: true,
    body: { getReader: () => mockReader },
  })

  pushEvent = (eventType: string, data: Record<string, unknown>) => {
    const sse = `event: ${eventType}\ndata: ${JSON.stringify(data)}\n\n`
    const encoded = encoder.encode(sse)
    if (resolveNext) {
      const resolve = resolveNext
      resolveNext = null
      resolve({ done: false, value: encoded })
    } else {
      queue.push({ done: false, value: encoded })
    }
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
  return new Promise<void>((resolve) => setTimeout(resolve, 10))
}

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
    await tick()
    expect(handler).toHaveBeenCalledWith(event)
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

  it('connected becomes true on open', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
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
  })
})

describe('dispatchToStore', () => {
  it('routes events to registered store handlers by type', async () => {
    const { dispatchToStore } = await import('../composables/useEventStream')
    const { registerHandler } = await import('../stores/syncRegistry')
    const handler = vi.fn()
    registerHandler('run', handler)

    const event: EventBusEvent = { type: 'run', id: 'r-1', action: 'created', version: 1, org_id: 'org-1' }
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
  it('skips event when entity is dirty', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn()
    const remove = vi.fn()
    const dirtyIds = new Set<string>(['dirty-1'])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'dirty-1', action: 'updated', version: 1, org_id: 'org-1' }
    handleSyncEvent(event)
    expect(fetch).not.toHaveBeenCalled()
    expect(remove).not.toHaveBeenCalled()
  })

  it('calls fetch for non-dirty updated event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>([])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'clean-1', action: 'updated', version: 1, org_id: 'org-1' }
    handleSyncEvent(event)
    expect(fetch).toHaveBeenCalledWith('clean-1')
  })

  it('calls remove for deleted event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>([])
    const handleSyncEvent = createSyncAdapter({ dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'del-1', action: 'deleted', version: 1, org_id: 'org-1' }
    handleSyncEvent(event)
    expect(remove).toHaveBeenCalledWith('del-1')
    expect(fetch).not.toHaveBeenCalled()
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
})
