import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'
import type { Mock } from 'vitest'

async function nextTick() { await vueNextTick(); await flushPromises() }

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsSsoView from '../views/SettingsSsoView.vue'
import { api } from '../lib/api/client'

const provider = (over: Record<string, unknown> = {}) => ({
  id: 'sso-1',
  provider_type: 'oidc',
  name: 'Acme SSO',
  client_id: 'cid-1',
  discovery_url: 'https://idp.example.com/.well-known/openid-configuration',
  metadata_url: null,
  metadata_xml: null,
  entity_id: null,
  scopes: ['openid', 'email'],
  auto_provision: true,
  default_role: 'runner',
  enabled: true,
  ...over,
})

function mountView() {
  return mount(SettingsSsoView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        JsonViewer: { props: ['data', 'showToolbar', 'maxHeight'], template: '<div data-testid="json-viewer-stub" />' },
      },
    },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  ;(api.GET as Mock).mockResolvedValue({ data: [provider()], error: undefined })
  ;(api.POST as Mock).mockResolvedValue({ data: provider(), error: undefined })
  ;(api.PUT as Mock).mockResolvedValue({ data: provider({ enabled: false }), error: undefined })
  ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
})

describe('SettingsSsoView — provider list', () => {
  it('renders provider cards with the type badge, client id and discovery url details', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('Acme SSO')
    expect(wrapper.text()).toContain('OIDC')
    expect(wrapper.text()).toContain('cid-1')
  })

  it('shows the empty state when no providers are configured (fe-003)', async () => {
    ;(api.GET as Mock).mockResolvedValue({ data: [], error: undefined })
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('No SSO providers configured')
  })

  it('surfaces a thrown load failure inline (fe-002 message path)', async () => {
    // A THROWN fetch error propagates through vue-query into the error state.
    ;(api.GET as Mock).mockRejectedValue(new Error('sso down'))
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('sso down')
  })

  it('renders the ErrorAlert retry button by default (fe-002)', async () => {
    // ErrorAlert defaults retryable to true (FAR-608), so any error rendered
    // with an on-retry handler offers the retry action.
    ;(api.GET as Mock).mockRejectedValue(new Error('sso down'))
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('sso down')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
  })

  it('surfaces a failing GET error envelope inline instead of the empty state (fe-002)', async () => {
    // The view's fetcher forwards the response's error envelope to
    // useDataFetch (FAR-608), so a failed GET renders the ErrorAlert — not
    // the "No SSO providers configured" empty state.
    ;(api.GET as Mock).mockResolvedValue({ data: undefined, error: { detail: 'sso down' } })
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('sso down')
    expect(wrapper.text()).not.toContain('No SSO providers configured')
    expect(wrapper.findAll('button').filter((b) => b.text() === 'Retry')).toHaveLength(1)
  })

  it('renders a SAML provider with its entity id', async () => {
    ;(api.GET as Mock).mockResolvedValue({ data: [provider({ id: 'sso-2', provider_type: 'saml', client_id: null, entity_id: 'https://idp/entity' })], error: undefined })
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).toContain('SAML')
    expect(wrapper.text()).toContain('https://idp/entity')
  })
})

