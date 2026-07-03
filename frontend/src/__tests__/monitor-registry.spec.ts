import { describe, it, expect, beforeEach, vi } from 'vitest'
import { MonitorBackendRegistry } from '../monitor/registry'
import type { MonitorBackend } from '../monitor/types'

function createMockBackend(key: string): MonitorBackend {
  return {
    key,
    init: vi.fn().mockReturnValue(true),
  }
}

describe('MonitorBackendRegistry', () => {
  let registry: MonitorBackendRegistry

  beforeEach(() => {
    registry = new MonitorBackendRegistry()
  })

  describe('add and getBackends', () => {
    it('can add backends', () => {
      registry.add(createMockBackend('test'))
      expect(registry.getBackends()).toHaveLength(1)
    })

    it('getBackends returns all added backends', () => {
      registry.add(createMockBackend('a'))
      registry.add(createMockBackend('b'))
      expect(registry.getBackends()).toHaveLength(2)
    })
  })

  describe('dispatch', () => {
    it('calls the right method on each backend', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.setUser = vi.fn()
      b.setUser = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.dispatch('setUser', { id: '1' })

      expect(a.setUser).toHaveBeenCalledWith({ id: '1' })
      expect(b.setUser).toHaveBeenCalledWith({ id: '1' })
    })

    it('does nothing when method does not exist on backend', () => {
      const backend = createMockBackend('a')
      registry.add(backend)

      expect(() => registry.dispatch('nonexistent', 'arg')).not.toThrow()
    })

    it('catches backend errors and logs a warning', () => {
      const backend = createMockBackend('a')
      backend.setUser = vi.fn().mockImplementation(() => { throw new Error('fail') })
      registry.add(backend)
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

      registry.dispatch('setUser', { id: '1' })

      expect(warn).toHaveBeenCalled()
      warn.mockRestore()
    })
  })

  describe('dispatchToImplementors', () => {
    it('excludes backends that have the excludeWith method', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.captureError = vi.fn()
      b.captureError = vi.fn()
      b.captureRawError = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.dispatchToImplementors('captureError', 'captureRawError', { level: 'error', message: 'test' })

      expect(a.captureError).toHaveBeenCalledTimes(1)
      expect(b.captureError).not.toHaveBeenCalled()
    })

    it('calls method on all backends when excludeWith is undefined', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.captureRawError = vi.fn()
      b.captureRawError = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.dispatchToImplementors('captureRawError', undefined, new Error('test'))

      expect(a.captureRawError).toHaveBeenCalledTimes(1)
      expect(b.captureRawError).toHaveBeenCalledTimes(1)
    })
  })

  describe('captureError', () => {
    it('dispatches captureRawError on backends that have it when rawError is provided', () => {
      const backend = createMockBackend('raw')
      backend.captureRawError = vi.fn()
      registry.add(backend)

      registry.captureError({ level: 'error', message: 'test' }, new Error('raw'))

      expect(backend.captureRawError).toHaveBeenCalledWith(expect.any(Error), undefined)
    })

    it('dispatches to captureRawError backends AND captureError-only backends (not both on same)', () => {
      const rawOnly = createMockBackend('raw')
      rawOnly.captureRawError = vi.fn()

      const errOnly = createMockBackend('err')
      errOnly.captureError = vi.fn()

      const both = createMockBackend('both')
      both.captureError = vi.fn()
      both.captureRawError = vi.fn()

      registry.add(rawOnly)
      registry.add(errOnly)
      registry.add(both)

      registry.captureError({ level: 'error', message: 'test' }, new Error('raw'))

      expect(rawOnly.captureRawError).toHaveBeenCalledTimes(1)
      expect(errOnly.captureError).toHaveBeenCalledTimes(1)
      expect(both.captureRawError).toHaveBeenCalledTimes(1)
      expect(both.captureError).not.toHaveBeenCalled()
    })

    it('dispatches only captureError when no rawError is given', () => {
      const backend = createMockBackend('a')
      backend.captureError = vi.fn()
      registry.add(backend)

      registry.captureError({ level: 'error', message: 'test' })

      expect(backend.captureError).toHaveBeenCalledWith({ level: 'error', message: 'test' })
    })
  })

  describe('captureMessage rate-limiting', () => {
    it('rate-limits the same message key within 60s', () => {
      const backend = createMockBackend('a')
      backend.captureMessage = vi.fn()
      registry.add(backend)

      registry.captureMessage('test message', 'error')
      expect(backend.captureMessage).toHaveBeenCalledTimes(1)

      registry.captureMessage('test message', 'error')
      expect(backend.captureMessage).toHaveBeenCalledTimes(1)
    })

    it('allows different message keys independently', () => {
      const backend = createMockBackend('a')
      backend.captureMessage = vi.fn()
      registry.add(backend)

      registry.captureMessage('msg-a', 'error')
      registry.captureMessage('msg-b', 'error')
      expect(backend.captureMessage).toHaveBeenCalledTimes(2)
    })

    it('blocks after 100 messages globally per minute', () => {
      const backend = createMockBackend('a')
      backend.captureMessage = vi.fn()
      registry.add(backend)

      for (let i = 0; i < 100; i++) {
        registry.captureMessage(`msg-${i}`, 'warning')
      }
      expect(backend.captureMessage).toHaveBeenCalledTimes(100)

      registry.captureMessage('msg-overflow', 'warning')
      expect(backend.captureMessage).toHaveBeenCalledTimes(100)
    })
  })

  describe('setUser and setTags', () => {
    it('setUser dispatches to all backends', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.setUser = vi.fn()
      b.setUser = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.setUser({ id: '1', email: 'test@test.com' })

      expect(a.setUser).toHaveBeenCalledWith({ id: '1', email: 'test@test.com' })
      expect(b.setUser).toHaveBeenCalledWith({ id: '1', email: 'test@test.com' })
    })

    it('setUser with null dispatches correctly', () => {
      const backend = createMockBackend('a')
      backend.setUser = vi.fn()
      registry.add(backend)

      registry.setUser(null)

      expect(backend.setUser).toHaveBeenCalledWith(null)
    })

    it('setTags dispatches to all backends', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.setTags = vi.fn()
      b.setTags = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.setTags({ environment: 'test', version: '1.0' })

      expect(a.setTags).toHaveBeenCalledWith({ environment: 'test', version: '1.0' })
      expect(b.setTags).toHaveBeenCalledWith({ environment: 'test', version: '1.0' })
    })
  })

  describe('disposeAll', () => {
    it('calls dispose on each backend and clears state', () => {
      const a = createMockBackend('a')
      const b = createMockBackend('b')
      a.dispose = vi.fn()
      b.dispose = vi.fn()
      registry.add(a)
      registry.add(b)

      registry.captureMessage('test', 'error')
      expect(registry.getBackends()).toHaveLength(2)

      registry.disposeAll()

      expect(a.dispose).toHaveBeenCalledTimes(1)
      expect(b.dispose).toHaveBeenCalledTimes(1)
      expect(registry.getBackends()).toHaveLength(0)
    })

    it('handles backends without dispose method', () => {
      registry.add(createMockBackend('a'))
      registry.add(createMockBackend('b'))

      expect(() => registry.disposeAll()).not.toThrow()
      expect(registry.getBackends()).toHaveLength(0)
    })
  })
})
