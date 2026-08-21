import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import LockIcon from '../components/LockIcon.vue'

describe('LockIcon', () => {
  it('renders when locked is true', () => {
    const wrapper = mount(LockIcon, { props: { locked: true } })
    expect(wrapper.find('[data-testid="lock-icon"]').exists()).toBe(true)
  })

  it('does not render when locked is false', () => {
    const wrapper = mount(LockIcon, { props: { locked: false } })
    expect(wrapper.find('[data-testid="lock-icon"]').exists()).toBe(false)
  })

  it('uses default tooltip when not provided', () => {
    const wrapper = mount(LockIcon, { props: { locked: true } })
    expect(wrapper.attributes('title')).toBe('Locked')
  })

  it('uses custom tooltip when provided', () => {
    const wrapper = mount(LockIcon, { props: { locked: true, tooltip: 'Available on team plan' } })
    expect(wrapper.attributes('title')).toBe('Available on team plan')
  })

  it('renders slot content', () => {
    const wrapper = mount(LockIcon, {
      props: { locked: true },
      slots: { default: 'Pro' },
    })
    expect(wrapper.text()).toContain('Pro')
  })
})
