import type { MonitorBackend, ErrorEventInput, UserInfo } from './types'

export class MonitorBackendRegistry {
  private backends: MonitorBackend[] = []
  private messageRateLimit: Map<string, number> = new Map()
  private globalMessageCount: number = 0
  private globalMessageWindowStart: number = 0
  private static readonly MESSAGE_KEY_COOLDOWN_MS = 60_000
  private static readonly GLOBAL_MESSAGE_MAX_PER_MIN = 100
  private static readonly RATE_LIMIT_MAP_MAX_SIZE = 1000

  add(backend: MonitorBackend): void {
    this.backends.push(backend)
  }

  getBackends(): MonitorBackend[] {
    return this.backends
  }

  private callBackendMethod(backend: MonitorBackend, method: string, args: unknown[]): void {
    const fn = (backend as unknown as Record<string, unknown>)[method]
    if (typeof fn === 'function') {
      (fn as Function).apply(backend, args)
    }
  }

  dispatch(method: string, ...args: unknown[]): void {
    for (const backend of this.backends) {
      try {
        this.callBackendMethod(backend, method, args)
      } catch (e) {
        console.warn(`[monitor] ${backend.key}.${method} failed:`, e)
      }
    }
  }

  dispatchToImplementors(method: string, excludeWith?: string, ...args: unknown[]): void {
    for (const backend of this.backends) {
      if (excludeWith && typeof (backend as unknown as Record<string, unknown>)[excludeWith] === 'function') continue
      try {
        this.callBackendMethod(backend, method, args)
      } catch (e) {
        console.warn(`[monitor] ${backend.key}.${method} failed:`, e)
      }
    }
  }

  captureError(event: ErrorEventInput, rawError?: Error, rawContext?: Record<string, unknown>): void {
    if (rawError) {
      this.dispatchToImplementors('captureRawError', undefined, rawError, rawContext)
    }
    this.dispatchToImplementors('captureError', 'captureRawError', event)
  }

  private pruneStaleRateLimitEntries(): void {
    if (this.messageRateLimit.size <= MonitorBackendRegistry.RATE_LIMIT_MAP_MAX_SIZE) return
    const cutoff = Date.now() - MonitorBackendRegistry.MESSAGE_KEY_COOLDOWN_MS
    for (const [key, ts] of this.messageRateLimit) {
      if (ts < cutoff) this.messageRateLimit.delete(key)
    }
  }

  captureMessage(message: string, level: 'error' | 'warning' | 'critical' = 'warning'): void {
    const now = Date.now()
    const lastSent = this.messageRateLimit.get(message) ?? 0
    if (now - lastSent < MonitorBackendRegistry.MESSAGE_KEY_COOLDOWN_MS) return

    if (now - this.globalMessageWindowStart > 60_000) {
      this.globalMessageCount = 0
      this.globalMessageWindowStart = now
    }
    if (this.globalMessageCount >= MonitorBackendRegistry.GLOBAL_MESSAGE_MAX_PER_MIN) return

    this.messageRateLimit.set(message, now)
    this.pruneStaleRateLimitEntries()
    this.globalMessageCount++
    this.dispatch('captureMessage', message, level)
  }

  setUser(user: UserInfo | null): void {
    this.dispatch('setUser', user)
  }

  setTags(tags: Record<string, string>): void {
    this.dispatch('setTags', tags)
  }

  disposeAll(): void {
    this.dispatch('dispose')
    this.backends = []
    this.messageRateLimit.clear()
    this.globalMessageCount = 0
    this.globalMessageWindowStart = 0
  }
}
