import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MonitorConfig } from '../monitor/types'
import { DatadogRumMonitorBackend } from '../monitor/backends/datadog-rum'
import { SentryMonitorBackend } from '../monitor/backends/sentry'
import { GrafanaFaroMonitorBackend } from '../monitor/backends/grafana-faro'
import { loadMonitorConfig, loadBackends } from '../monitor/index'
import { isModuleNotFound as realIsModuleNotFound } from '../monitor/backends/sdk-utils'
import * as sdkUtils from '../monitor/backends/sdk-utils'

const { ddRum, ddLogs, sentry, faroInit, faroApi, logsSdkMode } = vi.hoisted(() => {
  const ddRum = {
    init: vi.fn(),
    addError: vi.fn(),
    setUser: vi.fn(),
    clearUser: vi.fn(),
    addGlobalContext: vi.fn(),
  }
  const ddLogs = { init: vi.fn(), logger: { log: vi.fn() } }
  const sentry = {
    init: vi.fn(),
    captureException: vi.fn(),
    captureMessage: vi.fn(),
    setUser: vi.fn(),
    setTag: vi.fn(),
    addBreadcrumb: vi.fn(),
    close: vi.fn(),
  }
  const faroInit = vi.fn()
  const faroApi = {
    pushError: vi.fn(),
    pushLog: vi.fn(),
    setUser: vi.fn(),
    resetUser: vi.fn(),
    setSessionProperty: vi.fn(),
  }
  const logsSdkMode = { value: 'normal' as 'normal' | 'missing' }
  return { ddRum, ddLogs, sentry, faroInit, faroApi, logsSdkMode }
})

/**
 * The backends load their SDKs through runtime dynamic imports, so the SDK
 * mock must be (re-)registered non-hoisted: a test that needs a module to
 * FAIL to load overrides the registration with a throwing vi.doMock, and the
 * next beforeEach restores the normal mock for everyone else.
 */
function registerNormalSdkMocks(): void {
  vi.doMock('@datadog/browser-rum', () => ({ datadogRum: ddRum }))
  // The logs SDK export is a getter that consults logsSdkMode at property
  // access time (inside the backend's try/catch) — a single registration
  // means there is no mock-registry race between "normal" and "missing".
  vi.doMock('@datadog/browser-logs', () => ({
    get datadogLogs() {
      if (logsSdkMode.value === 'missing') {
        throw new Error('simulated missing @datadog/browser-logs')
      }
      return ddLogs
    },
  }))
  vi.doMock('@sentry/vue', () => sentry)
  vi.doMock('@grafana/faro-web-sdk', () => ({ initializeFaro: faroInit, api: faroApi }))
}

function baseConfig(overrides: Partial<MonitorConfig> = {}): MonitorConfig {
  return { monitorBackends: [], ...overrides }
}