describe('SettingsSsoView — create provider', () => {
  it('opens the add form from the header button and closes it on cancel', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.text()).not.toContain('New SSO Provider')

    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('New SSO Provider')

    await wrapper.findAll('button').find((b) => b.text() === 'Cancel')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('New SSO Provider')
  })

  it('the create button is disabled while the name is empty', async () => {
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    const createBtn = wrapper.findAll('button').find((b) => b.text() === 'Create')
    expect(createBtn?.attributes('disabled')).toBeDefined()
  })

  it('rejects a whitespace-only name with the validation error', async () => {
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    const vm = wrapper.vm as unknown as { createProvider: () => Promise<void> }
    await vm.createProvider()
    await nextTick()
    expect(wrapper.text()).toContain('Provider name is required')
    expect(api.POST).not.toHaveBeenCalled()
  })

  it('creates an OIDC provider via POST with the trimmed and parsed form values', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: provider({ id: 'sso-new', name: 'New SSO' }), error: undefined })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()

    await wrapper.find('#ssoproviderform-field-9').setValue('  New SSO  ')
    await wrapper.find('#ssoproviderform-field-8').setValue(' cid-new ')
    await wrapper.find('#ssoproviderform-field-6').setValue('https://idp.new/.well-known')
    await wrapper.find('#ssoproviderform-field-5').setValue('openid, email')
    await wrapper.findAll('button').find((b) => b.text() === 'Create')!.trigger('click')
    await nextTick()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/admin/sso/providers')
    expect(opts.body).toEqual({
      provider_type: 'oidc',
      name: 'New SSO',
      auto_provision: true,
      default_role: 'runner',
      enabled: true,
      client_id: 'cid-new',
      client_secret: null,
      discovery_url: 'https://idp.new/.well-known',
      scopes: ['openid', 'email'],
    })
    // The form closes and the list refetches (FAR-608): the created provider
    // appears once the GET mock reflects the server state after the POST.
    expect(wrapper.text()).not.toContain('New SSO Provider')
  })

  it('the created provider appears in the list after the refetch', async () => {
    // createProvider refetches on success (FAR-608) instead of pushing into
    // the readonly vue-query proxy — simulate the server gaining the provider.
    let current = [provider()]
    ;(api.GET as Mock).mockImplementation(async () => ({ data: current, error: undefined }))
    ;(api.POST as Mock).mockImplementation(async () => {
      current = [provider(), provider({ id: 'sso-new', name: 'New SSO' })]
      return { data: provider({ id: 'sso-new', name: 'New SSO' }), error: undefined }
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    await wrapper.find('#ssoproviderform-field-9').setValue('New SSO')
    await wrapper.findAll('button').find((b) => b.text() === 'Create')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Acme SSO')
    expect(wrapper.text()).toContain('New SSO')
  })

  it('creates a SAML provider carrying metadata fields instead of OIDC fields', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: provider({ id: 'sso-saml', provider_type: 'saml' }), error: undefined })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()

    // Switch the form to SAML mode via its toggle buttons.
    const samlToggle = wrapper.findAll('button').find((b) => b.text().includes('SAML'))
    await samlToggle!.trigger('click')
    await nextTick()

    await wrapper.find('#ssoproviderform-field-9').setValue('SAML Corp')
    await wrapper.find('#ssoproviderform-field-4').setValue('https://idp.example.com/metadata.xml')
    await wrapper.find('#ssoproviderform-field-2').setValue('https://idp/entity')
    await wrapper.findAll('button').find((b) => b.text() === 'Create')!.trigger('click')
    await nextTick()

    const body = (api.POST as Mock).mock.calls[0][1].body
    expect(body.provider_type).toBe('saml')
    expect(body.metadata_url).toBe('https://idp.example.com/metadata.xml')
    expect(body.entity_id).toBe('https://idp/entity')
    expect(body.client_id).toBeUndefined()
    expect(body.discovery_url).toBeUndefined()
  })

  it('shows a create failure in the form and keeps the form open', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: undefined, error: { detail: 'duplicate provider' } })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    await wrapper.find('#ssoproviderform-field-9').setValue('Dup')
    await wrapper.findAll('button').find((b) => b.text() === 'Create')!.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('duplicate provider')
    expect(wrapper.find('#ssoproviderform-field-9').exists()).toBe(true)
  })

  it('surfaces a thrown create error via formatError', async () => {
    ;(api.POST as Mock).mockRejectedValue(new Error('network gone'))
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-add-provider"]').trigger('click')
    await nextTick()
    await wrapper.find('#ssoproviderform-field-9').setValue('Boom')
    await wrapper.findAll('button').find((b) => b.text() === 'Create')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('network gone')
  })
})

