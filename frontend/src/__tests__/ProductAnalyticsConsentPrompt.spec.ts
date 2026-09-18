import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import ProductAnalyticsConsentPrompt from '../components/product-analytics/ProductAnalyticsConsentPrompt.vue'
import { useProductAnalyticsStore } from '../stores/productAnalyticsStore'
import { api } from '../lib/api/client'

vi.mock('vue-i18n', () => ({
  useI18n: () => ({
    t: (key: string) => key,
  }),
}))

vi.mock('primevue/button', () => ({
  default: {
    name: 'Button',
    template: '<button type="button" @click="$emit(\'click\')"><slot /></button>',
    emits: ['click'],
  },
}))

// Real flat wire shape returned by the backend (ConsentResponse).
const flatConsent = {
  level: 'off',
  prompted: null,
  prompted_at: null,
  level_changed_at: null,
  instance_enabled: true,
  egress_allowed: false,
  prompt_eligible: true,
}

function mockGet(data: unknown = flatConsent) {
  return vi.spyOn(api as any, 'GET').mockResolvedValue({ data, error: undefined } as never)
}
function mockPost(data: unknown = flatConsent) {
  return vi.spyOn(api as any, 'POST').mockResolvedValue({ data, error: undefined } as never)
}
function mockPut(data: unknown = { level: 'all', level_changed_at: null }) {
  return vi.spyOn(api as any, 'PUT').mockResolvedValue({ data, error: undefined } as never)
}

type ConsentOverrides = {
  instanceEnabled?: boolean
  promptEligible?: boolean
}

function setupConsentStore(overrides: ConsentOverrides = {}): ReturnType<typeof useProductAnalyticsStore> {
  const store = useProductAnalyticsStore()
  store.instanceEnabled = overrides.instanceEnabled ?? true
  store.consent.prompt_eligible = overrides.promptEligible ?? true
  return store
}

describe('ProductAnalyticsConsentPrompt', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  // Eligibility is backend-driven via consent.prompt_eligible (the backend owns
  // the dismiss-cooldown policy), so the render decision is a pure reflection of
  // that field gated by instance enablement.
  const visibilityCases: Array<[string, ConsentOverrides, boolean]> = [
    ['renders when backend reports prompt eligible', {}, true],
    ['does not render when backend reports not eligible', { promptEligible: false }, false],
    ['does not render when instance is disabled', { instanceEnabled: false }, false],
    ['does not render when already consented', { promptEligible: false }, false],
    ['does not render when declined permanently', { promptEligible: false }, false],
    ['renders when dismiss cooldown has expired (backend-driven)', { promptEligible: true }, true],
  ]

  it.each(visibilityCases)(
    '%s',
    (_name: string, overrides: ConsentOverrides, expectedVisible: boolean) => {
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
    const store = setupConsentStore()
    const submitSpy = vi.spyOn(store, 'submitConsent').mockResolvedValue(true)

    const wrapper = mount(ProductAnalyticsConsentPrompt)
    await wrapper.find(`[data-testid="${testid}"]`).trigger('click')

    expect(submitSpy).toHaveBeenCalledWith(action)
  })

  describe('contract round-trip (real flat payloads)', () => {
    it('maps GET /api/v1/org/product-analytics into store state', async () => {
      mockGet(flatConsent)
      const store = useProductAnalyticsStore()
      const ok = await store.fetchConsent()
      expect(ok).toBe(true)
      expect(store.consent.level).toBe('off')
      expect(store.consent.prompted).toBeNull()
      expect(store.instanceEnabled).toBe(true)
      expect(store.isOptedIn).toBe(false)
      expect(store.error).toBeNull()
    })

    it('maps POST /consent response and reflects opted-in state', async () => {
      mockPost({ ...flatConsent, level: 'all', prompted: 'yes' })
      const store = useProductAnalyticsStore()
      const ok = await store.submitConsent('accept')
      expect(ok).toBe(true)
      expect(store.isOptedIn).toBe(true)
      expect(store.consent.prompted).toBe('yes')
    })

    it('updateLevel PUTs and re-fetches the full consent state', async () => {
      const getSpy = mockGet({ ...flatConsent, level: 'all' })
      mockPut({ level: 'all', level_changed_at: '2026-08-22T00:00:00Z' })
      const store = useProductAnalyticsStore()
      const ok = await store.updateLevel('all')
      expect(ok).toBe(true)
      expect(getSpy).toHaveBeenCalledWith('/api/v1/org/product-analytics')
      expect(store.isOptedIn).toBe(true)
      expect(store.error).toBeNull()
    })

    it('surfaces an API error from GET as store.error', async () => {
      vi.spyOn(api as any, 'GET').mockResolvedValue({
        data: undefined,
        error: { detail: 'boom' },
      } as never)
      const store = useProductAnalyticsStore()
      const ok = await store.fetchConsent()
      expect(ok).toBe(false)
      expect(store.error).toBe('boom')
    })
  })
})
