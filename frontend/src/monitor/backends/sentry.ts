/**
 * Sentry MonitorBackend
 *
 * Privacy data sheet:
 * - Domains: *.ingest.sentry.io, *.sentry.io
 * - Data collected: error stack traces, breadcrumbs, user-agent,
 *   page URL, performance metrics, session replays (if enabled)
 * - Cookies: sentry* (session replay opt-out, ~1yr persistence)
 * - Config knobs:
 *   - replaysSessionSampleRate (0.0-1.0, default 0 = no replays)
 *   - replaysOnErrorSampleRate (0.0-1.0, default 1.0)
 *   - tracesSampleRate (0.0-1.0, default 0 = no performance)
 * - Residency: configurable via DSN endpoint
 *   (sentry.io / o1.ingest.us.sentry.io for US region)
 * - CSP required: connect-src *.ingest.sentry.io
 *
 * @module
 */

import type { Breadcrumb, ErrorEventInput, MonitorBackend, MonitorConfig, MonitorLevel, UserInfo } from '../types'
import { isModuleNotFound } from './sdk-utils'

interface SentryApi {
  init(config: Record<string, unknown>): void
  captureException(error: Error, context: { extra: Record<string, unknown> }): void
  captureMessage(message: string, level: string): void
  setUser(user: Record<string, unknown> | null): void
  setTag(key: string, value: string): void
  addBreadcrumb(breadcrumb: Record<string, unknown>): void
  close(): void
}

export class SentryMonitorBackend implements MonitorBackend {
  readonly key = 'sentry'
  private initialized = false
  private sentry: SentryApi | null = null

  async init(config: MonitorConfig): Promise<boolean> {
    if (!config.sentry?.dsn) {
      console.warn('[monitor] Sentry: no DSN configured')
      return false
    }

    try {
      const sentryModule = await import(/* @vite-ignore */ '@sentry/vue')
      this.sentry = sentryModule as SentryApi

      sentryModule.init({
        dsn: config.sentry.dsn,
        environment: config.sentry.environment ?? 'production',
        integrations: [],
        tracesSampleRate: config.sentry.tracesSampleRate ?? 0,
        replaysSessionSampleRate: config.sentry.replaysSessionSampleRate ?? 0,
        replaysOnErrorSampleRate: config.sentry.replaysOnErrorSampleRate ?? 0,
      })

      this.initialized = true
      console.warn('[monitor] Sentry backend initialized')
      return true
    } catch (e: unknown) {
      if (isModuleNotFound(e)) {
        console.warn('[monitor] @sentry/vue not installed — Sentry backend unavailable. Run: npm install @sentry/vue')
      } else {
        console.error('[monitor] Sentry init failed:', e)
      }
      return false
    }
  }

  captureError(event: ErrorEventInput, error?: Error, context?: Record<string, unknown>): void {
    if (!this.initialized || !this.sentry) return
    try {
      this.sentry.captureException(error ?? new Error(event.message), {
        extra: { ...event.context_json, ...context },
      })
    } catch (e) {
      console.warn('[monitor] Sentry.captureException failed:', e)
    }
  }

  captureMessage(message: string, level: MonitorLevel): void {
    if (!this.initialized || !this.sentry) return
    const mappedLevel = level === 'critical' ? 'fatal' : level
    try {
      this.sentry.captureMessage(message, mappedLevel)
    } catch (e) {
      console.warn('[monitor] Sentry.captureMessage failed:', e)
    }
  }

  setUser(user: UserInfo | null): void {
    if (!this.initialized || !this.sentry) return
    try {
      this.sentry.setUser(user ? {
        id: user.id,
        email: user.email,
        username: user.name,
      } : null)
    } catch (e) {
      console.warn('[monitor] Sentry.setUser failed:', e)
    }
  }

  setTags(tags: Record<string, string>): void {
    if (!this.initialized || !this.sentry) return
    try {
      for (const [key, value] of Object.entries(tags)) {
        this.sentry.setTag(key, value)
      }
    } catch (e) {
      console.warn('[monitor] Sentry.setTags failed:', e)
    }
  }

  addBreadcrumb(breadcrumb: Breadcrumb): void {
    if (!this.initialized || !this.sentry) return
    try {
      this.sentry.addBreadcrumb({
        type: breadcrumb.type,
        category: breadcrumb.type,
        data: breadcrumb.data,
        timestamp: breadcrumb.timestamp
          ? new Date(breadcrumb.timestamp).getTime() / 1000
          : undefined,
      })
    } catch (e) {
      console.warn('[monitor] Sentry.addBreadcrumb failed:', e)
    }
  }

  dispose(): void {
    if (this.sentry) {
      try {
        this.sentry.close()
      } catch { /* ignore */ }
    }
    this.initialized = false
    this.sentry = null
  }
}
