import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { loadMonitorConfig } from '../monitor/config'

beforeEach(() => {
  delete (window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__
})

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('loadMonitorConfig', () => {
  it('defaults to enabled when no env var is set', () => {
    const config = loadMonitorConfig()
    expect(config.enabled).toBe(true)
    expect(config.environment).toBeDefined()
  })

  it('disables tracking when __MODULO_ERROR_TRACKING_DISABLED__ is true', () => {
    (window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__ = true
    const config = loadMonitorConfig()
    expect(config.enabled).toBe(false)
  })

  it('returns environment from import.meta.env.MODE', () => {
    const config = loadMonitorConfig()
    expect(config.environment).toBe('test')
  })
})