beforeEach(() => {
  vi.clearAllMocks()
  logsSdkMode.value = 'normal'
  registerNormalSdkMocks()
  vi.spyOn(console, 'warn').mockImplementation(() => undefined)
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__MODULO_CONFIG__
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('DatadogRumMonitorBackend', () => {
  it('refuses init without a clientToken', async () => {
    const backend = new DatadogRumMonitorBackend()
    await expect(backend.init(baseConfig())).resolves.toBe(false)
    expect(ddRum.init).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith('[monitor] Datadog RUM: no clientToken configured')
  })

  it('initializes RUM and the logs SDK with config defaults', async () => {
    const backend = new DatadogRumMonitorBackend()
    const ok = await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-1' } }))
    expect(ok).toBe(true)
    expect(ddRum.init).toHaveBeenCalledWith(expect.objectContaining({
      clientToken: 'tok-1',
      site: 'datadoghq.com',
      service: 'modulo',
      env: 'production',
      sessionSampleRate: 100,
      sessionReplaySampleRate: 0,
      defaultPrivacyLevel: 'mask-user-input',
    }))
    expect(ddLogs.init).toHaveBeenCalledWith(expect.objectContaining({ clientToken: 'tok-1' }))
  })

  it('passes site/service/env/version overrides through to both SDKs', async () => {
    const backend = new DatadogRumMonitorBackend()
    const ok = await backend.init(baseConfig({
      datadogRum: { clientToken: 'tok-2', site: 'datadoghq.eu', service: 'svc', env: 'staging', version: '1.2.3' },
    }))
    expect(ok).toBe(true)
    const rumCfg = ddRum.init.mock.calls[0]?.[0] as Record<string, unknown>
    expect(rumCfg.site).toBe('datadoghq.eu')
    expect(rumCfg.service).toBe('svc')
    expect(rumCfg.env).toBe('staging')
    expect(rumCfg.version).toBe('1.2.3')
    expect(ddLogs.init).toHaveBeenCalledWith(expect.objectContaining({ site: 'datadoghq.eu', env: 'staging' }))
  })

  it('still initializes when the optional logs SDK is missing', async () => {
    logsSdkMode.value = 'missing'
    const backend = new DatadogRumMonitorBackend()
    const ok = await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-3' } }))
    expect(ok).toBe(true)
    expect(ddRum.init).toHaveBeenCalledTimes(1)
    expect(ddLogs.init).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith(
      '[monitor] @datadog/browser-logs not available — logs SDK is optional',
      expect.anything(),
    )
  })

  it('reports a missing RUM SDK as unavailable', async () => {
    // vitest cannot deterministically reject the runtime dynamic import, so
    // simulate the "package genuinely not installed" condition: the SDK init
    // fails and the module-not-found detector matches, exercising the
    // backend's missing-package warning path.
    const detector = vi.spyOn(sdkUtils, 'isModuleNotFound').mockReturnValue(true)
    ddRum.init.mockImplementationOnce(() => { throw new Error('simulated module resolution failure') })
    const backend = new DatadogRumMonitorBackend()
    const ok = await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-4' } }))
    detector.mockRestore()
    expect(ok).toBe(false)
    expect(console.warn).toHaveBeenCalledWith(
      expect.stringContaining('@datadog/browser-rum not installed'),
    )
  })

  it('reports unexpected SDK failures via console.error', async () => {
    ddRum.init.mockImplementationOnce(() => { throw new Error('rum loader exploded') })
    const backend = new DatadogRumMonitorBackend()
    const ok = await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-5' } }))
    expect(ok).toBe(false)
    expect(console.error).toHaveBeenCalledWith('[monitor] Datadog RUM init failed:', expect.any(Error))
  })

  it('no-ops capture methods before init', async () => {
    const backend = new DatadogRumMonitorBackend()
    expect(() => {
      backend.captureError({ level: 'error', message: 'boom' })
      backend.captureMessage('hello', 'error')
      backend.setUser({ id: 'u1' })
      backend.setTags({ a: 'b' })
    }).not.toThrow()
    expect(ddRum.addError).not.toHaveBeenCalled()
    expect(ddLogs.logger.log).not.toHaveBeenCalled()
  })

  it('forwards captureError to addError with source, stacktrace and context', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-6' } }))
    backend.captureError({
      level: 'error',
      message: 'boom',
      source: 'frontend',
      stacktrace: 'Error: boom\n at x',
      context_json: { k: 'v' },
    })
    expect(ddRum.addError).toHaveBeenCalledWith('boom', {
      source: 'frontend',
      stacktrace: 'Error: boom\n at x',
      context: { k: 'v' },
    })
  })

  it('warns (never throws) when addError fails', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-7' } }))
    ddRum.addError.mockImplementationOnce(() => { throw new Error('sdk down') })
    expect(() => backend.captureError({ level: 'error', message: 'boom' })).not.toThrow()
    expect(console.warn).toHaveBeenCalledWith('[monitor] Datadog RUM addError failed:', expect.any(Error))
  })

  it('maps critical messages to the error log level via the logs SDK', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-8' } }))
    backend.captureMessage('oh no', 'critical')
    backend.captureMessage('heads up', 'warning')
    expect(ddLogs.logger.log).toHaveBeenNthCalledWith(1, 'oh no', {}, 'error')
    expect(ddLogs.logger.log).toHaveBeenNthCalledWith(2, 'heads up', {}, 'warning')
  })

  it('silently skips messages when the logs SDK is absent', async () => {
    logsSdkMode.value = 'missing'
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-9' } }))
    expect(() => backend.captureMessage('ignored', 'error')).not.toThrow()
    expect(ddLogs.logger.log).not.toHaveBeenCalled()
  })

  it('sets and clears the user', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-10' } }))
    backend.setUser({ id: 'u1', name: 'Duncan', email: 'd@modulo.run' })
    expect(ddRum.setUser).toHaveBeenCalledWith({ id: 'u1', name: 'Duncan', email: 'd@modulo.run' })
    backend.setUser(null)
    expect(ddRum.clearUser).toHaveBeenCalledTimes(1)
  })

  it('writes each tag as a global context entry', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-11' } }))
    backend.setTags({ tier: 'team', env: 'prod' })
    expect(ddRum.addGlobalContext).toHaveBeenCalledWith('tier', 'team')
    expect(ddRum.addGlobalContext).toHaveBeenCalledWith('env', 'prod')
  })

  it('drops the SDK handles on dispose so later captures no-op', async () => {
    const backend = new DatadogRumMonitorBackend()
    await backend.init(baseConfig({ datadogRum: { clientToken: 'tok-12' } }))
    backend.dispose()
    expect(() => backend.captureError({ level: 'error', message: 'after dispose' })).not.toThrow()
    expect(ddRum.addError).not.toHaveBeenCalled()
  })
})

