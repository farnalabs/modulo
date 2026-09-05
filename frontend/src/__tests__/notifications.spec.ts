import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDashboardNotifications,
  reviewLater,
  dismissNotification,
  fetchNotifications,
  fetchNotificationDetail,
  fetchUnreadCount,
  type NotificationResponse,
} from '../lib/api/notifications'

const { apiGet, apiPost } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: apiGet,
    POST: apiPost,
  },
}))

const notification: NotificationResponse = {
  id: 'n-1',
  scope: 'org',
  level: 'info',
  category: 'deploy',
  title: 'Deploy finished',
  body: 'Pipeline p-1 deployed to production.',
  action_url: '/runs/r-9',
  dismiss_strategy: 'manual',
  dismissible_at_scope: true,
  created_at: '2026-09-01T00:00:00Z',
  scope_label: 'Organisation',
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fetchDashboardNotifications', () => {
  it('returns the dashboard payload', async () => {
    const payload = { notifications: [notification], total_unread: 3 }
    apiGet.mockResolvedValue({ data: payload })
    await expect(fetchDashboardNotifications()).resolves.toEqual(payload)
    expect(apiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app/dashboard')
  })

  it('propagates API errors as a thrown Error with the formatted detail', async () => {
    apiGet.mockResolvedValue({ error: { detail: 'not allowed' } })
    await expect(fetchDashboardNotifications()).rejects.toThrow('not allowed')
  })
})

describe('reviewLater', () => {
  it('POSTs review-later for the notification id', async () => {
    apiPost.mockResolvedValue({ data: undefined })
    await reviewLater('n-1')
    expect(apiPost).toHaveBeenCalledWith('/api/v1/notifications/in-app/{notification_id}/review-later', {
      params: { path: { notification_id: 'n-1' } },
    })
  })

  it('propagates API errors', async () => {
    apiPost.mockResolvedValue({ error: { detail: 'gone' } })
    await expect(reviewLater('n-1')).rejects.toThrow('gone')
  })
})

describe('dismissNotification', () => {
  it('POSTs the dismiss scope in the body', async () => {
    apiPost.mockResolvedValue({ data: undefined })
    await dismissNotification('n-2', 'scope')
    expect(apiPost).toHaveBeenCalledWith('/api/v1/notifications/in-app/{notification_id}/dismiss', {
      params: { path: { notification_id: 'n-2' } },
      body: { dismiss_scope: 'scope' },
    })
  })

  it('propagates API errors', async () => {
    apiPost.mockResolvedValue({ error: { detail: 'cannot dismiss at org scope' } })
    await expect(dismissNotification('n-2', 'self')).rejects.toThrow('cannot dismiss at org scope')
  })
})

describe('fetchNotifications', () => {
  it('passes query params through', async () => {
    const page = { items: [notification], total: 1, page: 2, page_size: 25 }
    apiGet.mockResolvedValue({ data: page })
    const params = { page: 2, page_size: 25, level: 'error', scope: 'org', category: 'deploy', status: 'unread' }
    await expect(fetchNotifications(params)).resolves.toEqual(page)
    expect(apiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app', { params: { query: params } })
  })

  it('sends an empty query object by default', async () => {
    apiGet.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 50 })
    await fetchNotifications()
    expect(apiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app', { params: { query: {} } })
  })

  it('propagates API errors', async () => {
    apiGet.mockResolvedValue({ error: { detail: 'boom' } })
    await expect(fetchNotifications()).rejects.toThrow('boom')
  })
})

describe('fetchNotificationDetail', () => {
  it('returns the notification detail', async () => {
    apiGet.mockResolvedValue({ data: notification })
    await expect(fetchNotificationDetail('n-1')).resolves.toEqual(notification)
    expect(apiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app/{notification_id}', {
      params: { path: { notification_id: 'n-1' } },
    })
  })

  it('propagates API errors', async () => {
    apiGet.mockResolvedValue({ error: { detail: 'missing' } })
    await expect(fetchNotificationDetail('n-x')).rejects.toThrow('missing')
  })
})

describe('fetchUnreadCount', () => {
  it('returns the count field', async () => {
    apiGet.mockResolvedValue({ data: { count: 7 } })
    await expect(fetchUnreadCount()).resolves.toBe(7)
    expect(apiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app/unread-count')
  })

  it('falls back to 0 when the count field is missing', async () => {
    apiGet.mockResolvedValue({ data: {} })
    await expect(fetchUnreadCount()).resolves.toBe(0)
  })

  it('propagates API errors', async () => {
    apiGet.mockResolvedValue({ error: { detail: 'unauthorised' } })
    await expect(fetchUnreadCount()).rejects.toThrow('unauthorised')
  })
})
