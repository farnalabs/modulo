import { api } from './client'
import { throwOnError } from './formatError'

export interface NotificationResponse {
  id: string
  scope: string
  level: string
  category: string
  title: string
  body: string
  action_url?: string | null
  dismiss_strategy: string
  dismissible_at_scope: boolean
  created_at: string
  scope_label: string
  /**
   * FAR-1234 — point-in-time state of the linked run, resolved by the server
   * at READ time (the notification row itself is unchanged). Null when the
   * notification is not run-linked or the run could not be resolved.
   * Field names/types mirror the generated OpenAPI types exactly
   * (`pnpm run generate:api`).
   */
  run_id?: string | null
  run_status?: string | null
  run_terminal: boolean
  run_cancel_reason?: string | null
}

export interface DashboardNotificationResponse {
  notifications: NotificationResponse[]
  total_unread: number
}

export interface PaginatedNotificationsResponse {
  items: NotificationResponse[]
  total: number
  page: number
  page_size: number
}

export async function fetchDashboardNotifications(): Promise<DashboardNotificationResponse> {
  return throwOnError(await api.GET('/api/v1/notifications/in-app/dashboard'))
}

export async function reviewLater(notificationId: string): Promise<void> {
  throwOnError(await api.POST('/api/v1/notifications/in-app/{notification_id}/review-later', {
    params: { path: { notification_id: notificationId } },
  }))
}

export async function dismissNotification(notificationId: string, dismissScope: 'self' | 'scope'): Promise<void> {
  throwOnError(await api.POST('/api/v1/notifications/in-app/{notification_id}/dismiss', {
    params: { path: { notification_id: notificationId } },
    body: { dismiss_scope: dismissScope },
  }))
}

export async function fetchNotifications(params: {
  page?: number
  page_size?: number
  level?: string
  scope?: string
  category?: string
  status?: string
} = {}): Promise<PaginatedNotificationsResponse> {
  return throwOnError(await api.GET('/api/v1/notifications/in-app', {
    params: { query: params },
  }))
}

export async function fetchNotificationDetail(id: string): Promise<NotificationResponse> {
  return throwOnError(await api.GET('/api/v1/notifications/in-app/{notification_id}', {
    params: { path: { notification_id: id } },
  }))
}

export async function fetchUnreadCount(): Promise<number> {
  const data = throwOnError(await api.GET('/api/v1/notifications/in-app/unread-count')) as { count?: number }
  return data.count ?? 0
}
