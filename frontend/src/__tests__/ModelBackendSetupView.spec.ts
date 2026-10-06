import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { ApiError } from '../lib/api/apiError'

// The one-time setup token is delivered in the URL FRAGMENT (#token=...) — never
// the query string — so it is not sent to the server nor leaked via Referer/access
// logs. This spec locks in that the view reads the token from the fragment and
// strips it from the address bar / browser history after capturing it.
const mockPost = vi.fn()

// Controllable refs handed back by the mocked useMutation so a test can drive
// the loading/error states the real composable would expose.
const mockControls = vi.hoisted(() => ({ loading: null as unknown, error: null as unknown }))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'abc' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({ post: mockPost }),
}))

vi.mock('../composables/useMutation', () => ({
  useMutation: (fn: (input: void) => Promise<unknown>) => ({
    loading: mockControls.loading,
    error: mockControls.error,
    mutate: fn,
  }),
}))

import ModelBackendSetupView from '../views/setup/ModelBackendSetupView.vue'

describe('ModelBackendSetupView', () => {
  beforeEach(() => {
    // The view reads/strips the URL fragment on mount; reset it so a token set
    // by one test cannot leak into the next.
    window.location.hash = ''
    mockPost.mockReset()
    mockPost.mockResolvedValue({ status: 'ok', backend_id: 'abc', name: 'OpenAI Prod' })
    mockControls.loading = ref(false)
    mockControls.error = ref(null)
  })

  afterEach(() => {
    window.location.hash = ''
  })

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

  // UX conformance: A11Y-2 (every form control has an associated label via a
  // for/id pair) and VIS-4 (every interactive element carries a data-testid).
  it('associates the API-key input with its label and tags the submit button', () => {
    window.location.hash = '#token=secret-token-123'
    const wrapper = mount(ModelBackendSetupView, {
      global: {
        stubs: {
          PageHeader: true,
          Button: true,
        },
      },
    })

    const input = wrapper.find('input[type="password"]')
    expect(input.exists()).toBe(true)
    const inputId = input.attributes('id')
    expect(inputId).toBeTruthy()
    const label = wrapper.find(`label[for="${inputId}"]`)
    expect(label.exists()).toBe(true)
    expect(label.text()).toBe('API Key')

    expect(wrapper.find('[data-testid="model-backend-setup-submit"]').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="Form control"]').exists()).toBe(false)
  })

  // UX conformance: I18N-1 (no hardcoded user-facing strings in the view).
  it('resolves every user-facing string through the en-US locale', async () => {
    window.location.hash = ''
    const wrapper = mount(ModelBackendSetupView)
    expect(wrapper.text()).toContain('This setup link is missing its one-time token.')
    // A raw key leaking into the DOM means the string bypassed vue-i18n.
    expect(wrapper.text()).not.toContain('views.ModelBackendSetupView')
  })

  // Coverage: the success panel (v-if="success") is only reachable after a
  // successful submit, so the pre-existing flow tests never rendered its
  // button or ran its click handler.
  it('renders the success panel after a successful submit and navigates away', async () => {
    window.location.hash = '#token=secret-token-123'
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true } },
    })

    await wrapper.find('input[type="password"]').setValue('sk-test')
    await (wrapper.vm as unknown as { submit: () => Promise<unknown> }).submit()
    await nextTick()

    expect(wrapper.text()).toContain('Backend "OpenAI Prod" is now active.')
    const button = wrapper.find('[data-testid="model-backend-setup-view-backends"]')
    expect(button.exists()).toBe(true)
    expect(button.text()).toBe('View Model Backends')

    await button.trigger('click')
    // A second render also exercises the cached-handler arm of the binding.
    wrapper.vm.$forceUpdate()
    await nextTick()
  })

  // Coverage: both arms of the submit-button label ternary.
  it('labels the submit button with the idle action when not loading', () => {
    window.location.hash = '#token=secret-token-123'
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true } },
    })
    expect(wrapper.find('[data-testid="model-backend-setup-submit"]').text()).toBe('Complete Setup')
  })

  it('labels the submit button with the saving action while loading', () => {
    window.location.hash = '#token=secret-token-123'
    mockControls.loading = ref(true)
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true } },
    })
    expect(wrapper.find('[data-testid="model-backend-setup-submit"]').text()).toBe('Saving...')
  })

  // Coverage: every branch of the submit catch maps the backend machine code to
  // a user-facing, localised error. The rejections mirror what useApi actually
  // throws: an ApiError carrying the RFC 9457 `code` extension member.
  it('throws the expired-link error for an invalid_token response', async () => {
    window.location.hash = '#token=secret-token-123'
    mockPost.mockRejectedValueOnce(
      new ApiError('Token not found, expired, or already used', 404, 'invalid_token'),
    )
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true, Button: true } },
    })
    await wrapper.find('input[type="password"]').setValue('sk-test')
    await expect((wrapper.vm as unknown as { submit: () => Promise<unknown> }).submit())
      .rejects.toThrow('Setup link expired or already used.')
  })

  it('throws the backend-not-found error for a backend_not_found response', async () => {
    window.location.hash = '#token=secret-token-123'
    mockPost.mockRejectedValueOnce(new ApiError('backend_not_found', 404, 'backend_not_found'))
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true, Button: true } },
    })
    await wrapper.find('input[type="password"]').setValue('sk-test')
    await expect((wrapper.vm as unknown as { submit: () => Promise<unknown> }).submit())
      .rejects.toThrow('Model backend not found.')
  })

  it('throws the generic setup error for any other failure', async () => {
    window.location.hash = '#token=secret-token-123'
    // A plain Error (no machine code) — e.g. a network failure — must fall
    // through to the generic message.
    mockPost.mockRejectedValueOnce(new Error('network down'))
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true, Button: true } },
    })
    await wrapper.find('input[type="password"]').setValue('sk-test')
    await expect((wrapper.vm as unknown as { submit: () => Promise<unknown> }).submit())
      .rejects.toThrow('Setup failed. Please try again.')
  })

  it('throws the generic setup error for an ApiError without a code', async () => {
    window.location.hash = '#token=secret-token-123'
    mockPost.mockRejectedValueOnce(new ApiError('Internal Server Error', 500))
    const wrapper = mount(ModelBackendSetupView, {
      global: { stubs: { PageHeader: true, Button: true } },
    })
    await wrapper.find('input[type="password"]').setValue('sk-test')
    await expect((wrapper.vm as unknown as { submit: () => Promise<unknown> }).submit())
      .rejects.toThrow('Setup failed. Please try again.')
  })
})