describe('SentryMonitorBackend', () => {
  it('refuses init without a DSN', async () => {
    const backend = new SentryMonitorBackend()
    await expect(backend.init(baseConfig())).resolves.toBe(false)
    expect(sentry.init).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith('[monitor] Sentry: no DSN configured')
  })

  it('initializes with the DSN and safe sampling defaults', async () => {
    const backend = new SentryMonitorBackend()
    const ok = await backend.init(baseConfig({ sentry: { dsn: 'https://dsn@example.ingest.sentry.io/1' } }))
    expect(ok).toBe(true)
    expect(sentry.init).toHaveBeenCalledWith({
      dsn: 'https://dsn@example.ingest.sentry.io/1',
      environment: 'production',
      integrations: [],
      tracesSampleRate: 0,
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
    })
  })

  it('passes environment and sample-rate overrides', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({
      sentry: {
        dsn: 'https://dsn',
        environment: 'staging',
        tracesSampleRate: 0.5,
        replaysSessionSampleRate: 0.2,
        replaysOnErrorSampleRate: 0.9,
      },
    }))
    expect(sentry.init).toHaveBeenCalledWith(expect.objectContaining({
      environment: 'staging',
      tracesSampleRate: 0.5,
      replaysSessionSampleRate: 0.2,
      replaysOnErrorSampleRate: 0.9,
    }))
  })

  it('reports a missing SDK as unavailable', async () => {
    const detector = vi.spyOn(sdkUtils, 'isModuleNotFound').mockReturnValue(true)
    sentry.init.mockImplementationOnce(() => { throw new Error('simulated module resolution failure') })
    const backend = new SentryMonitorBackend()
    const ok = await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    detector.mockRestore()
    expect(ok).toBe(false)
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('@sentry/vue not installed'))
  })

  it('reports unexpected SDK failures via console.error', async () => {
    sentry.init.mockImplementationOnce(() => { throw new Error('sentry loader exploded') })
    const backend = new SentryMonitorBackend()
    const ok = await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    expect(ok).toBe(false)
    expect(console.error).toHaveBeenCalledWith('[monitor] Sentry init failed:', expect.any(Error))
  })

  it('no-ops capture methods before init', async () => {
    const backend = new SentryMonitorBackend()
    expect(() => {
      backend.captureError({ level: 'error', message: 'boom' })
      backend.captureMessage('m', 'error')
      backend.setUser(null)
      backend.setTags({})
    }).not.toThrow()
    expect(sentry.captureException).not.toHaveBeenCalled()
  })

  it('captures an explicit Error instance with merged extra context', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    const error = new Error('explicit')
    backend.captureError({ level: 'error', message: 'ignored', context_json: { a: 1 } }, error, { b: 2 })
    expect(sentry.captureException).toHaveBeenCalledWith(error, { extra: { a: 1, b: 2 } })
  })

  it('synthesises an Error from the event when none is provided', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.captureError({ level: 'error', message: 'synthesised' })
    expect(sentry.captureException).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'synthesised' }),
      { extra: {} },
    )
  })

  it('warns (never throws) when captureException fails', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    sentry.captureException.mockImplementationOnce(() => { throw new Error('down') })
    expect(() => backend.captureError({ level: 'error', message: 'boom' })).not.toThrow()
    expect(console.warn).toHaveBeenCalledWith('[monitor] Sentry.captureException failed:', expect.any(Error))
  })

  it('maps critical messages to fatal severity', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.captureMessage('fatal-ish', 'critical')
    backend.captureMessage('warn-ish', 'warning')
    expect(sentry.captureMessage).toHaveBeenNthCalledWith(1, 'fatal-ish', 'fatal')
    expect(sentry.captureMessage).toHaveBeenNthCalledWith(2, 'warn-ish', 'warning')
  })

  it('maps user info to the sentry user shape and clears with null', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.setUser({ id: 'u9', name: 'Duncan', email: 'd@modulo.run' })
    expect(sentry.setUser).toHaveBeenCalledWith({ id: 'u9', email: 'd@modulo.run', username: 'Duncan' })
    backend.setUser(null)
    expect(sentry.setUser).toHaveBeenLastCalledWith(null)
  })

  it('forwards each tag to setTag', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.setTags({ a: '1', b: '2' })
    expect(sentry.setTag).toHaveBeenCalledWith('a', '1')
    expect(sentry.setTag).toHaveBeenCalledWith('b', '2')
  })

  it('converts breadcrumb timestamps to sentry epoch seconds', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    const iso = '2026-09-01T12:00:00.000Z'
    backend.addBreadcrumb({ type: 'api', timestamp: iso, data: { method: 'GET' } })
    expect(sentry.addBreadcrumb).toHaveBeenCalledWith({
      type: 'api',
      category: 'api',
      data: { method: 'GET' },
      timestamp: new Date(iso).getTime() / 1000,
    })
  })

  it('omits the timestamp for an unparseable breadcrumb date', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.addBreadcrumb({ type: 'click', timestamp: 'not-a-date' })
    expect(sentry.addBreadcrumb).toHaveBeenCalledWith({
      type: 'click',
      category: 'click',
      data: undefined,
      timestamp: undefined,
    })
  })

  it('closes the SDK on dispose and stops capturing afterwards', async () => {
    const backend = new SentryMonitorBackend()
    await backend.init(baseConfig({ sentry: { dsn: 'https://dsn' } }))
    backend.dispose()
    expect(sentry.close).toHaveBeenCalledTimes(1)
    backend.captureError({ level: 'error', message: 'after' })
    expect(sentry.captureException).not.toHaveBeenCalled()
  })

  it('never-initialised dispose is a safe no-op', async () => {
    const backend = new SentryMonitorBackend()
    expect(() => backend.dispose()).not.toThrow()
    expect(sentry.close).not.toHaveBeenCalled()
  })
})