describe('SettingsSsoView — edit provider', () => {
  async function openEditForm(wrapper: ReturnType<typeof mountView>) {
    await wrapper.find('[data-testid="settings-sso-edit"]').trigger('click')
    await nextTick()
  }

  it('pre-fills the edit form with the provider values', async () => {
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)

    const nameInput = wrapper.find('#ssoproviderform-field-9')
    expect((nameInput.element as HTMLInputElement).value).toBe('Acme SSO')
    expect((wrapper.find('#ssoproviderform-field-8').element as HTMLInputElement).value).toBe('cid-1')
    expect((wrapper.find('#ssoproviderform-field-5').element as HTMLInputElement).value).toBe('openid, email')
  })

  it('saves via PUT with an update body that omits a blank client secret', async () => {
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)

    await wrapper.find('#ssoproviderform-field-9').setValue('Acme SSO Renamed')
    await wrapper.findAll('button').find((b) => b.text() === 'Save')!.trigger('click')
    await nextTick()

    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.PUT as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/admin/sso/providers/{provider_id}')
    expect(opts.params.path.provider_id).toBe('sso-1')
    expect(opts.body.name).toBe('Acme SSO Renamed')
    expect(opts.body.client_id).toBe('cid-1')
    expect(opts.body.client_secret).toBeUndefined()
    expect(opts.body.scopes).toEqual(['openid', 'email'])
    // Edit form closes after success.
    expect(wrapper.find('#ssoproviderform-field-9').exists()).toBe(false)
  })

  it('the renamed provider is reflected in the list after the refetch', async () => {
    // updateProvider refetches on success (FAR-608) instead of writing into
    // the readonly vue-query proxy — simulate the server having the new name.
    let current = [provider()]
    ;(api.GET as Mock).mockImplementation(async () => ({ data: current, error: undefined }))
    ;(api.PUT as Mock).mockImplementation(async () => {
      current = [provider({ name: 'Acme SSO Renamed' })]
      return { data: current[0], error: undefined }
    })
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)
    await wrapper.find('#ssoproviderform-field-9').setValue('Acme SSO Renamed')
    await wrapper.findAll('button').find((b) => b.text() === 'Save')!.trigger('click')
    await nextTick()
    expect(api.PUT).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('Acme SSO Renamed')
    expect(wrapper.text()).not.toContain('New SSO Provider')
  })

  it('includes the client secret in the PUT body only when re-entered', async () => {
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)

    await wrapper.find('#ssoproviderform-field-7').setValue('sk-rotated')
    await wrapper.findAll('button').find((b) => b.text() === 'Save')!.trigger('click')
    await nextTick()
    expect((api.PUT as Mock).mock.calls[0][1].body.client_secret).toBe('sk-rotated')
  })

  it('shows a save failure in the edit form', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: { detail: 'update rejected' } })
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)
    await wrapper.findAll('button').find((b) => b.text() === 'Save')!.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('update rejected')
  })

  it('cancel closes the edit form', async () => {
    const wrapper = mountView()
    await nextTick()
    await openEditForm(wrapper)
    await wrapper.findAll('button').find((b) => b.text() === 'Cancel')!.trigger('click')
    await nextTick()
    expect(wrapper.find('#ssoproviderform-field-9').exists()).toBe(false)
  })
})

describe('SettingsSsoView — toggle provider', () => {
  it('toggles via the switch and calls the toggle endpoint', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: provider({ enabled: false }), error: undefined })
    const wrapper = mountView()
    await nextTick()

    const toggle = wrapper.find('[data-testid="settings-sso-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
    await toggle.trigger('click')
    await nextTick()

    expect(api.PUT).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.PUT as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/admin/sso/providers/{provider_id}/toggle')
    expect(opts.params.path.provider_id).toBe('sso-1')
  })

  it('a successful toggle refetches and flips the switch visually', async () => {
    // toggleProvider refetches on success (FAR-608) instead of writing into
    // the readonly vue-query proxy — simulate the server toggling the state.
    let current = provider({ enabled: true })
    ;(api.GET as Mock).mockImplementation(async () => ({ data: [current], error: undefined }))
    ;(api.PUT as Mock).mockImplementation(async () => {
      current = provider({ enabled: false })
      return { data: current, error: undefined }
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-toggle"]').trigger('click')
    await nextTick()
    expect(api.PUT).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-testid="settings-sso-toggle"]').attributes('aria-checked')).toBe('false')
  })

  it('shows a toggle failure from the error envelope', async () => {
    ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: { detail: 'toggle blocked' } })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-toggle"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Toggle failed:')
    expect(wrapper.text()).toContain('toggle blocked')
  })

  it('shows a toggle failure from a thrown error', async () => {
    ;(api.PUT as Mock).mockRejectedValue(new Error('toggle exploded'))
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-toggle"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Toggle failed:')
    expect(wrapper.text()).toContain('toggle exploded')
  })
})

