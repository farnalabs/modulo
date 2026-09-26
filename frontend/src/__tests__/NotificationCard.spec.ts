import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import NotificationCard from '../components/NotificationCard.vue'
import { dismissNotification } from '../lib/api/notifications'

vi.mock('../lib/api/notifications', () => ({
  dismissNotification: vi.fn(),
}))

// Stub DismissDialog so the card can be tested in isolation from the popover.
vi.mock('../components/DismissDialog.vue', () => ({
  default: { name: 'DismissDialog', template: '<div />' },
}))

function makeNotification(overrides: Record<string, unknown> = {}) {
  return {
    id: 'n-1',
    title: 'Test notification',
    level: 'info',
    category: 'pipeline_run',
    scope: 'org',
    body: '',
    action_url: 'https://example.com/run/1',
    dismiss_strategy: 'user_only',
    dismissible_at_scope: false,
    created_at: '2025-06-01T10:00:00Z',
    scope_label: 'Organization',
    // FAR-1234: the API always resolves point-in-time run metadata; no linked
    // run by default (tests opt in via `overrides`).
    run_id: null,
    run_status: null,
    run_terminal: false,
    run_cancel_reason: null,
    ...overrides,
  }
}

describe('NotificationCard', () => {
  it('shows a neutral "awaiting review" affordance for hitl.awaiting notifications (never claims the gate lapsed)', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification({ category: 'hitl.awaiting' }) },
    })
    expect(wrapper.text()).toContain('Awaiting your review')
    // The backend never retracts hitl.awaiting on gate resume, so we must not
    // assert the gate has lapsed for every such notification.
    expect(wrapper.text()).not.toContain('lapsed')
  })

  it('does not show the HITL affordance for non-HITL notifications', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification({ category: 'pipeline_run' }) },
    })
    expect(wrapper.text()).not.toContain('Awaiting your review')
  })

  it('labels the action link "View run" for HITL notifications and "View" otherwise', () => {
    const hitl = mount(NotificationCard, {
      props: { notification: makeNotification({ category: 'hitl.awaiting' }) },
    })
    expect(hitl.find('a').text()).toBe('View run')

    const other = mount(NotificationCard, {
      props: { notification: makeNotification({ category: 'pipeline_run' }) },
    })
    expect(other.find('a').text()).toBe('View')
  })

  // FAR-1234 — point-in-time run metadata ---------------------------------

  it('renders the linked run state and cancel reason resolved at read time', () => {
    const wrapper = mount(NotificationCard, {
      props: {
        notification: makeNotification({
          category: 'hitl.awaiting',
          action_url: '/runs/r-1',
          run_id: 'r-1',
          run_status: 'cancelled',
          run_terminal: true,
          run_cancel_reason: 'user_requested',
        }),
      },
    })

    const state = wrapper.get('[data-testid="notification-run-state"]')
    expect(state.text()).toContain('cancelled')
    expect(state.text()).toContain('Cancelled by an operator.')
    // Dynamic status must be announced, not silently injected (WCAG 4.1.3).
    expect(state.attributes('role')).toBe('status')
    expect(state.attributes('aria-live')).toBe('polite')
  })

  it('states that the reason was not recorded when cancel_reason is null', () => {
    const wrapper = mount(NotificationCard, {
      props: {
        notification: makeNotification({
          action_url: '/runs/r-1',
          run_id: 'r-1',
          run_status: 'cancelled',
          run_terminal: true,
          run_cancel_reason: null,
        }),
      },
    })

    expect(wrapper.get('[data-testid="notification-run-state"]').text()).toContain('Reason not recorded.')
  })

  it('hides the run-state line when the notification is not run-linked', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })
    expect(wrapper.find('[data-testid="notification-run-state"]').exists()).toBe(false)
  })

  it('humanises an unmapped run status rather than rendering the raw i18n key', () => {
    const wrapper = mount(NotificationCard, {
      props: {
        notification: makeNotification({
          run_id: 'r-1',
          run_status: 'quarantined_by_guard',
          run_terminal: false,
        }),
      },
    })

    // The locale map is closed, but the backend can add a status before the
    // client ships its translation: show readable text, never `run_statuses.…`.
    const state = wrapper.get('[data-testid="notification-run-state"]')
    expect(state.text()).toContain('quarantined by guard')
    expect(state.text()).not.toContain('run_statuses')
  })

  it('resolves an empty status word when the notification is not run-linked', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification({ run_status: null }) },
    })

    // `hasRunState` hides the run-state line, but runStateLabel is still a
    // public computed and must stay safe (and empty) with no linked run.
    const vm = wrapper.vm as unknown as { runStateLabel: string }
    expect(vm.runStateLabel).toBe('')
  })

  // FAR-1234 — demote stale HITL requests ---------------------------------

  it('demotes a hitl.awaiting notification whose run is terminal (never presented as live work)', () => {
    const wrapper = mount(NotificationCard, {
      props: {
        notification: makeNotification({
          category: 'hitl.awaiting',
          action_url: '/runs/r-1',
          run_id: 'r-1',
          run_status: 'cancelled',
          run_terminal: true,
        }),
      },
    })

    expect(wrapper.text()).not.toContain('Awaiting your review')
    expect(wrapper.text()).toContain('No review needed')
  })

  it('keeps the neutral "awaiting your review" wording while the run is still reviewable', () => {
    const wrapper = mount(NotificationCard, {
      props: {
        notification: makeNotification({
          category: 'hitl.awaiting',
          action_url: '/runs/r-1',
          run_id: 'r-1',
          run_status: 'awaiting_human',
          run_terminal: false,
        }),
      },
    })

    expect(wrapper.text()).toContain('Awaiting your review')
    expect(wrapper.text()).not.toContain('No review needed')
  })

  it('keeps the neutral wording when the linked run cannot be resolved', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification({ category: 'hitl.awaiting' }) },
    })

    expect(wrapper.text()).toContain('Awaiting your review')
  })

  // FAR-1234 — the clear affordances must be reachable without hover ------

  it('renders the dismiss and review-later controls outside any hover-only wrapper', () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })

    const actions = wrapper.get('.notification-actions')
    // The controls used to sit behind `hidden group-hover:flex`; `display:none`
    // drops them from the tab order, so a keyboard/touch user could not clear
    // a notification at all. Visibility is now driven by the component's scoped
    // rules (hover / focus-within / no-hover pointer) instead.
    expect(actions.classes()).not.toContain('hidden')
    expect(actions.classes()).not.toContain('group-hover:flex')

    const buttons = actions.findAll('button')
    expect(buttons.map((b) => b.text())).toEqual(['Review Later', 'Dismiss this notification'])
    for (const button of buttons) {
      expect(button.attributes('aria-label')).toBeTruthy()
    }
  })

  it('emits review-later when the review-later button is clicked', async () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })
    const reviewLaterBtn = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Review Later')
    expect(reviewLaterBtn).toBeTruthy()
    await reviewLaterBtn!.trigger('click')
    expect(wrapper.emitted('review-later')).toBeTruthy()
    expect(wrapper.emitted('review-later')![0]).toEqual(['n-1'])
  })

  it('opens the dismiss dialog when the dismiss button is clicked', async () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })
    const dismissBtn = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Dismiss this notification')
    expect(dismissBtn).toBeTruthy()
    await dismissBtn!.trigger('click')
    // Dialog visibility toggled open
    expect(wrapper.findComponent({ name: 'DismissDialog' }).exists()).toBe(true)
  })

  it('emits dismissed after a successful dismiss', async () => {
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })
    const vm = wrapper.vm as unknown as { onDismiss: (s: 'self' | 'scope') => Promise<void> }
    await vm.onDismiss('self')
    expect(vi.mocked(dismissNotification)).toHaveBeenCalledWith('n-1', 'self')
    expect(wrapper.emitted('dismissed')).toBeTruthy()
    expect(wrapper.emitted('dismissed')![0]).toEqual(['n-1'])
  })

  it('surfaces a dismiss error when the API fails', async () => {
    vi.mocked(dismissNotification).mockRejectedValue(new Error('dismiss failed'))
    const wrapper = mount(NotificationCard, {
      props: { notification: makeNotification() },
    })
    const vm = wrapper.vm as unknown as { onDismiss: (s: 'self' | 'scope') => Promise<void> }
    await vm.onDismiss('self')
    expect(wrapper.text()).toContain('dismiss failed')
  })
})