describe('GrafanaFaroMonitorBackend', () => {
  it('refuses init without a collector URL', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await expect(backend.init(baseConfig())).resolves.toBe(false)
    expect(faroInit).not.toHaveBeenCalled()
    expect(console.warn).toHaveBeenCalledWith('[monitor] Grafana Faro: no collector URL configured')
  })

  it('initializes with url, apiKey and app defaults', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    const ok = await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example', apiKey: 'k-1' } }))
    expect(ok).toBe(true)
    expect(faroInit).toHaveBeenCalledWith(expect.objectContaining({
      url: 'https://collector.example',
      apiKey: 'k-1',
      app: expect.objectContaining({ name: 'modulo', environment: import.meta.env.MODE }),
    }))
  })

  it('honours a custom appName', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example', appName: 'custom' } }))
    const cfg = faroInit.mock.calls[0]?.[0] as { app: { name: string } }
    expect(cfg.app.name).toBe('custom')
  })

  it('reports a missing SDK as unavailable', async () => {
    const detector = vi.spyOn(sdkUtils, 'isModuleNotFound').mockReturnValue(true)
    faroInit.mockImplementationOnce(() => { throw new Error('simulated module resolution failure') })
    const backend = new GrafanaFaroMonitorBackend()
    const ok = await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    detector.mockRestore()
    expect(ok).toBe(false)
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('@grafana/faro-web-sdk not installed'))
  })

  it('reports unexpected SDK failures via console.error', async () => {
    faroInit.mockImplementationOnce(() => { throw new Error('faro loader exploded') })
    const backend = new GrafanaFaroMonitorBackend()
    const ok = await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    expect(ok).toBe(false)
    expect(console.error).toHaveBeenCalledWith('[monitor] Grafana Faro init failed:', expect.any(Error))
  })

  it('no-ops capture methods before init', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    expect(() => {
      backend.captureError({ level: 'error', message: 'boom' })
      backend.captureMessage('m', 'error')
      backend.setUser(null)
      backend.setTags({})
      backend.addBreadcrumb({ type: 'click' })
    }).not.toThrow()
    expect(faroApi.pushError).not.toHaveBeenCalled()
  })

  it('forwards captureError as a pushed error with stacktrace and context', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.captureError({ level: 'error', message: 'boom', stacktrace: 'st', context_json: { k: 1 } })
    expect(faroApi.pushError).toHaveBeenCalledWith(expect.objectContaining({ message: 'boom' }), {
      stacktrace: 'st',
      context: { k: 1 },
    })
  })

  it('pushes messages as logs, mapping critical to error', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.captureMessage('fatal-ish', 'critical')
    backend.captureMessage('warn-ish', 'warning')
    expect(faroApi.pushLog).toHaveBeenNthCalledWith(1, ['fatal-ish'], { level: 'error' })
    expect(faroApi.pushLog).toHaveBeenNthCalledWith(2, ['warn-ish'], { level: 'warning' })
  })

  it('sets and resets the user with name/role attributes', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.setUser({ id: 'u1', name: 'Duncan', email: 'd@modulo.run', role: 'admin' })
    expect(faroApi.setUser).toHaveBeenCalledWith({
      id: 'u1',
      email: 'd@modulo.run',
      attributes: { name: 'Duncan', role: 'admin' },
    })
    backend.setUser(null)
    expect(faroApi.resetUser).toHaveBeenCalledTimes(1)
  })

  it('writes each tag as a session property', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.setTags({ a: '1' })
    expect(faroApi.setSessionProperty).toHaveBeenCalledWith('a', '1')
  })

  it('pushes breadcrumbs as logs with context data', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.addBreadcrumb({ type: 'route_change', data: { from: 'a', to: 'b' } })
    expect(faroApi.pushLog).toHaveBeenCalledWith(['breadcrumb: route_change'], {
      context: { from: 'a', to: 'b' },
    })
  })

  it('drops the SDK handle on dispose so later captures no-op', async () => {
    const backend = new GrafanaFaroMonitorBackend()
    await backend.init(baseConfig({ grafanaFaro: { url: 'https://collector.example' } }))
    backend.dispose()
    backend.captureError({ level: 'error', message: 'after' })
    expect(faroApi.pushError).not.toHaveBeenCalled()
  })
})

