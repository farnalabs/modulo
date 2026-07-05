import { describe, it, expect, beforeEach, vi } from 'vitest'
import { MonitorBackendRegistry } from '../monitor/registry'
import type { MonitorBackend } from '../monitor/types'

function createFullBackend(overrides: Partial<MonitorBackend> = {}): MonitorBackend {
  return {
    name: 'test',
    captureError: vi.fn(),
    captureMessage: vi.fn(),
    setUser: vi.fn(),
    setTags: vi.fn(),
    dispose: vi.fn(),
    ...overrides,
  }
}

describe('MonitorBackendRegistry', () => {
  let registry: MonitorBackendRegistry

  beforeEach(() => {
    registry = new MonitorBackendRegistry()
  })

  describe('add and remove', () => {
    it('can add backends', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.captureMessage('test', 'info')
      expect(a.captureMessage).toHaveBeenCalledTimes(1)
    })

    it('remove stops backend from receiving events', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.remove(a)
      registry.captureMessage('test', 'info')
      expect(a.captureMessage).not.toHaveBeenCalled()
    })
  })

  describe('captureError', () => {
    it('calls captureError on each backend', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.captureError({ level: 'error', message: 'test' })

      expect(a.captureError).toHaveBeenCalledWith({ level: 'error', message: 'test' }, undefined, undefined)
      expect(b.captureError).toHaveBeenCalledWith({ level: 'error', message: 'test' }, undefined, undefined)
    })

    it('catches backend errors and logs a warning', () => {
      const a = createFullBackend({ captureError: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      registry.add(a)
      const warnSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.captureError({ level: 'error', message: 'test' })

      expect(warnSpy).toHaveBeenCalled()
      warnSpy.mockRestore()
    })
  })

  describe('captureMessage', () => {
    it('calls captureMessage on each backend', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.captureMessage('hello', 'info')

      expect(a.captureMessage).toHaveBeenCalledWith('hello', 'info')
      expect(b.captureMessage).toHaveBeenCalledWith('hello', 'info')
    })

    it('catches backend errors and logs', () => {
      const a = createFullBackend({ captureMessage: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      registry.add(a)
      const warnSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.captureMessage('hello', 'info')

      expect(warnSpy).toHaveBeenCalled()
      warnSpy.mockRestore()
    })
  })

  describe('setUser and setTags', () => {
    it('setUser dispatches to all backends', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.setUser({ id: '1', email: 'test@test.com' })

      expect(a.setUser).toHaveBeenCalledWith({ id: '1', email: 'test@test.com' })
      expect(b.setUser).toHaveBeenCalledWith({ id: '1', email: 'test@test.com' })
    })

    it('setUser with null dispatches correctly', () => {
      const a = createFullBackend()
      registry.add(a)

      registry.setUser(null)

      expect(a.setUser).toHaveBeenCalledWith(null)
    })

    it('setTags dispatches to all backends', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.setTags({ environment: 'test', version: '1.0' })

      expect(a.setTags).toHaveBeenCalledWith({ environment: 'test', version: '1.0' })
      expect(b.setTags).toHaveBeenCalledWith({ environment: 'test', version: '1.0' })
    })
  })

  describe('disposeAll', () => {
    it('calls dispose on each backend and clears state', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.disposeAll()

      expect(a.dispose).toHaveBeenCalledTimes(1)
      expect(b.dispose).toHaveBeenCalledTimes(1)
    })

    it('handles backends without dispose method', () => {
      const a = createFullBackend({ dispose: undefined })
      const b = createFullBackend({ dispose: undefined })
      registry.add(a)
      registry.add(b)

      expect(() => registry.disposeAll()).not.toThrow()
    })

    it('does not dispatch to backends after disposeAll', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.disposeAll()

      a.captureMessage.mockClear()
      registry.captureMessage('after', 'info')
      expect(a.captureMessage).not.toHaveBeenCalled()
    })
  })
})
