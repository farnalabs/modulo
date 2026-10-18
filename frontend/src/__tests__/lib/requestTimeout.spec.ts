import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  withTimeout,
  RequestTimeoutError,
  DEFAULT_REQUEST_TIMEOUT_MS,
} from '../../lib/requestTimeout'

// FAR-1818 — dedicated coverage for the shared request-deadline helper
// (`lib/requestTimeout.ts`). It is now shared infrastructure (the pipeline host
// and the version-compare component both depend on it) and was previously only
// covered indirectly through those call sites.
//
// Only `setTimeout` / `clearTimeout` are faked — promises and microtasks stay
// real, so there are no computed wall-clock sleeps and no timing flake.

afterEach(() => {
  vi.useRealTimers()
})

function useDeadlineTimers(): void {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
}

describe('withTimeout', () => {
  it('resolves with the factory value when it settles before the deadline', async () => {
    useDeadlineTimers()
    const result = await withTimeout(() => Promise.resolve('value'), 1000)
    expect(result).toBe('value')
  })

  it('rejects with RequestTimeoutError once the deadline elapses', async () => {
    useDeadlineTimers()
    const pending = withTimeout(() => new Promise<string>(() => {}), 1000)
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })

  it('aborts the signal it passes to the factory when the deadline elapses', async () => {
    useDeadlineTimers()
    let captured: AbortSignal | undefined
    const pending = withTimeout((signal) => {
      captured = signal
      return new Promise<string>(() => {})
    }, 1000)
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    expect(captured).toBeDefined()
    expect(captured?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
    expect(captured?.aborted).toBe(true)
  })

  it('defaults the deadline to DEFAULT_REQUEST_TIMEOUT_MS', async () => {
    useDeadlineTimers()
    let captured: AbortSignal | undefined
    const pending = withTimeout((signal) => {
      captured = signal
      return new Promise<string>(() => {})
    })
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    // Not yet: the default window has not fully elapsed.
    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS - 1)
    expect(captured?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await assertion
    expect(captured?.aborted).toBe(true)
  })

  it('clears the deadline timer on the success path (no lingering timer)', async () => {
    useDeadlineTimers()
    const result = await withTimeout(() => Promise.resolve('ok'), 1000)
    expect(result).toBe('ok')
    expect(vi.getTimerCount()).toBe(0)
  })

  it('clears the deadline timer on the timeout path', async () => {
    useDeadlineTimers()
    const pending = withTimeout(() => new Promise<string>(() => {}), 1000)
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
    expect(vi.getTimerCount()).toBe(0)
  })

  it('propagates a factory rejection unchanged', async () => {
    useDeadlineTimers()
    const boom = new Error('factory rejected')
    await expect(withTimeout(() => Promise.reject(boom), 1000)).rejects.toBe(boom)
  })

  it('propagates a SYNCHRONOUS factory throw and still clears the timer', async () => {
    useDeadlineTimers()
    const boom = new Error('factory threw synchronously')
    await expect(
      withTimeout<string>(() => {
        throw boom
      }, 1000),
    ).rejects.toBe(boom)
    // The setup was wrapped, so no deadline timer survives the synchronous
    // failure. Without the fix this is 1 (the timer outlives the attempt).
    expect(vi.getTimerCount()).toBe(0)
  })

  it('forwards an already-aborted external signal to the factory', async () => {
    useDeadlineTimers()
    const external = new AbortController()
    external.abort()
    let captured: AbortSignal | undefined
    const pending = withTimeout(
      (signal) => {
        captured = signal
        return new Promise<string>(() => {})
      },
      1000,
      external.signal,
    )
    // The controller is aborted synchronously before the factory runs.
    expect(captured?.aborted).toBe(true)
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestTimeoutError)
    await vi.advanceTimersByTimeAsync(1000)
    await assertion
  })
})
