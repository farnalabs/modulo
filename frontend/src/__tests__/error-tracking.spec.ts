import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { createApp, h } from 'vue'
import { createErrorTracker, getErrorTracker } from '../lib/error-tracking'
import { BuiltinMonitorBackend } from '../monitor/backends/builtin'
import { BreadcrumbCollector } from '../lib/error-tracking/breadcrumbs'
import { usePlanStore } from '../stores/planStore'

const mockFetch = vi.fn()

function expectRecord(value: unknown): asserts value is Record<string, unknown> {
  expect(value).toBeDefined()
  expect(typeof value).toBe('object')
  expect(value).not.toBeNull()
}

function mockSessionKey(key = 'test-session-key') {
  mockFetch.mockImplementation((url: string) => {
    if (url.includes('/api/v1/errors/session-key')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ key: key }),
      })
    }
    if (url.includes('/api/v1/errors/ingest')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ results: [] }),
      })
    }
    return Promise.reject(new Error(`Unexpected URL: ${url}`))
  })
}

beforeEach(() => {
  mockFetch.mockReset()
  mockSessionKey()
  window.fetch = mockFetch as unknown as typeof fetch
  setActivePinia(createPinia())
})

afterEach(() => {
  const tracker = getErrorTracker()
  if (tracker) tracker.dispose()
  ;(window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__ = false
  vi.restoreAllMocks()
  mockSessionKey()
  vi.useRealTimers()
})

describe('ErrorTracker', () => {
  describe('singleton', () => {
    it('createErrorTracker returns the same instance', () => {
      const t1 = createErrorTracker()
      const t2 = createErrorTracker()
      expect(t1).toBe(t2)
    })

    it('getErrorTracker returns the instance after creation', () => {
      expect(getErrorTracker()).toBeNull()
      const t1 = createErrorTracker()
      expect(getErrorTracker()).toBe(t1)
    })
  })

  describe('captureError', () => {
    it('captures an error with context', () => {
      const tracker = createErrorTracker()
      const error = new Error('something broke')
      expect(() => tracker.captureError(error, { component: 'test' })).not.toThrow()
    })

    it('captures an error without context', () => {
      const tracker = createErrorTracker()
      expect(() => tracker.captureError(new Error('bare error'))).not.toThrow()
    })
  })

  describe('captureMessage', () => {
    it('captures a message at default level', () => {
      const tracker = createErrorTracker()
      expect(() => tracker.captureMessage('info message')).not.toThrow()
    })

    it('captures a message at warning level', () => {
      const tracker = createErrorTracker()
      expect(() => tracker.captureMessage('warning message', 'warning')).not.toThrow()
    })

    it('captures a message at critical level', () => {
      const tracker = createErrorTracker()
      expect(() => tracker.captureMessage('critical message', 'critical')).not.toThrow()
    })
  })

  describe('window error handlers', () => {
    it('captures errors via window.onerror', async () => {
      window.fetch = mockFetch as unknown as typeof fetch
      const tracker = createErrorTracker({ monitorBackends: [new BuiltinMonitorBackend()] })

      for (let i = 0; i < 9; i++) {
        tracker.captureMessage(`fill ${i}`)
      }
      window.dispatchEvent(new ErrorEvent('error', {
        message: 'runtime error',
        filename: 'app.js',
        lineno: 42,
        colno: 10,
        error: new Error('runtime error'),
      }))

      await vi.waitFor(() => {
        const ingestCalls = mockFetch.mock.calls.filter(
          (c: unknown[]) => typeof c[0] === 'string' && (c[0] as string).includes('/ingest'),
        )
        expect(ingestCalls.length).toBeGreaterThanOrEqual(1)
      }, { timeout: 5000 })
    })

    it('captures errors via window.onunhandledrejection', (ctx) => {
      if (typeof PromiseRejectionEvent === 'undefined') {
        // jsdom does not implement PromiseRejectionEvent
        ctx.skip()
      } else {
        window.fetch = mockFetch as unknown as typeof fetch
        const tracker = createErrorTracker()
        const captureError = vi.spyOn(tracker, 'captureError')
        window.dispatchEvent(new PromiseRejectionEvent('unhandledrejection', {
          reason: new Error('promise rejected'),
          promise: Promise.resolve(),
        }))

        expect(captureError).toHaveBeenCalledWith(
          expect.objectContaining({ message: 'promise rejected' }),
          { type: 'unhandled_promise_rejection' },
        )
      }
    })
  })

  describe('Vue plugin', () => {
    it('registers error and warn handlers on the app', () => {
      const tracker = createErrorTracker()
      const app = createApp({
        render() { return h('div', 'test') },
      })

      expect(app.config.errorHandler).toBeUndefined()
      app.use(tracker.vuePlugin)
      expect(app.config.errorHandler).toBeDefined()
      expect(app.config.warnHandler).toBeDefined()
    })

    it('captures errors thrown in Vue lifecycle', async () => {
      window.fetch = mockFetch as unknown as typeof fetch
      const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
      const tracker = createErrorTracker({ monitorBackends: [new BuiltinMonitorBackend()] })
      const app = createApp({
        mounted() { throw new Error('vue-lifecycle-error') },
        render() { return h('div') },
      })
      app.config.warnHandler = vi.fn()
      app.use(createPinia())
      app.use(tracker.vuePlugin)

      app.mount(document.createElement('div'))

      for (let i = 0; i < 9; i++) {
        tracker.captureMessage(`fill ${i}`)
      }

      await vi.waitFor(() => {
        const ingestCalls = mockFetch.mock.calls.filter(
          (c: unknown[]) => typeof c[0] === 'string' && (c[0] as string).includes('/ingest'),
        )
        expect(ingestCalls.length).toBeGreaterThanOrEqual(1)
      }, { timeout: 5000 })

      expect(consoleError).toHaveBeenCalledWith(
        '[vue] mounted hook:',
        expect.objectContaining({ message: 'vue-lifecycle-error' }),
      )
      app.unmount()
      tracker.dispose()
      consoleError.mockRestore()
    })
  })

  describe('dispose', () => {
    it('cleans up and allows creating a new tracker', () => {
      const t1 = createErrorTracker({ appName: 'first' })
      t1.dispose()
      expect(getErrorTracker()).toBeNull()

      const t2 = createErrorTracker({ appName: 'second' })
      expect(t2).not.toBeNull()
      expect(getErrorTracker()).toBe(t2)
      t2.dispose()
    })

    it('does not crash if called twice', () => {
      const tracker = createErrorTracker()
      tracker.dispose()
      expect(() => tracker.dispose()).not.toThrow()
    })
  })
})

describe('BreadcrumbCollector', () => {
  it('collects click breadcrumbs', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    const btn = document.createElement('button')
    btn.className = 'test-btn primary'
    btn.textContent = 'Click Me'
    document.body.appendChild(btn)
    btn.click()

    const crumbs = collector.getBreadcrumbs()
    expect(crumbs.length).toBeGreaterThanOrEqual(1)
    const clickCrumb = crumbs.find((c) => c.type === 'click')
    expect(clickCrumb).toBeDefined()
    expect(clickCrumb!.data.target).toContain('button')
    expect(clickCrumb!.data.text).toContain('Click Me')

    document.body.removeChild(btn)
    collector.stopAutoCapture()
  })

  it('collects API call breadcrumbs', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    collector.captureApiCall('POST', '/api/test', 201)
    const crumbs = collector.getBreadcrumbs()
    const apiCrumb = crumbs.find((c) => c.type === 'api')
    expect(apiCrumb).toBeDefined()
    expect(apiCrumb!.data.method).toBe('POST')
    expect(apiCrumb!.data.url).toBe('/api/test')
    expect(apiCrumb!.data.statusCode).toBe(201)

    collector.stopAutoCapture()
  })

  it('collects route change breadcrumbs', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    collector.captureRouteChange('dashboard', 'library')
    const crumbs = collector.getBreadcrumbs()
    const routeCrumb = crumbs.find((c) => c.type === 'route_change')
    expect(routeCrumb).toBeDefined()
    expect(routeCrumb!.data.from).toBe('dashboard')
    expect(routeCrumb!.data.to).toBe('library')

    collector.stopAutoCapture()
  })

  it('adds navigation breadcrumbs', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    collector.add('navigation', { action: 'scroll' })
    const crumbs = collector.getBreadcrumbs()
    const navCrumb = crumbs.find((c) => c.type === 'navigation')
    expect(navCrumb).toBeDefined()
    expect(navCrumb!.data.action).toBe('scroll')

    collector.stopAutoCapture()
  })

  it('limits the buffer to 50 entries', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    for (let i = 0; i < 60; i++) {
      collector.add('navigation', { index: i })
    }

    const crumbs = collector.getBreadcrumbs()
    expect(crumbs).toHaveLength(50)
    expect(crumbs[0].data.index).toBe(10)

    collector.stopAutoCapture()
  })

  it('clear empties the buffer', () => {
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    collector.add('navigation', { action: 'test' })
    expect(collector.getBreadcrumbs()).toHaveLength(1)

    collector.clear()
    expect(collector.getBreadcrumbs()).toHaveLength(0)

    collector.stopAutoCapture()
  })

  it('respects __MODULO_ERROR_TRACKING_DISABLED__', () => {
    (window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__ = true
    const collector = new BreadcrumbCollector(50)
    collector.startAutoCapture()

    collector.add('navigation', { action: 'test' })
    expect(collector.getBreadcrumbs()).toHaveLength(0)

    collector.stopAutoCapture()
  })
})

