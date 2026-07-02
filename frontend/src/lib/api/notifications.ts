import { api } from './client'

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
  const { data, error } = await api.GET('/api/v1/notifications/in-app/dashboard')
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
  return data as unknown as DashboardNotificationResponse
}

export async function reviewLater(notificationId: string): Promise<void> {
  const { error } = await api.POST('/api/v1/notifications/in-app/{notification_id}/review-later', {
    params: { path: { notification_id: notificationId } },
  })
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
}

export async function dismissNotification(notificationId: string, dismissScope: 'self' | 'scope'): Promise<void> {
  const { error } = await api.POST('/api/v1/notifications/in-app/{notification_id}/dismiss', {
    params: { path: { notification_id: notificationId } },
    body: { dismiss_scope: dismissScope },
  })
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
}

export async function fetchNotifications(params: {
  page?: number
  page_size?: number
  level?: string
  scope?: string
  category?: string
  status?: string
} = {}): Promise<PaginatedNotificationsResponse> {
  const { data, error } = await api.GET('/api/v1/notifications/in-app', {
    params: { query: params as any },
  })
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
  return data as unknown as PaginatedNotificationsResponse
}

export async function fetchNotificationDetail(id: string): Promise<NotificationResponse> {
  const { data, error } = await api.GET('/api/v1/notifications/in-app/{notification_id}', {
    params: { path: { notification_id: id } },
  })
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
  return data as unknown as NotificationResponse
}

export async function fetchUnreadCount(): Promise<number> {
  const { data, error } = await api.GET('/api/v1/notifications/in-app/unread-count')
  if (error) throw new Error(typeof error === 'string' ? error : JSON.stringify(error))
  return (data as { count: number }).count
}
