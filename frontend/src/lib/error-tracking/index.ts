import type { App, ComponentPublicInstance } from 'vue'
import type { ErrorTrackerConfig, ErrorEventInput } from './types'
import { BreadcrumbCollector, getCollector } from './breadcrumbs'
import { gatherContext } from './context'
import { initTransport, disposeTransport } from './transport'
import { onAuthChange } from '../api/client'
import type { Router } from 'vue-router'
import { MonitorBackendRegistry } from '../../monitor/registry'
import type { UserInfo, MonitorBackend } from '../../monitor/types'

interface ActiveConfig {
  appName: string
  environment: string
  version: string
  flushIntervalMs: number
  batchSize: number
}

let _instance: ErrorTracker | null = null
let _activeConfig: ActiveConfig | null = null

function getConfig(): ActiveConfig {
  return _activeConfig ?? {
    appName: 'modulo',
    environment: 'production',
    version: '',
    flushIntervalMs: 5000,
    batchSize: 10,
  }
}

export function createErrorTracker(config?: ErrorTrackerConfig): ErrorTracker {
  if (!_instance) {
    _instance = new ErrorTracker(config)
  }
  return _instance
}

export function getErrorTracker(): ErrorTracker | null {
  return _instance
}

export class ErrorTracker {
  private backends: MonitorBackendRegistry
  private breadcrumbs: BreadcrumbCollector
  private unsubRouter: (() => void) | null = null
  private _user: { id: string; email?: string; name?: string } | null = null
  private _tags: Record<string, string> = {}
  private _boundErrorHandler: ((event: ErrorEvent) => void) | null = null
  private _boundRejectionHandler: ((event: PromiseRejectionEvent) => void) | null = null

  constructor(config?: ErrorTrackerConfig) {
    _activeConfig = {
      appName: config?.appName ?? 'modulo',
      environment: config?.environment ?? 'production',
      version: config?.version ?? '',
      flushIntervalMs: config?.flushIntervalMs ?? 5000,
      batchSize: config?.batchSize ?? 10,
    }

    this.backends = new MonitorBackendRegistry()
    if (config?.monitorBackends) {
      for (const backend of config.monitorBackends) {
        this.backends.add(backend)
      }
    }

    this.breadcrumbs = new BreadcrumbCollector(50)

    if (!isDisabled()) {
      this.breadcrumbs.startAutoCapture()
      initTransport(onAuthChange)
      this.installWindowHandlers()
    }
  }

  connectRouter(router: Router): void {
    if (this.unsubRouter) this.unsubRouter()
    this.unsubRouter = router.afterEach((to, from) => {
      const collector = getCollector()
      if (collector) {
        collector.captureRouteChange(
          typeof from?.name === 'string' ? from.name : undefined,
          typeof to?.name === 'string' ? to.name : undefined,
        )
      }
    })
  }

  get vuePlugin() {
    return { install: (app: App) => this.installVuePlugin(app) }
  }

  captureError(error: Error, context?: Record<string, unknown>): void {
    if (isDisabled()) return
    const event = buildErrorEvent(error, context)
    if (event) {
      this.backends.captureError(event, error, context)
    }
  }

  captureMessage(message: string, level: 'error' | 'warning' | 'critical' = 'error'): void {
    if (isDisabled()) return
    this.backends.captureMessage(message, level)
  }

  setUser(user: UserInfo | null): void {
    this._user = user
    this.backends.setUser(user)
  }

  setTags(tags: Record<string, string>): void {
    this._tags = { ...tags }
    this.backends.setTags(tags)
  }

  async reloadBackends(newBackends: MonitorBackend[]): Promise<void> {
    this.backends.disposeAll()
    for (const b of newBackends) {
      this.backends.add(b)
    }
    if (this._user) this.backends.setUser(this._user)
    if (Object.keys(this._tags).length > 0) this.backends.setTags(this._tags)
  }

  dispose(): void {
    if (this.unsubRouter) {
      this.unsubRouter()
      this.unsubRouter = null
    }
    this.removeWindowHandlers()
    this.breadcrumbs.stopAutoCapture()
    this.backends.disposeAll()
    disposeTransport()
    _activeConfig = null
    _instance = null
  }

  installVuePlugin(app: App): void {
    app.config.errorHandler = (err: unknown, _instance: unknown, info: string): void => {
      if (isDisabled()) return
      console.error(`[vue] ${info}:`, err)
      const error = err instanceof Error ? err : new Error(String(err))
      this.captureError(error, { vueInfo: info })
    }

    const origWarnHandler = app.config.warnHandler
    app.config.warnHandler = (msg: string, instance: ComponentPublicInstance | null, trace: string): void => {
      if (isDisabled()) return
      this.captureMessage(msg, 'warning')
      if (origWarnHandler) {
        origWarnHandler(msg, instance, trace)
      }
    }
  }

  private installWindowHandlers(): void {
    this._boundErrorHandler = (event: ErrorEvent) => this._onError(event)
    this._boundRejectionHandler = (event: PromiseRejectionEvent) => this._onRejection(event)
    window.addEventListener('error', this._boundErrorHandler)
    window.addEventListener('unhandledrejection', this._boundRejectionHandler)
  }

  private removeWindowHandlers(): void {
    if (this._boundErrorHandler) {
      window.removeEventListener('error', this._boundErrorHandler)
      this._boundErrorHandler = null
    }
    if (this._boundRejectionHandler) {
      window.removeEventListener('unhandledrejection', this._boundRejectionHandler)
      this._boundRejectionHandler = null
    }
  }

  private _onError(event: ErrorEvent): void {
    if (isDisabled()) return
    const error = event.error ?? new Error(event.message ?? 'Unknown error')
    const err = error instanceof Error ? error : new Error(String(error))
    this.captureError(err, {
      source: event.filename,
      line: event.lineno,
      col: event.colno,
    })
  }

  private _onRejection(event: PromiseRejectionEvent): void {
    if (isDisabled()) return
    const reason = event.reason
    const err = reason instanceof Error ? reason : new Error(String(reason))
    this.captureError(err, { type: 'unhandled_promise_rejection' })
  }
}

function isDisabled(): boolean {
  return !!(window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__
}

function buildBaseEvent(): ErrorEventInput {
  const config = getConfig()
  const collector = getCollector()
  const ctx = gatherContext()
  if (_instance) {
    if (_instance._user) {
      ctx.user = _instance._user
    }
    if (Object.keys(_instance._tags).length > 0) {
      ctx.tags = _instance._tags
    }
  }
  return {
    level: 'error',
    message: '',
    context_json: ctx,
    source: 'frontend',
    environment: config.environment,
    version: config.version || undefined,
    breadcrumbs: collector?.getBreadcrumbs(),
  }
}

function buildErrorEvent(error: Error, extraContext?: Record<string, unknown>): ErrorEventInput | null {
  if (!error || typeof error.message !== 'string') return null
  const base = buildBaseEvent()
  base.message = error.message
  base.stacktrace = error.stack
  if (extraContext) {
    base.context_json = { ...base.context_json, ...extraContext }
  }
  return base
}
