/**
 * Grafana Faro MonitorBackend
 *
 * Privacy data sheet:
 * - Domains: user-configured collector URL
 * - Data collected: error stack traces, OTEL traces,
 *   user-agent, page URL, resource timings, console logs
 * - Cookies: none (intentionally — Faro does not set cookies)
 * - Config knobs:
 *   - url (collector endpoint, required)
 *   - apiKey (optional, for authenticated collectors)
 *   - appName (optional, defaults to 'modulo')
 * - Residency: determined by collector URL (user-controlled)
 * - CSP required: connect-src <collector-url>
 *
 * @module
 */

import type { Breadcrumb, ErrorEventInput, MonitorBackend, MonitorConfig, MonitorLevel, UserInfo } from '../types'
import { isModuleNotFound } from './sdk-utils'

interface FaroApi {
  pushError(error: Error, options: Record<string, unknown>): void
  pushLog(messages: string[], options: Record<string, unknown>): void
  setUser(user: Record<string, unknown>): void
  resetUser(): void
  setSessionProperty(key: string, value: string): void
}

interface FaroModule {
  initializeFaro(config: Record<string, unknown>): void
  api: FaroApi
}

export class GrafanaFaroMonitorBackend implements MonitorBackend {
  readonly key = 'grafana-faro'
  private initialized = false
  private faro: FaroModule | null = null

  async init(config: MonitorConfig): Promise<boolean> {
    if (!config.grafanaFaro?.url) {
      console.warn('[monitor] Grafana Faro: no collector URL configured')
      return false
    }

    try {
      const moduleName = '@grafana/faro-web-sdk'
      const faroModule = await import(/* @vite-ignore */ moduleName) as FaroModule
      this.faro = faroModule

      const cfg = config.grafanaFaro

      faroModule.initializeFaro({
        url: cfg.url,
        apiKey: cfg.apiKey,
        app: {
          name: cfg.appName ?? 'modulo',
          version: typeof import.meta !== 'undefined' ? (import.meta.env?.VITE_APP_VERSION as string) ?? '' : '',
          environment: typeof import.meta !== 'undefined' ? (import.meta.env?.MODE as string) ?? 'production' : 'production',
        },
      })

      this.initialized = true
      console.warn('[monitor] Grafana Faro backend initialized')
      return true
    } catch (e: unknown) {
      if (isModuleNotFound(e)) {
        console.warn('[monitor] @grafana/faro-web-sdk not installed — Grafana Faro unavailable. Run: npm install @grafana/faro-web-sdk')
      } else {
        console.error('[monitor] Grafana Faro init failed:', e)
      }
      return false
    }
  }

  captureError(event: ErrorEventInput): void {
    if (!this.initialized || !this.faro) return
    try {
      this.faro.api.pushError(new Error(event.message), {
        stacktrace: event.stacktrace,
        context: event.context_json,
      })
    } catch (e) {
      console.warn('[monitor] Grafana Faro pushError failed:', e)
    }
  }

  captureMessage(message: string, level: MonitorLevel): void {
    if (!this.initialized || !this.faro) return
    try {
      this.faro.api.pushLog([message], {
        level: level === 'critical' ? 'error' : level,
      })
    } catch (e) {
      console.warn('[monitor] Grafana Faro pushLog failed:', e)
    }
  }

  setUser(user: UserInfo | null): void {
    if (!this.initialized || !this.faro) return
    try {
      if (user) {
        this.faro.api.setUser({ id: user.id, email: user.email, attributes: { name: user.name, role: user.role } })
      } else {
        this.faro.api.resetUser()
      }
    } catch (e) {
      console.warn('[monitor] Grafana Faro setUser failed:', e)
    }
  }

  setTags(tags: Record<string, string>): void {
    if (!this.initialized || !this.faro) return
    try {
      for (const [key, value] of Object.entries(tags)) {
        this.faro.api.setSessionProperty(key, value)
      }
    } catch (e) {
      console.warn('[monitor] Grafana Faro setTags failed:', e)
    }
  }

  addBreadcrumb(breadcrumb: Breadcrumb): void {
    if (!this.initialized || !this.faro) return
    try {
      this.faro.api.pushLog([`breadcrumb: ${breadcrumb.type}`], {
        context: breadcrumb.data,
      })
    } catch (e) {
      console.warn('[monitor] Grafana Faro addBreadcrumb failed:', e)
    }
  }

  dispose(): void {
    this.initialized = false
    this.faro = null
  }
}
