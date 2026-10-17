import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const h = vi.hoisted(() => ({
  mockFetchUnreadCount: vi.fn(),
  mockApiGet: vi.fn(),
  mockSubscribe: vi.fn((_type: string, _cb: (e: unknown) => void) => vi.fn()),
  mockOnReconnect: vi.fn((_cb: () => void) => vi.fn()),
  mockReconnect: vi.fn(),
}))

vi.mock('../lib/api/notifications', () => ({
  fetchUnreadCount: h.mockFetchUnreadCount,
  fetchDashboardNotifications: vi.fn().mockResolvedValue({ notifications: [], total_unread: 0 }),
  markAllNotificationsRead: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: h.mockApiGet },
}))

vi.mock('../lib/api/formatError', () => ({
  throwOnError: (result: { data?: unknown; error?: unknown }) => {
    if (result.error) throw new Error('prefs failed')
    return result.data
  },
  // The unmocked notifications store imports formatApiError from this module
  // and calls it in the fetchRecent/markAllRead error paths. The mock must
  // provide it or those paths crash with "formatApiError is not a function".
  formatApiError: (err: unknown): string => {
    if (err instanceof Error) return err.message
    if (typeof err === 'string') return err
    return 'Unknown error'
  },
}))

// Reactive banner flag: a plain { value } object would NOT trigger a re-render
// (Vue only tracks refs/computed), so the mock owns a real ref and the test
// flips it through the exported __banner / __feedback handles (FAR-1842 adds
// the reconnect feedback tri-state on top of the banner flag).
vi.mock('../composables/useEventStream', async () => {
  const { ref } = await import('vue')
  const banner = ref(false)
  const feedback = ref<'idle' | 'attempting' | 'failed'>('idle')
  return {
    __banner: banner,
    __feedback: feedback,
    eventBus: {
      get reconnectRequired() {
        return banner.value
      },
      get reconnectFeedback() {
        return feedback.value
      },
      get connected() {
        return false
      },
      get state() {
        return 'idle'
      },
      subscribe: (type: string, cb: (e: unknown) => void) => h.mockSubscribe(type, cb),
      onReconnect: (cb: () => void) => h.mockOnReconnect(cb),
      reconnect: () => h.mockReconnect(),
      unsubscribe: vi.fn(),
    },
  }
})

import NotificationBell from '../components/NotificationBell.vue'
import * as eventStreamModule from '../composables/useEventStream'

const bannerFlag = (eventStreamModule as unknown as { __banner: { value: boolean } }).__banner
const feedbackFlag = (
  eventStreamModule as unknown as { __feedback: { value: 'idle' | 'attempting' | 'failed' } }
).__feedback

function notificationEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: 'notification',
    id: 'n-1',
    action: 'created',
    version: 1,
    org_id: 'org-1',
    notification_id: 'n-1',
    category: 'run_failed',
    created_at: '2026-09-23T12:00:00+00:00',
    ...overrides,
  }
}

