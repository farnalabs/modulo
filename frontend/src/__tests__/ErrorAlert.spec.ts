import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import ErrorAlert from '../components/shared/ErrorAlert.vue'

describe('ErrorAlert', () => {
  it('renders the i18n Retry label (FAR-1655 / I18N-1)', async () => {
    // The Retry label is not hardcoded English — it resolves through the
    // shared `components.ErrorAlert.retry` locale key.
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onRetry: () => {} },
    })
    await nextTick()
    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)
    expect(retry.text()).toBe('Retry')
    // Unresolved-key leak guard: vue-i18n returns the key itself when the
    // message is missing, so the raw dotted key must never appear.
    expect(retry.text()).not.toContain('components.ErrorAlert')
  })

  it('invokes onRetry when the retry button is clicked', async () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onRetry },
    })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('omits the retry button when no onRetry is supplied', async () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom' },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
  })

  it('omits the retry button when retryable is false', async () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onRetry: () => {}, retryable: false },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
  })
})
