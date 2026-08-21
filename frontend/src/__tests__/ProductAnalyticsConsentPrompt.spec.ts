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

  const visibilityCases: Array<[string, ConsentOverrides, boolean]> = [
    ['renders when prompt is eligible', {}, true],
    ['does not render when already consented', { prompted: 'yes', level: 'all' }, false],
    ['does not render when instance is disabled', { instanceEnabled: false }, false],
    ['does not render when declined permanently', { prompted: 'no' }, false],
    [
      'does not render when dismissed within cooldown',
      { prompted: 'dismissed', prompted_at: new Date().toISOString() },
      false,
    ],
    [
      'renders when dismiss cooldown has expired',
      {
        prompted: 'dismissed',
        prompted_at: new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString(), // nosemgrep: new-date-without-guard - deterministic past timestamp
      },
      true,
    ],
  ]

  it.each(visibilityCases)(
    '%s',
    (name: string, overrides: ConsentOverrides, expectedVisible: boolean) => {
      setupConsentStore(overrides)

      const wrapper = mount(ProductAnalyticsConsentPrompt)
      expect(
        wrapper.find('[data-testid="product-analytics-consent-prompt"]').exists(),
      ).toBe(expectedVisible)
    },
  )

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
})
