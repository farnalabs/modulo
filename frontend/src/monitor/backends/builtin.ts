import type { ErrorEventInput, MonitorBackend, MonitorConfig, MonitorLevel, UserInfo } from '../types'
import { enqueueError, disposeTransport } from '../../lib/error-tracking/transport'

export class BuiltinMonitorBackend implements MonitorBackend {
  readonly key = 'builtin'

  async init(_config: MonitorConfig): Promise<boolean> {
    return true
  }

  captureError(event: ErrorEventInput): void {
    enqueueError({
      ...event,
      source: 'frontend',
    })
  }

  captureMessage(message: string, level: MonitorLevel): void {
    enqueueError({
      level,
      message,
      source: 'frontend',
    })
  }

  setUser(_user: UserInfo | null): void {
    // Intentional no-op: the builtin backend has no server to report user identity to.
  }

  setTags(_tags: Record<string, string>): void {
    // Intentional no-op: the builtin backend does not support global tags.
  }

  dispose(): void {
    disposeTransport()
  }
}
