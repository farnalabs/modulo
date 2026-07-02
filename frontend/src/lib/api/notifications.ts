import { api } from './client'

function throwOnError<T>(result: { data?: T; error?: unknown }): T {
  if (result.error) throw new Error(typeof result.error === 'string' ? result.error : JSON.stringify(result.error))
  return result.data as T
}

export interface NotificationResponse {
  id: string
  scope: string
  level: string
  category: string
  title: string
  body: string
  action_url: string | null
  dismiss_strategy: string
  dismissible_at_scope: boolean
  created_at: string
  scope_label: string
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
  return (throwOnError(await api.GET('/api/v1/notifications/in-app/unread-count')) as { count: number }).count
}
