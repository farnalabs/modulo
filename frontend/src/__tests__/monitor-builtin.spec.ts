import { describe, it, expect, vi, beforeEach } from 'vitest'
import { BuiltinMonitorBackend } from '../monitor/backends/builtin'

vi.mock('../lib/error-tracking/transport', () => ({
  enqueueError: vi.fn(),
  disposeTransport: vi.fn(),
}))

import { enqueueError, disposeTransport } from '../lib/error-tracking/transport'

const mockEnqueueError = vi.mocked(enqueueError)
const mockDisposeTransport = vi.mocked(disposeTransport)

describe('BuiltinMonitorBackend', () => {
  let backend: BuiltinMonitorBackend

  beforeEach(() => {
    backend = new BuiltinMonitorBackend()
    mockEnqueueError.mockReset()
    mockDisposeTransport.mockReset()
  })

  it('has key "builtin"', () => {
    expect(backend.key).toBe('builtin')
  })

  it('init returns true', async () => {
    await expect(backend.init({ monitorBackends: [] })).resolves.toBe(true)
  })

  describe('captureError', () => {
    it('calls enqueueError with source=frontend', () => {
      backend.captureError({ level: 'error', message: 'test error' })

      expect(mockEnqueueError).toHaveBeenCalledTimes(1)
      expect(mockEnqueueError).toHaveBeenCalledWith({
        level: 'error',
        message: 'test error',
        source: 'frontend',
      })
    })

    it('preserves extra event fields', () => {
      backend.captureError({
        level: 'critical',
        message: 'critical error',
        stacktrace: 'Error: critical error\n  at Foo.bar (baz.js:1:2)',
        environment: 'production',
      })

      expect(mockEnqueueError).toHaveBeenCalledWith(expect.objectContaining({
        level: 'critical',
        message: 'critical error',
        stacktrace: 'Error: critical error\n  at Foo.bar (baz.js:1:2)',
        environment: 'production',
      }))
    })
  })

  describe('captureMessage', () => {
    it('calls enqueueError with source=frontend and given level', () => {
      backend.captureMessage('warning message', 'warning')

      expect(mockEnqueueError).toHaveBeenCalledTimes(1)
      expect(mockEnqueueError).toHaveBeenCalledWith({
        level: 'warning',
        message: 'warning message',
        source: 'frontend',
      })
    })

    it('handles error-level messages', () => {
      backend.captureMessage('error message', 'error')

      expect(mockEnqueueError).toHaveBeenCalledWith(expect.objectContaining({
        level: 'error',
        message: 'error message',
      }))
    })
  })

  describe('dispose', () => {
    it('calls disposeTransport', () => {
      backend.dispose()

      expect(mockDisposeTransport).toHaveBeenCalledTimes(1)
    })
  })
})
