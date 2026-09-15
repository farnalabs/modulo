import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import NotificationCard from '../components/NotificationCard.vue'

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
})
