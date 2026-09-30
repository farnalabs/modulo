import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// main.ts runs `main().catch(...)` at module load. This spec proves the catch
// actually surfaces a bootstrap failure (rather than leaving an unhandled
// rejection), using the documented failure point: router.isReady() rejecting.
describe('main bootstrap failure handling', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.doMock('../router', () => ({
      default: {
        install: vi.fn(),
        afterEach: vi.fn(),
        isReady: () => Promise.reject(new Error('router not ready')),
      },
    }))
  })

  afterEach(() => {
    vi.doUnmock('../router')
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('logs a fatal error when the application bootstrap rejects', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    await import('../main')

    await vi.waitFor(() => {
      expect(consoleError).toHaveBeenCalledWith(
        'Fatal: application bootstrap failed',
        expect.any(Error),
      )
    })
  })
})
