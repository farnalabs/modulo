import { describe, it, expect, vi, beforeEach } from 'vitest'
import { registerHandler, getHandlers } from '../stores/syncRegistry'
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
    getHandlers('run').clear()
    getHandlers('pipeline').clear()
    getHandlers('schema').clear()
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
})