async function mountBell(props: Record<string, unknown> = {}) {
  const wrapper = mount(NotificationBell, {
    props,
    global: {
      plugins: [createPinia()],
      // FAR-1926: the dropdown teleports to <body>; stub the teleport so it
      // renders in place and wrapper.find keeps working.
      stubs: { teleport: true },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('NotificationBell (FAR-250)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    bannerFlag.value = false
    feedbackFlag.value = 'idle'
    h.mockFetchUnreadCount.mockResolvedValue(3)
    h.mockApiGet.mockResolvedValue({
      data: {
        dashboard_level: 'warning',
        notification_opt_outs: { run_failed: true, hitl_overdue: false },
      },
      error: undefined,
    })
  })

  it('fetches notification preferences BEFORE subscribing to the stream', async () => {
    await mountBell()

    expect(h.mockApiGet).toHaveBeenCalledWith('/api/v1/notifications/in-app/preferences')
    expect(h.mockSubscribe).toHaveBeenCalledTimes(1)
    // invocationCallOrder proves prefs loaded before the first SSE subscription
    // (read-time suppression is active from the first event).
    const prefsOrder = h.mockApiGet.mock.invocationCallOrder[0]
    const subscribeOrder = h.mockSubscribe.mock.invocationCallOrder[0]
    expect(prefsOrder).toBeLessThan(subscribeOrder)
  })

  it('subscribes to the notification resource type (retains the shared stream)', async () => {
    await mountBell()
    expect(h.mockSubscribe.mock.calls[0][0]).toBe('notification')
    expect(h.mockSubscribe.mock.calls[0][1]).toBeTypeOf('function')
  })

  it('keeps the desktop hit-box by default and enlarges it to 44px for the touch size', async () => {
    const desktop = await mountBell()
    const desktopButton = desktop.find('[data-testid="notification-bell-button"]')
    expect(desktopButton.classes()).toContain('p-2')
    expect(desktopButton.classes()).not.toContain('h-11')

    const touch = await mountBell({ size: 'touch' })
    const touchButton = touch.find('[data-testid="notification-bell-button"]')
    expect(touchButton.classes()).toContain('h-11')
    expect(touchButton.classes()).toContain('w-11')
    // The visible glyph is unchanged — only the hit box is enlarged.
    expect(touchButton.classes()).not.toContain('p-2')
    // The size variant must not regress the WCAG 2.5.3 accessible name: the
    // touch control still uses bellAriaLabel (count 3 from the default fetch).
    expect(touchButton.attributes('aria-label')).toBe('Notifications, 3 unread')
  })

  it('renders the unread badge from the initial fetch', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(7)
    const wrapper = await mountBell()
    const badge = wrapper.find('[data-testid="notification-unread-badge"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('7')
  })

  it('caps the badge at "99+" when the unread count exceeds 99', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(150)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('99+')
  })

  // -------------------------------------------------------------------------
  // WCAG 2.5.3 — the visible badge text must be part of the accessible name.
  // The badge is aria-hidden (kept out of the a11y tree) but still VISIBLE, so
  // axe's label-content-name-mismatch rule requires the button's accessible
  // name to contain the visible count.
  // -------------------------------------------------------------------------

  it('includes the visible unread count in the bell button accessible name', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(7)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-bell-button"]').attributes('aria-label')).toBe(
      'Notifications, 7 unread',
    )
  })

  it('keeps the plain accessible name when there is no unread badge', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(0)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-bell-button"]').attributes('aria-label')).toBe(
      'Notifications',
    )
  })

  it('keeps the accessible-name count in step with the capped "99+" badge', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(150)
    const wrapper = await mountBell()
    // The badge and the accessible name must carry the SAME visible text, or
    // label-content-name-mismatch still fails for counts above the cap.
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('99+')
    expect(wrapper.find('[data-testid="notification-bell-button"]').attributes('aria-label')).toBe(
      'Notifications, 99+ unread',
    )
  })

  it('refetches the unread count when a non-opted-out notification event arrives', async () => {
    const wrapper = await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()
    h.mockFetchUnreadCount.mockResolvedValue(4)

    handler(notificationEvent({ category: 'hitl_overdue' }))
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('4')
  })

  it('keeps the last badge value when the unread-count refresh fails', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(5)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('5')

    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockRejectedValue(new Error('transient'))
    handler(notificationEvent({ category: 'hitl_overdue' }))
    await flushPromises()

    // Fail-open: a transient error must not zero the badge (which used to
    // hide it entirely and announce "0 unread" for notifications the user
    // never saw).
    expect(wrapper.find('[data-testid="notification-unread-badge"]').text()).toBe('5')
    expect(wrapper.find('[data-testid="notification-unread-live"]').text()).toContain('5')
  })

  it('applies read-time suppression: no refetch for an opted-out category', async () => {
    await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: 'run_failed' })) // opted out in the prefs mock
    await flushPromises()

    expect(h.mockFetchUnreadCount).not.toHaveBeenCalled()
  })

  it('tolerates events without a category field (generic envelope) by refetching', async () => {
    await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: undefined }))
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
  })

  it('registers an onReconnect backfill that refetches the unread count', async () => {
    await mountBell()
    expect(h.mockOnReconnect).toHaveBeenCalledTimes(1)
    const backfill = h.mockOnReconnect.mock.calls[0][0] as () => void
    h.mockFetchUnreadCount.mockClear()

    backfill()
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
  })

  it('fails open when the preferences fetch fails (empty opt-out set)', async () => {
    h.mockApiGet.mockResolvedValue({ data: undefined, error: { detail: 'boom' } })
    const wrapper = await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: 'run_failed' }))
    await flushPromises()

    // Suppression set is empty -> refetch proceeds (server still filters reads).
    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="notification-unread-badge"]').exists()).toBe(true)
  })

  it('treats a missing notification_opt_outs map as no opt-outs (nullish fallback)', async () => {
    h.mockApiGet.mockResolvedValue({
      data: { dashboard_level: 'warning' },
      error: undefined,
    })
    await mountBell()
    const handler = h.mockSubscribe.mock.calls[0][1] as (e: Record<string, unknown>) => void
    h.mockFetchUnreadCount.mockClear()

    handler(notificationEvent({ category: 'run_failed' }))
    await flushPromises()

    expect(h.mockFetchUnreadCount).toHaveBeenCalledTimes(1)
  })

  it('shows the reconnect banner when the stream stopped on a 4xx, and restarts on click', async () => {
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(false)

    bannerFlag.value = true
    await nextTick()

    const banner = wrapper.find('[data-testid="sse-reconnect-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')

    await wrapper.find('[data-testid="sse-reconnect-button"]').trigger('click')
    expect(h.mockReconnect).toHaveBeenCalledTimes(1)

    bannerFlag.value = false
    await nextTick()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(false)
  })

  it('keeps the banner visible with attempting feedback and a disabled button while a manual restart is in flight (FAR-1842)', async () => {
    const wrapper = await mountBell()
    // No terminal stop, but a manual restart is in flight: the banner stays
    // so the click is visibly acknowledged (it used to vanish silently).
    feedbackFlag.value = 'attempting'
    await nextTick()

    const banner = wrapper.find('[data-testid="sse-reconnect-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Reconnecting')
    expect(wrapper.find('[data-testid="sse-reconnect-button"]').attributes('disabled')).toBeDefined()
  })

  it('shows explicit failure feedback when a manual restart cannot recover (FAR-1842)', async () => {
    const wrapper = await mountBell()
    bannerFlag.value = true
    feedbackFlag.value = 'failed'
    await nextTick()

    const banner = wrapper.find('[data-testid="sse-reconnect-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Reconnect failed. Sign in again')
    // The copy instructs a fresh sign-in, so the button must not contradict
    // it by inviting an immediate retry: without a fresh token a click would
    // only re-run the refresh+retry that just failed. The auth-change
    // re-arm restarts the stream when the fresh sign-in lands (FAR-1861).
    expect(wrapper.find('[data-testid="sse-reconnect-button"]').attributes('disabled')).toBeDefined()
  })

  it('keeps the reconnect button enabled on the plain auth-stop banner (FAR-1861)', async () => {
    // The base banner (no manual attempt yet, feedback idle) still offers the
    // manual restart — only the terminal 'failed' feedback disables it.
    const wrapper = await mountBell()
    bannerFlag.value = true
    feedbackFlag.value = 'idle'
    await nextTick()

    const banner = wrapper.find('[data-testid="sse-reconnect-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Live updates stopped')
    expect(wrapper.find('[data-testid="sse-reconnect-button"]').attributes('disabled')).toBeUndefined()
  })

  it('clears the banner when the stream state leaves auth_failed (FAR-1842)', async () => {
    // State-transition coverage: banner shown while failed, then cleared —
    // driven by the reactive flags, matching eventBus leaving auth_failed
    // (manual recovery or the auth-change re-arm).
    const wrapper = await mountBell()
    bannerFlag.value = true
    feedbackFlag.value = 'failed'
    await nextTick()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(true)

    bannerFlag.value = false
    feedbackFlag.value = 'idle'
    await nextTick()
    expect(wrapper.find('[data-testid="sse-reconnect-banner"]').exists()).toBe(false)
  })

  it('does not render a badge when the unread count is zero', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(0)
    const wrapper = await mountBell()
    expect(wrapper.find('[data-testid="notification-unread-badge"]').exists()).toBe(false)
  })

  // -------------------------------------------------------------------------
  // FAR-249 — dropdown toggle
  // -------------------------------------------------------------------------

  it('toggles the dropdown from the bell button with aria-expanded in step', async () => {
    const wrapper = await mountBell()
    const button = wrapper.find('[data-testid="notification-bell-button"]')

    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(false)
    expect(button.attributes('aria-expanded')).toBe('false')
    expect(button.attributes('aria-controls')).toBe('notification-dropdown')

    await button.trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)
    expect(button.attributes('aria-expanded')).toBe('true')

    await button.trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(false)
    expect(button.attributes('aria-expanded')).toBe('false')
  })

  it('exposes an aria-live region announcing the unread count', async () => {
    h.mockFetchUnreadCount.mockResolvedValue(3)
    const wrapper = await mountBell()
    const live = wrapper.find('[data-testid="notification-unread-live"]')
    expect(live.exists()).toBe(true)
    // <output> carries the implicit ARIA status role natively (SonarCloud
    // Web:S6819) — assert the native element rather than a redundant role.
    expect(live.element.tagName).toBe('OUTPUT')
    expect(live.attributes('aria-live')).toBe('polite')
    // The visual badge is decorative — the live region carries the announcement.
    expect(wrapper.find('[data-testid="notification-unread-badge"]').attributes('aria-hidden')).toBe('true')
  })

  it('closes the dropdown on Escape', async () => {
    const wrapper = await mountBell()
    const button = wrapper.find('[data-testid="notification-bell-button"]')
    await button.trigger('click')

    await wrapper.find('[data-testid="notification-dropdown"]').trigger('keydown', { key: 'Escape' })
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(false)
  })

  it('closes the dropdown when a click lands outside the bell', async () => {
    const wrapper = await mountBell()
    await wrapper.find('[data-testid="notification-bell-button"]').trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)

    const outside = document.createElement('div')
    document.body.appendChild(outside)
    outside.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(false)
    outside.remove()
  })

  it('keeps the dropdown open when the click is inside the bell root', async () => {
    const wrapper = await mountBell()
    const button = wrapper.find('[data-testid="notification-bell-button"]')
    await button.trigger('click')

    // A click on the button itself must not immediately re-close via the
    // document-level outside-click handler (toggle wins).
    await button.trigger('click')
    await button.trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)
  })

  it('ignores an outside-click whose target is inside the bell root', async () => {
    const addSpy = vi.spyOn(document, 'addEventListener')
    const wrapper = await mountBell()
    await wrapper.find('[data-testid="notification-bell-button"]').trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)

    const clickCall = addSpy.mock.calls.find(([type]) => type === 'click')
    expect(clickCall).toBeDefined()
    const handler = clickCall![1] as unknown as (e: MouseEvent) => void

    // A click whose target the root contains is NOT an outside click; the
    // handler must leave the dropdown open.
    handler({ target: wrapper.element } as unknown as MouseEvent)
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)
    addSpy.mockRestore()
  })

  it('keeps the dropdown open when the click lands inside the (body-teleported) dropdown', async () => {
    // FAR-1926: the panel teleports to <body>, so it is NOT inside the bell
    // root — the outside-click handler must still treat its own subtree as
    // "inside" (matched via #notification-dropdown), or the panel would close
    // on every internal click in the real app.
    const addSpy = vi.spyOn(document, 'addEventListener')
    const wrapper = await mountBell()
    await wrapper.find('[data-testid="notification-bell-button"]').trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)

    const clickCall = addSpy.mock.calls.find(([type]) => type === 'click')
    expect(clickCall).toBeDefined()
    const handler = clickCall![1] as unknown as (e: MouseEvent) => void

    // A detached element that is the teleported dialog itself (or inside it):
    // closest('#notification-dropdown') matches, so the dropdown stays open.
    const teleportedDialog = document.createElement('div')
    teleportedDialog.id = 'notification-dropdown'
    handler({ target: teleportedDialog } as unknown as MouseEvent)
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)
    addSpy.mockRestore()
  })

  it('ignores an outside-click event that carries no target (defensive)', async () => {
    const addSpy = vi.spyOn(document, 'addEventListener')
    const wrapper = await mountBell()
    await wrapper.find('[data-testid="notification-bell-button"]').trigger('click')
    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)

    const clickCall = addSpy.mock.calls.find(([type]) => type === 'click')
    const handler = clickCall![1] as unknown as (e: MouseEvent) => void

    // A synthetic event with no target must bail out (never throw, never be
    // treated as an outside click that closes the panel).
    expect(() => handler({ target: null } as unknown as MouseEvent)).not.toThrow()
    await flushPromises()

    expect(wrapper.find('[data-testid="notification-dropdown"]').exists()).toBe(true)
    addSpy.mockRestore()
  })

  it('removes the document click listener when unmounted', async () => {
    const removeSpy = vi.spyOn(document, 'removeEventListener')
    const wrapper = await mountBell()
    wrapper.unmount()
    expect(removeSpy).toHaveBeenCalledWith('click', expect.any(Function))
    removeSpy.mockRestore()
  })
})
