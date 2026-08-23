import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'

describe('ErrorAlert', () => {
  it('renders a Retry button when onRetry is passed and no variant is set', async () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert as any, {
      props: { message: 'Something failed', onRetry },
    })
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeTruthy()
    await retry?.trigger('click')
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('renders a Retry button when variant is "error"', () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert as any, {
      props: { message: 'Something failed', variant: 'error', onRetry },
    })
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeTruthy()
  })

  it('does NOT render a Retry button when variant is "success"', () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert as any, {
      props: { message: 'Saved', variant: 'success', onRetry },
    })
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeFalsy()
  })

  it('does NOT render a Retry button when onRetry is omitted', () => {
    const wrapper = mount(ErrorAlert as any, {
      props: { message: 'Something failed' },
    })
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeFalsy()
  })

  it('does NOT render the Retry button when retryable is false', () => {
    const onRetry = vi.fn()
    const wrapper = mount(ErrorAlert as any, {
      props: { message: 'Something failed', onRetry, retryable: false },
    })
    const retry = wrapper.findAll('button').find((b) => b.text() === 'Retry')
    expect(retry).toBeFalsy()
  })
})
