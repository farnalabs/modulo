import type { ErrorEventInput, MonitorBackend, MonitorLevel, UserInfo } from './types'

export class MonitorBackendRegistry {
  private backends: MonitorBackend[] = []

  add(backend: MonitorBackend): void {
    this.backends.push(backend)
  }

  remove(backend: MonitorBackend): void {
    const idx = this.backends.indexOf(backend)
    if (idx >= 0) this.backends.splice(idx, 1)
  }

  captureError(event: ErrorEventInput, error?: Error, context?: Record<string, unknown>): void {
    for (const backend of this.backends) {
      try {
        backend.captureError(event, error, context)
      } catch (error) {
        console.error('[MonitorBackendRegistry] Backend error:', error)
      }
    }
  }

  captureMessage(message: string, level: MonitorLevel): void {
    for (const backend of this.backends) {
      try {
        backend.captureMessage(message, level)
      } catch (error) {
        console.error('[MonitorBackendRegistry] Backend error:', error)
      }
    }
  }

  setUser(user: UserInfo | null): void {
    for (const backend of this.backends) {
      if (typeof backend.setUser === 'function') {
        backend.setUser(user)
      }
    }
  }

  setTags(tags: Record<string, string>): void {
    for (const backend of this.backends) {
      if (typeof backend.setTags === 'function') {
        backend.setTags(tags)
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
