import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'

// The one-time setup token is delivered in the URL FRAGMENT (#token=...) — never
// the query string — so it is not sent to the server nor leaked via Referer/access
// logs. This spec locks in that the view reads the token from the fragment and
// strips it from the address bar / browser history after capturing it.
const mockPost = vi.fn().mockResolvedValue({ status: 'ok', backend_id: 'abc', name: 'OpenAI Prod' })

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'abc' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ post: mockPost }),
}))

vi.mock('../../composables/useMutation', () => ({
  useMutation: (fn: (input: void) => Promise<unknown>) => ({
    loading: { value: false },
    error: { value: null },
    mutate: fn,
  }),
}))

import ModelBackendSetupView from '../views/setup/ModelBackendSetupView.vue'

describe('ModelBackendSetupView', () => {
  it('reads the setup token from the URL fragment and strips it from the address bar', () => {
    window.location.hash = '#token=secret-token-123'
    const originalSearch = window.location.search
    const originalPath = window.location.pathname

    mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
          InputText: true,
        },
      },
    })

    // The token was parsed from the fragment, so the view strips it via
    // replaceState. If parsing had failed, the strip guard would not fire.
    expect(window.location.hash).toBe('')
    expect(window.location.search).toBe(originalSearch)
    expect(window.location.pathname).toBe(originalPath)
  })

  it('does not touch the URL when no fragment token is present', () => {
    window.location.hash = ''
    mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
          InputText: true,
        },
      },
    })

    expect(window.location.hash).toBe('')
  })

  it('hides the API-key form and shows the missing-token message when there is no fragment token', () => {
    window.location.hash = ''
    const wrapper = mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
          InputText: true,
        },
      },
    })

    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).toContain('missing its one-time token')
  })

  it('treats an empty fragment value (#token=) as missing', () => {
    window.location.hash = '#token='
    const wrapper = mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
          InputText: true,
        },
      },
    })

    expect(wrapper.find('form').exists()).toBe(false)
    expect(wrapper.text()).toContain('missing its one-time token')
  })

  it('renders the API-key form when a fragment token is present', () => {
    window.location.hash = '#token=secret-token-123'
    const wrapper = mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
          InputText: true,
        },
      },
    })

    expect(wrapper.find('form').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('missing its one-time token')
  })
})
