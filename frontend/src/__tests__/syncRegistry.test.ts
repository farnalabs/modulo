import { describe, it, expect, vi, beforeEach } from 'vitest'
import { registerHandler, getHandlers, clearAllRegistrations, registerSyncHandlers, disposeSyncHandlers } from '../stores/syncRegistry'
import { ref } from 'vue'
import type { EventBusEvent } from '../types/events'

function makeEvent(overrides: Partial<EventBusEvent> = {}): EventBusEvent {
  return {
    type: 'run',
    id: 'test-id',
    action: 'updated',
    version: 1,
    org_id: 'org-1',
    ...overrides,
  }
}

describe('syncRegistry', () => {
  beforeEach(() => {
    clearAllRegistrations()
  })

  it('registers and dispatches to a handler', () => {
    const handler = vi.fn()
    registerHandler('run', handler)
    const handlers = getHandlers('run')
    expect(handlers.size).toBe(1)

    const event = makeEvent()
    for (const h of handlers) h(event)
    expect(handler).toHaveBeenCalledWith(event)
  })

  it('unregister removes handler', () => {
    const handler = vi.fn()
    const unsub = registerHandler('run', handler)
    unsub()
    expect(getHandlers('run').size).toBe(0)
  })

  it('dispatches to multiple handlers', () => {
    const handler1 = vi.fn()
    const handler2 = vi.fn()
    registerHandler('run', handler1)
    registerHandler('run', handler2)
    const event = makeEvent()
    for (const h of getHandlers('run')) h(event)
    expect(handler1).toHaveBeenCalledWith(event)
    expect(handler2).toHaveBeenCalledWith(event)
  })

  it('does not dispatch to wrong resource type', () => {
    const handler = vi.fn()
    registerHandler('pipeline', handler)
    const event = makeEvent()
    for (const h of getHandlers('run')) h(event)
    expect(handler).not.toHaveBeenCalled()
  })

  it('deduplicates identical handler registrations', () => {
    const handler = vi.fn()
    registerHandler('run', handler)
    registerHandler('run', handler)
    expect(getHandlers('run').size).toBe(1)
  })

  it('returns a defensive copy from getHandlers', () => {
    const handler = vi.fn()
    registerHandler('run', handler)
    const copy = getHandlers('run')
    copy.delete(handler)
    expect(getHandlers('run').size).toBe(1)
  })

  it('returns an empty set for an unregistered resource type', () => {
    expect(getHandlers('missing')).toEqual(new Set())
  })

  it('registerSyncHandlers and dispose handle an empty registration set', () => {
    const unsubHandlers: Array<() => void> = []
    const syncingIds = ref(new Set(['run-1']))

    registerSyncHandlers(unsubHandlers, syncingIds, [], vi.fn())
    disposeSyncHandlers(unsubHandlers, syncingIds)

    expect(unsubHandlers).toHaveLength(0)
    expect(syncingIds.value.size).toBe(0)
  })

  it('registerSyncHandlers wires every resource and dispose clears state', () => {
    const handler = vi.fn()
    const unsubHandlers: Array<() => void> = []
    const syncingIds = ref(new Set(['run-1']))

    registerSyncHandlers(unsubHandlers, syncingIds, ['run', 'pipeline'], handler)
    expect(unsubHandlers).toHaveLength(2)
    expect(getHandlers('run').size).toBe(1)
    expect(getHandlers('pipeline').size).toBe(1)

    disposeSyncHandlers(unsubHandlers, syncingIds)

    expect(unsubHandlers).toHaveLength(0)
    expect(syncingIds.value.size).toBe(0)
    expect(getHandlers('run').size).toBe(0)
    expect(getHandlers('pipeline').size).toBe(0)
  })

  it('disposeSyncHandlers is safe when registrations were already removed', () => {
    const unsubHandlers: Array<() => void> = [vi.fn()]
    const syncingIds = ref(new Set(['run-1']))

    disposeSyncHandlers(unsubHandlers, syncingIds)

    expect(unsubHandlers).toHaveLength(0)
    expect(syncingIds.value.size).toBe(0)
  })

  it('allows re-registration after unregister', () => {
    const handler = vi.fn()
    const unsub = registerHandler('run', handler)
    unsub()
    expect(getHandlers('run').size).toBe(0)
    registerHandler('run', handler)
    const event = makeEvent()
    for (const h of getHandlers('run')) h(event)
    expect(handler).toHaveBeenCalledWith(event)
  })
})
