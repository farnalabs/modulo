import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { runnerTierForProvider, runnerTierLabelKey, runnerTierLabelKeyForProvider } from '../../lib/runnerTiers'
import { createI18n } from 'vue-i18n'
import enUS from '../../locales/en-US.js'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

// Harness renders the tier badge the same way EnvironmentProfileList does,
// so the unit suite owns the badge rendering (ADR 029 D3).
import { defineComponent, h } from 'vue'

const BadgeHarness = defineComponent({
  props: { providerType: { type: String, required: true } },
  setup(props) {
    const tier = runnerTierForProvider(props.providerType)
    return () =>
      h(
        'span',
        { 'data-testid': 'envprofile-tier-badge' },
        tier ? i18n.global.t(runnerTierLabelKey(tier)) : props.providerType
      )
  },
})

describe('runnerTiers', () => {
  it('maps runner_docker to the Bundled Runner (Docker) tier', () => {
    expect(runnerTierForProvider('runner_docker')).toBe('bundled_docker')
  })

  it('maps legacy local_docker to the Bundled Runner (Docker) tier', () => {
    expect(runnerTierForProvider('local_docker')).toBe('bundled_docker')
  })

  it('maps e2b to the External Runner (E2B) tier', () => {
    expect(runnerTierForProvider('e2b')).toBe('external_e2b')
  })

  it('maps local to the Local tier', () => {
    expect(runnerTierForProvider('local')).toBe('local')
  })

  it('returns null for unknown provider types', () => {
    expect(runnerTierForProvider('warp_drive')).toBeNull()
    expect(runnerTierForProvider(null)).toBeNull()
    expect(runnerTierForProvider(undefined)).toBeNull()
  })

  it('builds locale keys inside the components.RunnerTier namespace', () => {
    expect(runnerTierLabelKey('bundled_docker')).toBe('components.RunnerTier.bundled_docker')
    expect(runnerTierLabelKey('external_e2b')).toBe('components.RunnerTier.external_e2b')
    expect(runnerTierLabelKey('local')).toBe('components.RunnerTier.local')
  })

  it('resolves every tier label key to a non-empty locale message', () => {
    for (const tier of ['bundled_docker', 'external_e2b', 'local'] as const) {
      const label = i18n.global.t(runnerTierLabelKey(tier))
      expect(label.length).toBeGreaterThan(0)
      expect(label).not.toContain('components.RunnerTier')
    }
  })

  it('resolves label keys straight from a provider type', () => {
    expect(runnerTierLabelKeyForProvider('e2b')).toBe('components.RunnerTier.external_e2b')
    expect(runnerTierLabelKeyForProvider('nope')).toBeNull()
  })

  it('renders the Bundled Runner (Docker) badge for a docker profile', () => {
    const wrapper = mount(BadgeHarness, { props: { providerType: 'runner_docker' }, global: { plugins: [i18n] } })
    expect(wrapper.text()).toBe('Bundled Runner (Docker)')
  })

  it('renders the External Runner (E2B) badge for an e2b profile', () => {
    const wrapper = mount(BadgeHarness, { props: { providerType: 'e2b' }, global: { plugins: [i18n] } })
    expect(wrapper.text()).toBe('External Runner (E2B)')
  })

  it('renders the Local badge for a local profile', () => {
    const wrapper = mount(BadgeHarness, { props: { providerType: 'local' }, global: { plugins: [i18n] } })
    expect(wrapper.text()).toBe('Local')
  })

  it('falls back to the raw provider type when no tier matches', () => {
    const wrapper = mount(BadgeHarness, { props: { providerType: 'mystery' }, global: { plugins: [i18n] } })
    expect(wrapper.text()).toBe('mystery')
  })
})
