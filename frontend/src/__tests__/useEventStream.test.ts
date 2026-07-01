import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { EventBusEvent } from '../types/events'

const mockInstances: any[] = []

beforeEach(() => {
  mockInstances.splice(0)
  vi.resetModules()
  localStorage.setItem('modulo_access_token', 'test-token')
  ;(globalThis as any).EventSource = vi.fn().mockImplementation((url: string) => {
    const instance = {
      url,
      onopen: null,
      onmessage: null,
      onerror: null,
      close: vi.fn(),
    }
    mockInstances.push(instance)
    return instance
  })
})

function triggerEvent(data: Record<string, unknown>) {
  const instance = mockInstances[0]
  if (instance?.onmessage) {
    instance.onmessage({ data: JSON.stringify(data) })
  }
}

describe('eventBus', () => {
  it('subscribe adds handler and connects EventSource', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    const unsub = eventBus.subscribe('run', handler)
    expect(mockInstances.length).toBe(1)
    expect(mockInstances[0].url).toContain('/api/v1/events')
    expect(mockInstances[0].url).toContain('token=test-token')
    unsub()
  })

  it('EventSource message triggers handler', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    const event = { type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' }
    triggerEvent(event)
    expect(handler).toHaveBeenCalledWith(event)
  })

  it('unsubscribe removes handler', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    const unsub = eventBus.subscribe('run', handler)
    unsub()
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    expect(handler).not.toHaveBeenCalled()
  })

  it('handler is not called for different resource type', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('pipeline', handler)
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    expect(handler).not.toHaveBeenCalled()
  })

  it('connected ref is reactive', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    expect(eventBus.connected.value).toBe(false)
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    triggerEvent({ type: 'run', id: 'r-1', action: 'updated', version: 2, org_id: 'org-1' })
    expect(eventBus.connected.value).toBe(false)
  })

  it('connected becomes true on open', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    mockInstances[0].onopen?.()
    expect(eventBus.connected.value).toBe(true)
  })

  it('connected becomes false on error', async () => {
    const { eventBus } = await import('../composables/useEventStream')
    const handler = vi.fn()
    eventBus.subscribe('run', handler)
    mockInstances[0].onopen?.()
    expect(eventBus.connected.value).toBe(true)
    mockInstances[0].onerror?.()
    expect(eventBus.connected.value).toBe(false)
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
    const handleSyncEvent = createSyncAdapter('run', { dirtyIds, fetch, remove })

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
    const handleSyncEvent = createSyncAdapter('run', { dirtyIds, fetch, remove })

    const event: EventBusEvent = { type: 'run', id: 'clean-1', action: 'updated', version: 1, org_id: 'org-1' }
    handleSyncEvent(event)
    expect(fetch).toHaveBeenCalledWith('clean-1')
  })

  it('calls remove for deleted event', async () => {
    const { createSyncAdapter } = await import('../composables/useSyncStore')
    const fetch = vi.fn().mockResolvedValue(undefined)
    const remove = vi.fn()
    const dirtyIds = new Set<string>([])
    const handleSyncEvent = createSyncAdapter('run', { dirtyIds, fetch, remove })

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
