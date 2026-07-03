/**
 * Datadog RUM MonitorBackend
 *
 * Privacy data sheet:
 * - Domains: *.datadoghq.com, *.dd.dg, *.rum.browserevents.com
 * - Data collected: RUM performance metrics, resource timings,
 *   user interactions, console logs (if enabled), viewport,
 *   page URL, user-agent
 * - Cookies: _dd_*, dd_* (session replay, persistent ~1yr)
 * - Config knobs:
 *   - sessionSampleRate (0-100, default 100)
 *   - sessionReplaySampleRate (0-100, default 0)
 *   - trackUserInteractions (boolean, default true)
 *   - trackResources (boolean, default true)
 *   - trackLongTasks (boolean, default true)
 * - Residency: configurable via site parameter
 *   (datadoghq.com / datadoghq.eu / us3.datadoghq.com)
 * - CSP required: connect-src *.datadoghq.com *.dd.dg *.rum.browserevents.com
 *
 * @module
 */

import type { MonitorBackend, MonitorConfig, ErrorEventInput, UserInfo } from '../types'

export class DatadogRumMonitorBackend implements MonitorBackend {
  readonly key = 'datadog-rum'
  private initialized = false
  private ddRum: any = null
  private ddLogger: any = null

  async init(config: MonitorConfig): Promise<boolean> {
    if (!config['datadog-rum']?.clientToken) {
      console.warn('[monitor] Datadog RUM: no clientToken configured')
      return false
    }

    try {
      const datadogRum = await import(/* @vite-ignore */ '@datadog/browser-rum')
      this.ddRum = datadogRum.datadogRum

      try {
        const datadogLogs = await import(/* @vite-ignore */ '@datadog/browser-logs')
        this.ddLogger = datadogLogs.datadogLogs
      } catch { /* logs SDK is optional */ }

      const cfg = config['datadog-rum']!

      this.ddRum.init({
        clientToken: cfg.clientToken,
        site: cfg.site ?? 'datadoghq.com',
        service: cfg.service ?? 'modulo',
        env: cfg.env ?? 'production',
        version: cfg.version ?? '',
        sessionSampleRate: 100,
        sessionReplaySampleRate: 0,
        trackUserInteractions: true,
        trackResources: true,
        trackLongTasks: true,
        defaultPrivacyLevel: 'mask-user-input',
      })

      if (this.ddLogger) {
        this.ddLogger.init({
          clientToken: cfg.clientToken,
          site: cfg.site ?? 'datadoghq.com',
          service: cfg.service ?? 'modulo',
          env: cfg.env ?? 'production',
          version: cfg.version ?? '',
          forwardErrorsToLogs: true,
        })
      }

      this.initialized = true
      console.info('[monitor] Datadog RUM backend initialized')
      return true
    } catch (e: any) {
      if (e?.code === 'MODULE_NOT_FOUND' || e?.message?.includes('Cannot find module')) {
        console.warn('[monitor] @datadog/browser-rum not installed — Datadog RUM unavailable. Run: npm install @datadog/browser-rum')
      } else {
        console.error('[monitor] Datadog RUM init failed:', e)
      }
      return false
    }
  }

  captureError(event: ErrorEventInput): void {
    if (!this.initialized || !this.ddRum) return
    try {
      this.ddRum.addError(event.message, {
        source: event.source,
        stacktrace: event.stacktrace,
        context: event.context_json,
      })
    } catch (e) {
      console.warn('[monitor] Datadog RUM addError failed:', e)
    }
  }

  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void {
    if (!this.initialized) return
    try {
      if (this.ddLogger) {
        this.ddLogger.logger.log(message, {}, level === 'critical' ? 'error' : level)
      }
    } catch (e) {
      console.warn('[monitor] Datadog logs log failed:', e)
    }
  }

  setUser(user: UserInfo | null): void {
    if (!this.initialized || !this.ddRum) return
    try {
      if (user) {
        this.ddRum.setUser({ id: user.id, name: user.name, email: user.email })
      } else {
        this.ddRum.clearUser()
      }
    } catch (e) {
      console.warn('[monitor] Datadog RUM setUser failed:', e)
    }
  }

  setTags(tags: Record<string, string>): void {
    if (!this.initialized || !this.ddRum) return
    try {
      for (const [key, value] of Object.entries(tags)) {
        this.ddRum.addGlobalContext(key, value)
      }
    } catch (e) {
      console.warn('[monitor] Datadog RUM setTags failed:', e)
    }
  }

  dispose(): void {
    this.initialized = false
    this.ddRum = null
    this.ddLogger = null
  }
}
