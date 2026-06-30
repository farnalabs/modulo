import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'

describe('FeatureGate', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows content when feature is enabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': true } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: { default: 'Gated Content' },
    })

    expect(wrapper.text()).toContain('Gated Content')
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
  })

  it('shows lock overlay when feature is disabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': false } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: { default: 'Gated Content' },
    })

    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Gated Content')
  })

  it('uses custom requiredTier in tooltip', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': false } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature', requiredTier: 'team' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.find('[data-testid="lock-icon"]').attributes('title')).toBe('Available on team plan')
  })

  it('renders locked slot when feature is disabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': false } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: {
        default: 'Gated Content',
        locked: '<span data-testid="custom-locked">Upgrade required</span>',
      },
    })

    expect(wrapper.find('[data-testid="custom-locked"]').exists()).toBe(true)
  })

  it('does not render locked slot when feature is enabled', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': true } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature' },
      slots: {
        default: 'Gated Content',
        locked: '<span data-testid="custom-locked">Upgrade required</span>',
      },
    })

    expect(wrapper.find('[data-testid="custom-locked"]').exists()).toBe(false)
  })
})