describe('monitor/index', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_MONITOR_BACKEND', '')
    vi.stubEnv('VITE_SENTRY_DSN', '')
    vi.stubEnv('VITE_DATADOG_RUM_CLIENT_TOKEN', '')
    vi.stubEnv('VITE_GRAFANA_FARO_URL', '')
  })

  it('defaults to the builtin backend with no provider config', () => {
    const cfg = loadMonitorConfig()
    expect(cfg.monitorBackends).toEqual(['builtin'])
    expect(cfg.sentry).toBeUndefined()
    expect(cfg.datadogRum).toBeUndefined()
    expect(cfg.grafanaFaro).toBeUndefined()
  })

  it('reads backends and provider config from VITE_ env vars', () => {
    vi.stubEnv('VITE_MONITOR_BACKEND', 'builtin, sentry ,, datadog-rum')
    vi.stubEnv('VITE_SENTRY_DSN', 'https://env-dsn')
    vi.stubEnv('VITE_DATADOG_RUM_CLIENT_TOKEN', 'env-tok')
    vi.stubEnv('VITE_GRAFANA_FARO_URL', 'https://env-collector')
    const cfg = loadMonitorConfig()
    expect(cfg.monitorBackends).toEqual(['builtin', 'sentry', 'datadog-rum'])
    expect(cfg.sentry).toEqual({ dsn: 'https://env-dsn' })
    expect(cfg.datadogRum).toEqual({ clientToken: 'env-tok' })
    expect(cfg.grafanaFaro).toEqual({ url: 'https://env-collector' })
  })

  it('prefers the runtime __MODULO_CONFIG__ window override', () => {
    ;(window as unknown as Record<string, unknown>).__MODULO_CONFIG__ = {
      monitor: {
        monitorBackends: ['grafana-faro'],
        sentry: { dsn: 'rt-dsn' },
        datadogRum: { clientToken: 'rt-tok' },
        grafanaFaro: { url: 'rt-url' },
      },
    }
    vi.stubEnv('VITE_SENTRY_DSN', 'https://env-dsn')
    const cfg = loadMonitorConfig()
    expect(cfg.monitorBackends).toEqual(['grafana-faro'])
    expect(cfg.sentry).toEqual({ dsn: 'rt-dsn' })
    expect(cfg.datadogRum).toEqual({ clientToken: 'rt-tok' })
    expect(cfg.grafanaFaro).toEqual({ url: 'rt-url' })
  })

  it('falls back to env vars when the runtime config has no monitor key', () => {
    ;(window as unknown as Record<string, unknown>).__MODULO_CONFIG__ = { other: true }
    vi.stubEnv('VITE_MONITOR_BACKEND', 'sentry')
    expect(loadMonitorConfig().monitorBackends).toEqual(['sentry'])
  })

  it('loadBackends returns an initialised builtin backend', async () => {
    const backends = await loadBackends(baseConfig({ monitorBackends: ['builtin'] }))
    expect(backends).toHaveLength(1)
    expect(backends[0]?.key).toBe('builtin')
  })

  it('loadBackends skips unknown keys with a warning', async () => {
    const backends = await loadBackends(baseConfig({ monitorBackends: ['nope'] }))
    expect(backends).toHaveLength(0)
    expect(console.warn).toHaveBeenCalledWith('[monitor] Unknown backend: nope')
  })

  it('loadBackends excludes a backend whose init fails', async () => {
    const backends = await loadBackends(baseConfig({ monitorBackends: ['datadog-rum'] }))
    expect(backends).toHaveLength(0)
  })

  it('loadBackends includes sentry and the datadog_rum alias when configured', async () => {
    const backends = await loadBackends(baseConfig({
      monitorBackends: ['sentry', 'datadog_rum'],
      sentry: { dsn: 'https://dsn' },
      datadogRum: { clientToken: 'tok' },
    }))
    expect(backends.map((b) => b.key)).toEqual(['sentry', 'datadog-rum'])
  })

  it('loadBackends includes the grafana_faro alias when configured', async () => {
    const backends = await loadBackends(baseConfig({
      monitorBackends: ['grafana_faro'],
      grafanaFaro: { url: 'https://collector.example' },
    }))
    expect(backends.map((b) => b.key)).toEqual(['grafana-faro'])
  })
})

describe('isModuleNotFound', () => {
  it('detects the MODULE_NOT_FOUND error code', () => {
    const err = Object.assign(new Error('whatever'), { code: 'MODULE_NOT_FOUND' })
    expect(realIsModuleNotFound(err)).toBe(true)
  })

  it('detects the "Cannot find module" message', () => {
    expect(realIsModuleNotFound(new Error("Cannot find module '@sentry/vue'"))).toBe(true)
  })

  it('returns false for ordinary errors, non-errors, and missing code', () => {
    expect(realIsModuleNotFound(new Error('network down'))).toBe(false)
    expect(realIsModuleNotFound('just a string')).toBe(false)
    expect(realIsModuleNotFound(null)).toBe(false)
  })
})
