import type { MonitorBackend, MonitorEvent, UserInfo } from '../types'
import { enqueueError } from '../../lib/error-tracking/transport'
import type { ErrorEventInput } from '../../lib/error-tracking/types'

export class TransportBackend implements MonitorBackend {
  captureError(event: MonitorEvent, error: Error, context?: Record<string, unknown>): void {
    enqueueError(event as unknown as ErrorEventInput)
  }

  captureMessage(message: string, level: string): void {
    enqueueError({ level, message } as unknown as ErrorEventInput)
  }

  setUser(_user: UserInfo | null): void {}

  setTags(_tags: Record<string, string>): void {}

  dispose(): void {}
}