describe('Transport batching', () => {
  it('flushes when 10 errors are enqueued', async () => {
    window.fetch = mockFetch as unknown as typeof fetch
    const tracker = createErrorTracker({ monitorBackends: [new BuiltinMonitorBackend()] })

    for (let i = 0; i < 10; i++) {
      tracker.captureError(new Error(`error ${i}`))
    }

    await vi.waitFor(() => {
      const ingestCalls = mockFetch.mock.calls.filter(
        (c: unknown[]) => typeof c[0] === 'string' && (c[0] as string).includes('/ingest'),
      )
      expect(ingestCalls.length).toBe(1)
    }, { timeout: 5000 })

    tracker.dispose()
  })

  it('sets timer on first error and clears on dispose', () => {
    vi.useFakeTimers()
    window.fetch = mockFetch as unknown as typeof fetch
    const tracker = createErrorTracker({ monitorBackends: [new BuiltinMonitorBackend()] })

    // Before any error, no timer
    const t1 = vi.getTimerCount()
    tracker.captureMessage('first error')
    // Timer should now be active
    expect(vi.getTimerCount()).toBe(t1 + 1)

    tracker.dispose()
    // After dispose, the timer should have been cleared
    expect(vi.getTimerCount()).toBe(t1)

    vi.useRealTimers()
  })

  it('gets session key before first ingest', async () => {
    window.fetch = mockFetch as unknown as typeof fetch
    const tracker = createErrorTracker({ monitorBackends: [new BuiltinMonitorBackend()] })

    for (let i = 0; i < 10; i++) {
      tracker.captureError(new Error(`error ${i}`))
    }

    await vi.waitFor(() => {
      const keyCalls = mockFetch.mock.calls.filter(
        (c: unknown[]) => typeof c[0] === 'string' && (c[0] as string).includes('/session-key'),
      )
      expect(keyCalls.length).toBeGreaterThanOrEqual(1)
    }, { timeout: 5000 })

    tracker.dispose()
  })

  it('handles fetch failure gracefully', async () => {
    window.fetch = mockFetch as unknown as typeof fetch
    mockFetch.mockRejectedValue(new Error('network error'))
    const tracker = createErrorTracker()

    expect(() => {
      tracker.captureError(new Error('should not throw'))
    }).not.toThrow()

    tracker.dispose()
  })
})

