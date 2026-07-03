import type { MonitorBackend, MonitorConfig, ErrorEventInput } from '../types'
import { enqueueError, disposeTransport } from '../../lib/error-tracking/transport'

export class BuiltinMonitorBackend implements MonitorBackend {
  readonly key = 'builtin'

  init(_config: MonitorConfig): boolean {
    return true
  }

  captureError(event: ErrorEventInput): void {
    enqueueError({
      ...event,
      source: 'frontend',
    })
  }

  captureMessage(message: string, level: 'error' | 'warning' | 'critical'): void {
    enqueueError({
      level,
      message,
      source: 'frontend',
    })
  }

  dispose(): void {
    disposeTransport()
  }
}
