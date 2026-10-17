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
 * Shared notification state for the bell + dropdown (FAR-249), plus the
 * shared "Dismiss all" action behind every surface that offers it (FAR-1926).
 *
 * Owns the unread badge count, the recent-notification list the dropdown
 * renders, and the `dismissAll` action. The SSE wiring (read-time category
 * suppression, reconnect backfill) stays in `NotificationBell.vue` — it
 * drives these actions rather than living here, so the store stays a plain
 * data/actions layer with no stream lifecycle.
 *
 * Deliberately holds NO dismiss-all error state: a failure raised from one
 * surface must never render as a stale alert on the others, so each surface
 * keeps its own local error (QA FAR-1926).
 */
export const useNotificationsStore = defineStore('notifications', () => {
  const unreadCount = ref(0)
  const recentNotifications = ref<NotificationResponse[]>([])
  const loadingRecent = ref(false)
  /** Set when the dropdown's recent-list fetch fails (FAR-249 QA). */
  const fetchError = ref<string | null>(null)

  /**
   * Refresh the unread badge count. Fail-open: any error leaves the last
   * value. Zeroing on a transient error would falsely clear the badge and
   * announce "0 unread" for notifications the user never saw.
   */
  async function refreshUnreadCount(): Promise<void> {
    try {
      unreadCount.value = await fetchUnreadCount()
    } catch (e: unknown) {
      // Fail-open WITH a log: KEEP the previous count. Zeroing on a
      // transient error would falsely clear the badge and announce
      // "0 unread" for notifications the user never saw.
      console.warn('[notifications] unread-count refresh failed; keeping last badge value', e)
    }
  }

  /**
   * Load the dropdown's recent list from the severity-filtered dashboard
   * feed. `total_unread` from the same response keeps the badge in step with
   * the list in one round-trip.
   *
   * On failure the previous list and badge are KEPT (fail-open) and
   * `fetchError` is set so the dropdown renders an error state — never the
   * "No notifications" empty state, which would falsely claim the feed is
   * clear. A later successful fetch clears the flag.
   */
  async function fetchRecent(limit: number = DROPDOWN_LIMIT): Promise<void> {
    loadingRecent.value = true
    fetchError.value = null
    try {
      const data = await fetchDashboardNotifications(limit)
      recentNotifications.value = data.notifications ?? []
      unreadCount.value = data.total_unread
    } catch (e: unknown) {
      fetchError.value = formatApiError(e)
    } finally {
      loadingRecent.value = false
    }
  }

  /**
   * Dismiss every live notification in the caller's view FOR THIS USER ONLY
   * (FAR-249/FAR-1926). Modulo models read-ness as a per-user dismissal: the
   * server files a `dismiss_scope='self'` row for each live notification, so
   * this is strictly "for me" — never a scope-wide dismissal for everyone.
   *
   * This is the ONE shared action path behind every "Dismiss all" control
   * (bell dropdown, dashboard panel, notifications page). After the write it
   * refreshes the dropdown's list and the bell badge so the UI reflects the
   * server's post-state.
   *
   * Returns the server's `marked` count so a surface can tell a real clear
   * (marked > 0) from a no-op re-run (marked === 0) and show truthful copy
   * either way — a zero must never be announced as "All notifications
   * dismissed".
   *
   * On failure it throws an Error carrying the FORMATTED API message
   * (`formatApiError`) rather than recording shared store state: the caller
   * stores it in its own local error ref, so one surface's failure never
   * renders as a stale alert on the others (QA FAR-1926). The list refresh is
   * skipped on failure so no surface shows a cleared state that did not
   * happen.
   */
  async function dismissAll(): Promise<number> {
    let marked: number
    try {
      marked = (await markAllNotificationsRead()).marked
    } catch (e: unknown) {
      throw new Error(formatApiError(e), { cause: e })
    }
    await Promise.all([fetchRecent(), refreshUnreadCount()])
    return marked
  }

  return {
    unreadCount,
    recentNotifications,
    loadingRecent,
    fetchError,
    refreshUnreadCount,
    fetchRecent,
    dismissAll,
  }
})
