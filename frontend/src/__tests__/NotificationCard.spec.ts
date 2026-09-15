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
