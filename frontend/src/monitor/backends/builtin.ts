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

  setUser(_user: UserInfo | null): void {}

  setTags(_tags: Record<string, string>): void {}

  dispose(): void {
    disposeTransport()
  }
}
