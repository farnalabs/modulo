import type { App, ComponentPublicInstance } from 'vue'
import type { ErrorTrackerConfig, ErrorEventInput } from './types'
import { BreadcrumbCollector, getCollector } from './breadcrumbs'
import { gatherContext } from './context'
import { enqueueError, initTransport, disposeTransport } from './transport'
import { onAuthChange } from '../api/client'
import type { Router } from 'vue-router'

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
  private breadcrumbs: BreadcrumbCollector
  private unsubRouter: (() => void) | null = null

  constructor(config?: ErrorTrackerConfig) {
    _activeConfig = {
      appName: config?.appName ?? 'modulo',
      environment: config?.environment ?? 'production',
      version: config?.version ?? '',
      flushIntervalMs: config?.flushIntervalMs ?? 5000,
      batchSize: config?.batchSize ?? 10,
    }

    this.breadcrumbs = new BreadcrumbCollector(50)

    if (!isDisabled()) {
      this.breadcrumbs.startAutoCapture()
      initTransport(onAuthChange)
      installWindowHandlers()
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
    return createVuePlugin()
  }

  captureError(error: Error, context?: Record<string, unknown>): void {
    if (isDisabled()) return
    const event = buildErrorEvent(error, context)
    if (event) enqueueError(event)
  }

  captureMessage(message: string, level: 'error' | 'warning' | 'critical' = 'error'): void {
    if (isDisabled()) return
    const event = buildMessageEvent(message, level)
    enqueueError(event)
  }

  dispose(): void {
    if (this.unsubRouter) {
      this.unsubRouter()
      this.unsubRouter = null
    }
    this.breadcrumbs.stopAutoCapture()
    removeWindowHandlers()
    disposeTransport()
    _activeConfig = null
    _instance = null
  }
}

function isDisabled(): boolean {
  return !!(window as unknown as Record<string, unknown>).__MODULO_ERROR_TRACKING_DISABLED__
}

function buildBaseEvent(): ErrorEventInput {
  const config = getConfig()
  const collector = getCollector()
  return {
    level: 'error',
    message: '',
    context_json: gatherContext(),
    source: config.appName,
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

function buildMessageEvent(message: string, level: ErrorEventInput['level']): ErrorEventInput {
  const base = buildBaseEvent()
  base.level = level
  base.message = message
  return base
}

function createVuePlugin() {
  return {
    install(app: App): void {
      app.config.errorHandler = (err: unknown, _instance: unknown, info: string): void => {
        if (isDisabled()) return
        const error = err instanceof Error ? err : new Error(String(err))
        const event = buildErrorEvent(error, { vueInfo: info })
        if (event) enqueueError(event)
      }

      const origWarnHandler = app.config.warnHandler

      app.config.warnHandler = (msg: string, instance: ComponentPublicInstance | null, trace: string): void => {
        if (isDisabled()) return
        const event = buildMessageEvent(msg, 'warning')
        enqueueError(event)
        if (origWarnHandler) {
          origWarnHandler(msg, instance, trace)
        }
      }
    },
  }
}

let _installed = false

const _errorHandler = (event: ErrorEvent): void => {
  if (isDisabled() || !_instance) return
  const error = event.error ?? new Error(event.message ?? 'Unknown error')
  const err = error instanceof Error ? error : new Error(String(error))
  const extraCtx: Record<string, unknown> = {
    source: event.filename,
    line: event.lineno,
    col: event.colno,
  }
  const errorEvent = buildErrorEvent(err, extraCtx)
  if (errorEvent) enqueueError(errorEvent)
}

const _rejectionHandler = (event: PromiseRejectionEvent): void => {
  if (isDisabled() || !_instance) return
  const reason = event.reason
  const err = reason instanceof Error ? reason : new Error(String(reason))
  const errorEvent = buildErrorEvent(err, { type: 'unhandled_promise_rejection' })
  if (errorEvent) enqueueError(errorEvent)
}

function installWindowHandlers(): void {
  if (_installed) return
  _installed = true
  window.addEventListener('error', _errorHandler)
  window.addEventListener('unhandledrejection', _rejectionHandler)
}

function removeWindowHandlers(): void {
  if (!_installed) return
  window.removeEventListener('error', _errorHandler)
  window.removeEventListener('unhandledrejection', _rejectionHandler)
  _installed = false
}
