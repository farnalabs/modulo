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
    expect(wrapper.find('[data-testid="feature-gate-lock"]').text()).toContain('Team Feature')
  })

  it('uses custom requiredTier in tooltip', () => {
    const store = usePlanStore()
    store.$patch({ features: { 'test-feature': false } })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'test-feature', requiredTier: 'team' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.find('[data-testid="lock-icon"]').attributes('title')).toBe(
      'Available on higher plan tier — Team — /settings/license',
    )
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

  // ── FAR-1283: tier-accurate copy for a COMMUNITY-tier flag ────────────────
  //
  // mcp_server is a community-tier flag. With it off the old banner called it a
  // "Team Feature" and told the operator to buy a higher tier — both false, and
  // the "View Plans" link pointed at a purchase that cannot change the answer.
  // These tests fail on the pre-FAR-1283 component.

  it('never calls a disabled community-tier feature a team feature', () => {
    const store = usePlanStore()
    // currentTier '' leaves the gate disabled (an unranked tier), which is the
    // state a community org is in when mcp_server is off despite its tier.
    store.$patch({ features: { mcp_server: false }, currentTier: '' })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server', requiredTier: 'community' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.exists()).toBe(true)
    const text = lock.text()
    expect(text).toContain('Feature not available')
    expect(text).not.toContain('Team Feature')
    expect(text).not.toContain('Available on higher plan tier')
    expect(text).toContain('turned off for your organisation')
  })

  it('omits the pricing link for a disabled community-tier feature', () => {
    const store = usePlanStore()
    store.$patch({ features: { mcp_server: false }, currentTier: '' })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server', requiredTier: 'community' },
      slots: { default: 'Gated Content' },
    })

    expect(wrapper.find('[data-testid="feature-gate-lock"] a').exists()).toBe(false)
  })

  it('keeps the upgrade copy and pricing link for a disabled team-tier feature', () => {
    const store = usePlanStore()
    store.$patch({ features: { audit_viewer: false }, currentTier: 'community' })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'audit_viewer', requiredTier: 'team' },
      slots: { default: 'Gated Content' },
    })

    const lock = wrapper.find('[data-testid="feature-gate-lock"]')
    expect(lock.text()).toContain('Team Feature')
    expect(lock.find('a').exists()).toBe(true)
  })

  it('applies the community copy in the show-disabled banner too', () => {
    const store = usePlanStore()
    store.$patch({ features: { mcp_server: false }, currentTier: '' })

    const wrapper = mount(FeatureGate, {
      props: { featureName: 'mcp_server', requiredTier: 'community', showDisabled: true },
      slots: { default: 'Gated Content' },
    })

    const banner = wrapper.find('[data-testid="feature-gate-disabled"]')
    expect(banner.find('[data-testid="feature-gate-title"]').text()).toBe('Feature not available')
    expect(banner.text()).not.toContain('Team Feature')
    expect(banner.find('a').exists()).toBe(false)
  })
})