describe('Context enrichment', () => {
  it('includes URL and viewport in context', () => {
    const mockBackend = new BuiltinMonitorBackend()
    const captureSpy = vi.spyOn(mockBackend, 'captureError')
    const tracker = createErrorTracker({ monitorBackends: [mockBackend] })
    const error = new Error('context test')
    tracker.captureError(error)

    expect(captureSpy).toHaveBeenCalledTimes(1)
    const event = captureSpy.mock.calls[0]?.[0]
    expectRecord(event?.context_json)
    expect(event.context_json.url).toBeTypeOf('string')
    expectRecord(event.context_json.viewport)
    expect(event.context_json.viewport.width).toBeTypeOf('number')
    expect(event.context_json.viewport.height).toBeTypeOf('number')

    tracker.dispose()
  })

  it('includes tier from planStore', () => {
    setActivePinia(createPinia())
    const plan = usePlanStore()
    plan.$patch({ currentTier: 'team' })

    const mockBackend = new BuiltinMonitorBackend()
    const captureSpy = vi.spyOn(mockBackend, 'captureError')
    const tracker = createErrorTracker({ monitorBackends: [mockBackend] })
    const error = new Error('tier test')
    tracker.captureError(error)

    expect(captureSpy).toHaveBeenCalledTimes(1)
    const event = captureSpy.mock.calls[0]?.[0]
    expectRecord(event?.context_json)
    expect(event.context_json.tier).toBe('team')

    tracker.dispose()
  })
})
