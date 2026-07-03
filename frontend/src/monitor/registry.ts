import type { MonitorBackend, MonitorEvent, UserInfo } from './types'

export class MonitorBackendRegistry implements MonitorBackend {
  private backends: MonitorBackend[] = []

  add(backend: MonitorBackend): void {
    this.backends.push(backend)
  }

  remove(backend: MonitorBackend): void {
    const idx = this.backends.indexOf(backend)
    if (idx >= 0) this.backends.splice(idx, 1)
  }

  captureError(event: MonitorEvent, error: Error, context?: Record<string, unknown>): void {
    for (const backend of this.backends) {
      try {
        backend.captureError(event, error, context)
      } catch (e) {
        console.error('[MonitorBackendRegistry] Backend error:', e)
      }
    }
  }

  captureMessage(message: string, level: string): void {
    for (const backend of this.backends) {
      try {
        backend.captureMessage(message, level)
      } catch (e) {
        console.error('[MonitorBackendRegistry] Backend error:', e)
      }
    }
  }

  setUser(user: UserInfo | null): void {
    for (const backend of this.backends) {
      try {
        backend.setUser(user)
      } catch (e) {
        console.error('[MonitorBackendRegistry] Backend error:', e)
      }
    }
  }

  setTags(tags: Record<string, string>): void {
    for (const backend of this.backends) {
      try {
        backend.setTags(tags)
      } catch (e) {
        console.error('[MonitorBackendRegistry] Backend error:', e)
      }
    }
  }

  disposeAll(): void {
    for (const backend of this.backends) {
      try {
        backend.dispose()
      } catch (e) {
        console.error('[MonitorBackendRegistry] Backend error:', e)
      }
    }
    this.backends.length = 0
  }
}
