import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ProductAnalyticsConsentPrompt from '../components/product-analytics/ProductAnalyticsConsentPrompt.vue'
import { useProductAnalyticsStore } from '../stores/productAnalyticsStore'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string) => key,
  }),
}))

vi.mock('primevue/button', () => ({
  default: {
    name: 'Button',
    template: '<button @click="$emit(\'click\')"><slot /></button>',
    emits: ['click'],
  },
}))

type ConsentOverrides = {
  instanceEnabled?: boolean
  isPartnerLicence?: boolean
  prompted?: 'yes' | 'no' | 'dismissed' | null
  prompted_at?: string | null
  level?: 'off' | 'all'
}

function setupConsentStore(overrides: ConsentOverrides = {}): ReturnType<typeof useProductAnalyticsStore> {
  const store = useProductAnalyticsStore()
  store.instanceEnabled = overrides.instanceEnabled ?? true
  store.isPartnerLicence = overrides.isPartnerLicence ?? false
  store.consent.prompted = overrides.prompted ?? null
  store.consent.prompted_at = overrides.prompted_at ?? null
  store.consent.level = overrides.level ?? 'off'
  return store
}

describe('ProductAnalyticsConsentPrompt', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders when prompt is eligible', () => {
    setupConsentStore()

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(true)
  })

  it('does not render when already consented', () => {
    setupConsentStore({ prompted: 'yes', level: 'all' })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('does not render when instance is disabled', () => {
    setupConsentStore({ instanceEnabled: false })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('does not render when declined permanently', () => {
    setupConsentStore({ prompted: 'no' })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it.each([
    ['accept', 'product-analytics-accept'],
    ['decline', 'product-analytics-decline'],
    ['dismiss', 'product-analytics-dismiss'],
  ])('calls submitConsent with %s on %s button click', async (action, testid) => {
    const store = setupConsentStore({ isPartnerLicence: false })
    const submitSpy = vi.spyOn(store, 'submitConsent').mockResolvedValue(true)

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    await wrapper.find(`[data-testid="${testid}"]`).trigger('click')

    expect(submitSpy).toHaveBeenCalledWith(action)
  })

  it('renders partner carve-out variant with enable and stay-community buttons', () => {
    setupConsentStore({ isPartnerLicence: true })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-partner-enable"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="product-analytics-partner-stay-community"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="product-analytics-accept"]').exists()).toBe(false)
  })

  it('does not render when dismissed within cooldown', () => {
    setupConsentStore({ prompted: 'dismissed', prompted_at: new Date().toISOString() })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('renders when dismiss cooldown has expired', () => {
    setupConsentStore({
      prompted: 'dismissed',
      prompted_at: new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString(), // nosemgrep: new-date-without-guard - deterministic past timestamp
    })

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(true)
  })
})