describe('SettingsSsoView — delete provider', () => {
  async function openDeleteConfirm(wrapper: ReturnType<typeof mountView>) {
    const deleteBtn = wrapper.findAll('button').find((b) => b.text() === 'Delete')
    await deleteBtn!.trigger('click')
    await nextTick()
  }

  it('asks for confirmation, then DELETEs and removes the card', async () => {
    const wrapper = mountView()
    await nextTick()
    await openDeleteConfirm(wrapper)

    expect(wrapper.text()).toContain('Delete "Acme SSO"?')
    expect(wrapper.text()).toContain('This action cannot be undone.')

    await wrapper.find('[data-testid="settings-sso-delete-confirm"]').trigger('click')
    await nextTick()

    expect(api.DELETE).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.DELETE as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/admin/sso/providers/{provider_id}')
    expect(opts.params.path.provider_id).toBe('sso-1')
    expect(wrapper.text()).not.toContain('Acme SSO')
    expect(wrapper.text()).toContain('No SSO providers configured')
  })

  it('cancel closes the delete confirmation', async () => {
    const wrapper = mountView()
    await nextTick()
    await openDeleteConfirm(wrapper)
    await wrapper.find('[data-testid="settings-sso-delete-cancel"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('Delete "Acme SSO"?')
    expect(api.DELETE).not.toHaveBeenCalled()
  })

  it('a DELETE error envelope surfaces the failure and keeps the provider', async () => {
    ;(api.DELETE as Mock).mockResolvedValue({ response: { status: 409 }, error: { detail: 'in use' } })
    const wrapper = mountView()
    await nextTick()
    await openDeleteConfirm(wrapper)
    await wrapper.find('[data-testid="settings-sso-delete-confirm"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('in use')
    expect(wrapper.text()).toContain('Acme SSO')
  })

  it('a thrown DELETE error surfaces the failure', async () => {
    ;(api.DELETE as Mock).mockRejectedValue(new Error('delete exploded'))
    const wrapper = mountView()
    await nextTick()
    await openDeleteConfirm(wrapper)
    await wrapper.find('[data-testid="settings-sso-delete-confirm"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('delete exploded')
  })
})

describe('SettingsSsoView — test connection', () => {
  it('shows a successful test result with the provider info viewer', async () => {
    ;(api.POST as Mock).mockResolvedValue({
      data: { success: true, message: 'All good', provider_info: { issuer: 'https://idp' } },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()

    const testBtn = wrapper.find('[data-testid="settings-sso-test"]')
    await testBtn.trigger('click')
    await nextTick()

    expect(api.POST).toHaveBeenCalledTimes(1)
    const [url, opts] = (api.POST as Mock).mock.calls[0]
    expect(url).toBe('/api/v1/admin/sso/providers/{provider_id}/test')
    expect(opts.params.path.provider_id).toBe('sso-1')
    expect(wrapper.text()).toContain('Connection successful')
    expect(wrapper.text()).toContain('All good')
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(true)
  })

  it('shows a failed test result without the provider info viewer', async () => {
    ;(api.POST as Mock).mockResolvedValue({
      data: { success: false, message: 'Discovery failed', provider_info: null },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-test"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('Discovery failed')
    expect(wrapper.find('[data-testid="json-viewer-stub"]').exists()).toBe(false)
  })

  it('shows the error envelope as a failed result', async () => {
    ;(api.POST as Mock).mockResolvedValue({ data: undefined, error: { detail: 'idp unreachable' } })
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-test"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('idp unreachable')
  })

  it('shows a thrown error as a failed result', async () => {
    ;(api.POST as Mock).mockRejectedValue(new Error('timeout'))
    const wrapper = mountView()
    await nextTick()
    await wrapper.find('[data-testid="settings-sso-test"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Connection failed')
    expect(wrapper.text()).toContain('timeout')
  })
})
