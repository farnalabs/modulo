import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { loadMonitorConfig } from '../monitor/config'

beforeEach(() => {
  delete (window as unknown as Record<string, unknown>).__MODULO_CONFIG__
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('loadMonitorConfig', () => {
  it('defaults to builtin when no env vars or window config are set', () => {
    const config = loadMonitorConfig()
    expect(config.builtin).toEqual({ enabled: true })
    expect(config.sentry).toBeUndefined()
    expect(config['datadog-rum']).toBeUndefined()
    expect(config['grafana-faro']).toBeUndefined()
  })

  it('works without window.__MODULO_CONFIG__', () => {
    expect(() => loadMonitorConfig()).not.toThrow()
    const config = loadMonitorConfig()
    expect(config.builtin).toBeDefined()
  })

  it('reads backends from window.__MODULO_CONFIG__.monitor.monitorBackends', () => {
    window.__MODULO_CONFIG__ = {
      monitor: {
        monitorBackends: ['sentry'],
        sentry: { dsn: 'https://key@sentry.io/project' },
      },
    }
    const config = loadMonitorConfig()
    expect(config.sentry).toBeDefined()
    expect(config.sentry!.dsn).toBe('https://key@sentry.io/project')
    expect(config.builtin).toBeUndefined()
  })

  it('includes datadog-rum when configured via window config', () => {
    window.__MODULO_CONFIG__ = {
      monitor: {
        monitorBackends: ['datadog-rum'],
        'datadog-rum': { clientToken: 'ddtkn123', site: 'datadoghq.eu' },
      },
    }
    const config = loadMonitorConfig()
    expect(config['datadog-rum']).toBeDefined()
    expect(config['datadog-rum']!.clientToken).toBe('ddtkn123')
    expect(config['datadog-rum']!.site).toBe('datadoghq.eu')
  })

  it('includes grafana-faro when configured via window config', () => {
    window.__MODULO_CONFIG__ = {
      monitor: {
        monitorBackends: ['grafana-faro'],
        'grafana-faro': { url: 'https://faro.example.com/collect' },
      },
    }
    const config = loadMonitorConfig()
    expect(config['grafana-faro']).toBeDefined()
    expect(config['grafana-faro']!.url).toBe('https://faro.example.com/collect')
  })

  it('uses window config over env vars when both are present', () => {
    window.__MODULO_CONFIG__ = {
      monitor: {
        monitorBackends: ['sentry'],
        sentry: { dsn: 'https://runtime@sentry.io/project' },
      },
    }
    const config = loadMonitorConfig()
    expect(config.sentry).toBeDefined()
    expect(config.sentry!.dsn).toBe('https://runtime@sentry.io/project')
  })
})
