import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  fetchDashboardNotifications,
  fetchUnreadCount,
  markAllNotificationsRead,
  type NotificationResponse,
} from '../lib/api/notifications'
import { formatApiError } from '../lib/api/formatError'

/** How many recent notifications the bell dropdown fetches (FAR-249). */
export const DROPDOWN_LIMIT = 10

/**
 * Shared notification state for the bell + dropdown (FAR-249).
 *
 * Owns the unread badge count, the recent-notification list the dropdown
 * renders, and the `markAllRead` action. The SSE wiring (read-time category
 * suppression, reconnect backfill) stays in `NotificationBell.vue` — it
 * drives these actions rather than living here, so the store stays a plain
 * data/actions layer with no stream lifecycle.
 */
export const useNotificationsStore = defineStore('notifications', () => {
  const unreadCount = ref(0)
  const recentNotifications = ref<NotificationResponse[]>([])
  const loadingRecent = ref(false)
  const markAllReadError = ref<string | null>(null)

  /** Refresh the unread badge count. Fail-open: any error leaves the last value. */
  async function refreshUnreadCount(): Promise<void> {
    try {
      unreadCount.value = await fetchUnreadCount()
    } catch {
      unreadCount.value = 0
    }
  }

  /**
   * Load the dropdown's recent list from the severity-filtered dashboard
   * feed. `total_unread` from the same response keeps the badge in step with
   * the list in one round-trip.
   */
  async function fetchRecent(limit: number = DROPDOWN_LIMIT): Promise<void> {
    loadingRecent.value = true
    try {
      const data = await fetchDashboardNotifications(limit)
      recentNotifications.value = data.notifications ?? []
      unreadCount.value = data.total_unread
    } catch {
      recentNotifications.value = []
    } finally {
      loadingRecent.value = false
    }
  }

  /**
   * Mark every live notification in the caller's view as read, then refresh
   * both the list and the badge so the UI reflects the server's post-state.
   * Re-throws on failure (after recording a user-facing message) so the
   * caller can keep the dropdown open instead of showing a cleared state that
   * did not happen.
   */
  async function markAllRead(): Promise<void> {
    markAllReadError.value = null
    try {
      await markAllNotificationsRead()
    } catch (e: unknown) {
      markAllReadError.value = formatApiError(e)
      throw e
    }
    await Promise.all([fetchRecent(), refreshUnreadCount()])
  }

  return {
    unreadCount,
    recentNotifications,
    loadingRecent,
    markAllReadError,
    refreshUnreadCount,
    fetchRecent,
    markAllRead,
  }
})
