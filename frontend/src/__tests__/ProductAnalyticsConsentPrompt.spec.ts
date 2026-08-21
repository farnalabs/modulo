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

describe('ProductAnalyticsConsentPrompt', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('renders when prompt is eligible', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.consent.prompted = null
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(true)
  })

  it('does not render when already consented', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.consent.prompted = 'yes'
    store.consent.level = 'all'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('does not render when instance is disabled', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = false
    store.consent.prompted = null
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('does not render when declined permanently', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.consent.prompted = 'no'
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('calls submitConsent with accept on accept button click', async () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.isPartnerLicence = false
    store.consent.prompted = null
    store.consent.level = 'off'
    const submitSpy = vi.spyOn(store, 'submitConsent').mockResolvedValue(true)

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    await wrapper.find('[data-testid="product-analytics-accept"]').trigger('click')

    expect(submitSpy).toHaveBeenCalledWith('accept')
  })

  it('calls submitConsent with decline on decline button click', async () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.isPartnerLicence = false
    store.consent.prompted = null
    store.consent.level = 'off'
    const submitSpy = vi.spyOn(store, 'submitConsent').mockResolvedValue(true)

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    await wrapper.find('[data-testid="product-analytics-decline"]').trigger('click')

    expect(submitSpy).toHaveBeenCalledWith('decline')
  })

  it('calls submitConsent with dismiss on dismiss button click', async () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.isPartnerLicence = false
    store.consent.prompted = null
    store.consent.level = 'off'
    const submitSpy = vi.spyOn(store, 'submitConsent').mockResolvedValue(true)

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    await wrapper.find('[data-testid="product-analytics-dismiss"]').trigger('click')

    expect(submitSpy).toHaveBeenCalledWith('dismiss')
  })

  it('renders partner carve-out variant with enable and stay-community buttons', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.isPartnerLicence = true
    store.consent.prompted = null
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-partner-enable"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="product-analytics-partner-stay-community"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="product-analytics-accept"]').exists()).toBe(false)
  })

  it('does not render when dismissed within cooldown', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.consent.prompted = 'dismissed'
    store.consent.prompted_at = new Date().toISOString()
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(false)
  })

  it('renders when dismiss cooldown has expired', () => {
    const store = useProductAnalyticsStore()
    store.instanceEnabled = true
    store.consent.prompted = 'dismissed'
    store.consent.prompted_at = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString()
    store.consent.level = 'off'

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    expect(wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists()).toBe(true)
  })
})
