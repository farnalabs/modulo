import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { MonitorBackendRegistry } from '../monitor/registry'
import type { ErrorEventInput, MonitorBackend } from '../monitor/types'

let errorSpy: ReturnType<typeof vi.spyOn>

afterEach(() => {
  errorSpy?.mockRestore()
})

function createFullBackend(overrides: Partial<MonitorBackend> = {}): MonitorBackend {
  return {
    key: 'test',
    init: vi.fn().mockResolvedValue(true),
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
      registry.captureMessage('test', 'warning')
      expect(a.captureMessage).toHaveBeenCalledTimes(1)
    })

    it('remove stops backend from receiving events', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.remove(a)
      registry.captureMessage('test', 'warning')
      expect(a.captureMessage).not.toHaveBeenCalled()
    })

    it('remove is a no-op for a backend that was never registered', () => {
      const a = createFullBackend()
      const stranger = createFullBackend({ key: 'stranger' })
      registry.add(a)
      registry.remove(stranger)
      registry.captureMessage('test', 'warning')
      expect(a.captureMessage).toHaveBeenCalledTimes(1)
    })

    it('remove is idempotent', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.remove(a)
      registry.remove(a)
      registry.captureMessage('test', 'warning')
      expect(a.captureMessage).not.toHaveBeenCalled()
    })

    it('re-registers a backend after removal', () => {
      const a = createFullBackend()
      registry.add(a)
      registry.remove(a)
      registry.add(a)
      registry.captureMessage('test', 'warning')
      expect(a.captureMessage).toHaveBeenCalledTimes(1)
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
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.captureError({ level: 'error', message: 'test' })

      expect(errorSpy).toHaveBeenCalledWith(
        '[MonitorBackendRegistry] Backend error:',
        expect.objectContaining({ message: 'fail' }),
      )
    })

    it('forwards the error and context to each backend', () => {
      const a = createFullBackend()
      registry.add(a)
      const boom = new Error('boom')
      const context = { source: 'unit-test' }

      registry.captureError({ level: 'error', message: 'test' }, boom, context)

      expect(a.captureError).toHaveBeenCalledWith({ level: 'error', message: 'test' }, boom, context)
    })

    it('isolates a throwing backend from its peers', () => {
      const a = createFullBackend({ captureError: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
      const event = { level: 'error', message: 'test' } satisfies ErrorEventInput
      const boom = new Error('boom')
      const context = { source: 'unit-test' }

      registry.captureError(event, boom, context)

      expect(a.captureError).toHaveBeenCalledTimes(1)
      expect(b.captureError).toHaveBeenCalledWith(event, boom, context)
    })
  })

  describe('captureMessage', () => {
    it('calls captureMessage on each backend', () => {
      const a = createFullBackend()
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)

      registry.captureMessage('hello', 'warning')

      expect(a.captureMessage).toHaveBeenCalledWith('hello', 'warning')
      expect(b.captureMessage).toHaveBeenCalledWith('hello', 'warning')
    })

    it('catches backend errors and logs', () => {
      const a = createFullBackend({ captureMessage: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      registry.add(a)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.captureMessage('hello', 'warning')

      expect(errorSpy).toHaveBeenCalledWith(
        '[MonitorBackendRegistry] Backend error:',
        expect.objectContaining({ message: 'fail' }),
      )
    })

    it('isolates a throwing captureMessage backend from its peers', () => {
      const a = createFullBackend({ captureMessage: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.captureMessage('hello', 'warning')

      expect(b.captureMessage).toHaveBeenCalledWith('hello', 'warning')
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

    it('skips backends that do not implement setUser', () => {
      const partial = { key: 'partial', captureError: vi.fn(), captureMessage: vi.fn(), dispose: vi.fn() }
      const full = createFullBackend()
      registry.add(partial as unknown as MonitorBackend)
      registry.add(full)

      registry.setUser({ id: '1' })

      expect(full.setUser).toHaveBeenCalledWith({ id: '1' })
    })

    it('skips backends that do not implement setTags', () => {
      const partial = { key: 'partial', captureError: vi.fn(), captureMessage: vi.fn(), dispose: vi.fn() }
      const full = createFullBackend()
      registry.add(partial as unknown as MonitorBackend)
      registry.add(full)

      registry.setTags({ env: 'test' })

      expect(full.setTags).toHaveBeenCalledWith({ env: 'test' })
    })

    it('isolates a throwing setUser backend from its peers', () => {
      const a = createFullBackend({ setUser: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.setUser({ id: '1' })

      expect(errorSpy).toHaveBeenCalledWith(
        '[MonitorBackendRegistry] Backend error:',
        expect.objectContaining({ message: 'fail' }),
      )
      expect(b.setUser).toHaveBeenCalledWith({ id: '1' })
    })

    it('isolates a throwing setTags backend from its peers', () => {
      const a = createFullBackend({ setTags: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.setTags({ env: 'test' })

      expect(errorSpy).toHaveBeenCalledWith(
        '[MonitorBackendRegistry] Backend error:',
        expect.objectContaining({ message: 'fail' }),
      )
      expect(b.setTags).toHaveBeenCalledWith({ env: 'test' })
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

    it('does not dispatch to backends after disposeAll', () => {
      const captureMessage = vi.fn()
      const a = createFullBackend({ captureMessage })
      registry.add(a)
      registry.disposeAll()

      captureMessage.mockClear()
      registry.captureMessage('after', 'warning')
      expect(captureMessage).not.toHaveBeenCalled()
    })

    it('isolates a throwing dispose from its peers and still clears state', () => {
      const a = createFullBackend({ dispose: vi.fn().mockImplementation(() => { throw new Error('fail') }) })
      const b = createFullBackend()
      registry.add(a)
      registry.add(b)
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      registry.disposeAll()

      expect(errorSpy).toHaveBeenCalledWith(
        '[MonitorBackendRegistry] Backend error:',
        expect.objectContaining({ message: 'fail' }),
      )
      expect(b.dispose).toHaveBeenCalledTimes(1)
      registry.captureMessage('after', 'warning')
      expect(b.captureMessage).not.toHaveBeenCalled()
    })

    it('is idempotent across repeated calls', () => {
      const dispose = vi.fn()
      const a = createFullBackend({ dispose })
      registry.add(a)
      registry.disposeAll()
      dispose.mockClear()

      expect(() => registry.disposeAll()).not.toThrow()
      expect(dispose).not.toHaveBeenCalled()
    })
  })
})
