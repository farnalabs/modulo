import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { ref } from 'vue'

const fetchConsent = vi.fn()
const error = ref<string | null>(null)
let nullishProbe = false
let errorReads = 0

vi.mock('../stores/productAnalyticsStore', () => ({
  useProductAnalyticsStore: () => ({
    get error() {
      errorReads += 1
      if (nullishProbe && errorReads > 3) return null
      return error.value
    },
    set error(value: string | null) {
      error.value = value
    },
    fetchConsent,
  }),
}))

import ProductAnalyticsError from '../components/product-analytics/ProductAnalyticsError.vue'

const ErrorAlertStub = {
  props: ['message', 'onRetry', 'onDismiss', 'dismissLabel'],
  template: `
    <div data-testid="error-alert">
      <span>{{ message }}</span>
      <button data-testid="retry" @click="onRetry">Retry</button>
      <button data-testid="dismiss" @click="onDismiss">{{ dismissLabel }}</button>
    </div>
  `,
}

describe('ProductAnalyticsError', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    error.value = null
    nullishProbe = false
    errorReads = 0
    fetchConsent.mockReset()
  })

  it('renders nothing when the store has no error', () => {
    const wrapper = mount(ProductAnalyticsError, { global: { stubs: { ErrorAlert: ErrorAlertStub } } })
    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
  })

  it('renders the error and wires retry and dismiss behavior', async () => {
    error.value = 'Consent request failed'
    nullishProbe = true
    const wrapper = mount(ProductAnalyticsError, { global: { stubs: { ErrorAlert: ErrorAlertStub } } })
    await flushPromises()

    expect(wrapper.find('[data-testid="error-alert"]').text()).toContain('Consent request failed')
    expect(errorReads).toBeGreaterThan(0)
    expect(wrapper.find('[data-testid="error-alert"]').attributes('data-testid')).toBe('error-alert')
    await wrapper.find('[data-testid="retry"]').trigger('click')
    expect(fetchConsent).toHaveBeenCalledOnce()

    await wrapper.find('[data-testid="dismiss"]').trigger('click')
    await wrapper.vm.$nextTick()
    expect(error.value).toBeNull()
    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
  })
})
