import type { ErrorEventInput, MonitorBackend, MonitorConfig, MonitorLevel, UserInfo } from '../types'
import { enqueueError } from '../../lib/error-tracking/transport'

export class TransportBackend implements MonitorBackend {
  readonly key = 'transport'

  async init(_config: MonitorConfig): Promise<boolean> {
    return true
  }

  captureError(event: ErrorEventInput, _error?: Error, _context?: Record<string, unknown>): void {
    enqueueError(event)
  }

  captureMessage(message: string, level: MonitorLevel): void {
    enqueueError({ level, message })
  }

  setUser(_user: UserInfo | null): void {}

  setTags(_tags: Record<string, string>): void {}

  dispose(): void {}
}
