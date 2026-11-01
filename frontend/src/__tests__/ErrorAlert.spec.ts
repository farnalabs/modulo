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
    expect(wrapper.attributes('role')).toBe('alert')
    expect(wrapper.attributes('aria-live')).toBe('assertive')
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

  it('renders an empty body when no message is supplied (no empty <p>)', async () => {
    // The message prop is optional; omitting it must not render a blank <p>.
    const wrapper = mount(ErrorAlert, {
      props: { onRetry: () => {} },
    })
    await nextTick()
    expect(wrapper.find('p').exists()).toBe(false)
  })

  // -- ProblemDetail message (RFC 9457 problem object) ---------------------

  it('renders a ProblemDetail message as "title: detail"', async () => {
    // When `message` is a problem object rather than a string, the component
    // shows the RFC 9457 `title` and `detail` instead of the raw message.
    const wrapper = mount(ErrorAlert, {
      props: {
        message: {
          type: 'urn:problem:modulo:not_found',
          title: 'Not Found',
          status: 404,
          detail: 'Pipeline not found',
        },
      },
    })
    await nextTick()
    expect(wrapper.text()).toContain('Not Found: Pipeline not found')
  })

  // -- compact density variant (FAR-1669 sweep follow-up) ------------------

  it('applies the compact density classes when compact is true', async () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', compact: true },
    })
    await nextTick()
    // The tighter form the bespoke dense-context boxes used: p-3 text-sm.
    expect(wrapper.classes()).toContain('p-3')
    expect(wrapper.classes()).toContain('text-sm')
    expect(wrapper.classes()).not.toContain('p-4')
    // The shared destructive shell is unchanged.
    expect(wrapper.classes()).toEqual(expect.arrayContaining([
      'rounded-lg', 'border', 'border-destructive/50', 'bg-destructive/10', 'text-destructive-text',
    ]))
    expect(wrapper.text()).toContain('boom')
  })

  it('keeps the default density when compact is not supplied', async () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom' },
    })
    await nextTick()
    expect(wrapper.classes()).toContain('p-4')
    expect(wrapper.classes()).not.toContain('p-3')
    expect(wrapper.classes()).not.toContain('text-sm')
    expect(wrapper.classes()).toEqual(expect.arrayContaining([
      'rounded-lg', 'border', 'border-destructive/50', 'bg-destructive/10', 'text-destructive-text',
    ]))
    expect(wrapper.text()).toContain('boom')
  })

  // -- icon dismiss (FAR-1669 sweep follow-up) -----------------------------

  it('renders an icon-only dismiss button with the aria-label when dismissIcon is true', async () => {
    const onDismiss = vi.fn()
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onDismiss, dismissLabel: 'Close', dismissIcon: true },
    })
    await nextTick()
    const dismiss = wrapper.find('[data-testid="error-alert-dismiss"]')
    expect(dismiss.exists()).toBe(true)
    expect(dismiss.attributes('aria-label')).toBe('Close')
    // Icon-only: no visible text label, the X svg carries it.
    expect(dismiss.text()).toBe('')
    expect(dismiss.find('svg').exists()).toBe(true)
    // Root lays out as a flex row so the icon sits right of the message.
    expect(wrapper.classes()).toContain('flex')
    expect(wrapper.classes()).toContain('items-start')
    expect(wrapper.classes()).toContain('justify-between')
    await dismiss.trigger('click')
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('keeps the inline text dismiss by default (dismissIcon false)', async () => {
    const onDismiss = vi.fn()
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onDismiss, dismissLabel: 'Close' },
    })
    await nextTick()
    const dismiss = wrapper.find('[data-testid="error-alert-dismiss"]')
    expect(dismiss.exists()).toBe(true)
    expect(dismiss.text()).toBe('Close')
    expect(dismiss.attributes('aria-label')).toBeUndefined()
    expect(dismiss.find('svg').exists()).toBe(false)
    expect(wrapper.classes()).not.toContain('flex')
    await dismiss.trigger('click')
    expect(onDismiss).toHaveBeenCalledTimes(1)
  })

  it('keeps the retry button alongside the icon dismiss', async () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom', onRetry, onDismiss: () => {}, dismissLabel: 'Close', dismissIcon: true },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="error-alert-dismiss"]').exists()).toBe(true)
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})
