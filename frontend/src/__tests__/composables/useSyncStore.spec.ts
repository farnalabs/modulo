import { afterEach, describe, expect, it, vi } from 'vitest'
import { effect } from 'vue'
import { createSyncAdapter, useDirtyTracker } from '../../composables/useSyncStore'
import type { EventBusEvent } from '../../types/events'

const event = (action: EventBusEvent['action'], id: string): EventBusEvent => ({
  type: 'pipeline',
  id,
  action,
  version: 1,
  org_id: 'org-1',
})

describe('createSyncAdapter', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('fetches created and updated records and handles fetch failures', async () => {
    const fetch = vi.fn().mockRejectedValue(new Error('fetch failed'))
    const remove = vi.fn()
    const handle = createSyncAdapter({ dirtyIds: new Set(), fetch, remove })
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    handle(event('created', 'created-1'))
    handle(event('updated', 'updated-1'))
    await Promise.resolve()

    expect(fetch).toHaveBeenNthCalledWith(1, 'created-1')
    expect(fetch).toHaveBeenNthCalledWith(2, 'updated-1')
    expect(remove).not.toHaveBeenCalled()
    expect(error).toHaveBeenCalledWith('[SyncAdapter] fetch error', expect.any(Error))
  })

  it('removes deleted records and logs removal failures', () => {
    const remove = vi.fn(() => { throw new Error('remove failed') })
    const fetch = vi.fn()
    const handle = createSyncAdapter({ dirtyIds: new Set(), fetch, remove })
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})

    expect(() => handle(event('deleted', 'deleted-1'))).not.toThrow()
    expect(remove).toHaveBeenCalledWith('deleted-1')
    expect(fetch).not.toHaveBeenCalled()
    expect(error).toHaveBeenCalledWith('[SyncAdapter] remove error', expect.any(Error))
  })
})

describe('useDirtyTracker', () => {
  it('tracks ids independently and notifies effects for both mutations', () => {
    const tracker = useDirtyTracker()
    const runs = vi.fn()
    effect(() => {
      const size = tracker.dirtyIds.value.size
      runs(size)
    })

    tracker.markDirty('one')
    tracker.markDirty('two')
    tracker.markClean('one')
    tracker.markClean('missing')

    expect(tracker.isDirty('one')).toBe(false)
    expect(tracker.isDirty('two')).toBe(true)
    expect(tracker.dirtyIds.value.size).toBe(1)
    expect(runs.mock.calls.length).toBeGreaterThan(1)
  })

  it('does not expose a dirty id after it is marked clean', () => {
    const tracker = useDirtyTracker()
    tracker.markDirty('draft-1')
    expect(tracker.isDirty('draft-1')).toBe(true)

    tracker.markClean('draft-1')

    expect(tracker.isDirty('draft-1')).toBe(false)
    expect(tracker.dirtyIds.value.has('draft-1')).toBe(false)
  })
})
