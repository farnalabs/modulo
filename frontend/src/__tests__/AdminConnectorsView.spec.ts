import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const { mockGet, mockPatch, mockPost, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
  mockPatch: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  mockPost: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  mockDelete: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: mockPost,
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: mockPatch,
    DELETE: mockDelete,
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminConnectorsView from '../views/AdminConnectorsView.vue'

function restConnectorItem(id: string, name: string, configJson: Record<string, unknown>) {
  return {
    id,
    name,
    connector_type_id: 'rest',
    tier: 'native',
    status: 'active',
    config_json: configJson,
  }
}

function mountView() {
  return mount(AdminConnectorsView, {
    global: {
      stubs: {
        LoadingSpinner: true,
        ErrorAlert: true,
        FeatureGate: { template: '<div><slot /></div>' },
      },
    },
  })
}

async function openEdit(wrapper: Awaited<ReturnType<typeof mountView>>, connectorId: string) {
  const row = wrapper.find(`[data-testid="connector-row-${connectorId}"]`)
  // TableActions renders Edit first, then Delete.
  await row.findAll('button')[0].trigger('click')
  await nextTick()
}

function patchBody(): { config_json: Record<string, unknown>; credentials?: string } | undefined {
  const init = mockPatch.mock.calls[0]?.[1] as
    | { body?: { config_json?: Record<string, unknown>; credentials?: string } }
    | undefined
  return init?.body as { config_json: Record<string, unknown>; credentials?: string } | undefined
}

describe('AdminConnectorsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminConnectorsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Connectors')
  })

  it('segregates preview connectors into a disclosure section and hides in-dev connectors', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'native-1', name: 'Native Connector', connector_type: 'postgresql', description: null, tier: 'native' },
          { id: 'preview-1', name: 'Preview Connector', connector_type: 'http', description: null, tier: 'preview' },
          { id: 'indev-1', name: 'InDev Connector', connector_type: 'http', description: null, tier: 'in_dev' },
        ],
      },
      error: undefined,
    })

    const wrapper = mount(AdminConnectorsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Native Connector')
    expect(wrapper.text()).not.toContain('InDev Connector')

    const previewSection = wrapper.find('[data-testid="connectors-preview-section"]')
    expect(previewSection.exists()).toBe(true)
    expect(previewSection.text()).toContain('Preview Connector')
  })

  it('sends the edited description in the PATCH body instead of the stale stored snapshot', async () => {
    // The stored description is a first-class form control, NOT a key inside
    // the advanced JSON editor. prefillRestConfig must not snapshot it into
    // advanced_json, or buildRestConfig would stomp the user's fresh edit with
    // the stale snapshot on save (FAR-466 QA fix 2).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            description: 'Stale description',
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    const advancedJson = wrapper.find('[data-testid="rest-connector-advanced-json"]')
    expect((advancedJson.element as HTMLTextAreaElement).value).not.toContain('Stale description')

    await wrapper.find('[data-testid="admin-connectors-edit-description"]').setValue('Fresh description')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const body = patchBody()
    expect(body?.config_json.description).toBe('Fresh description')
  })

  it('never lets an advanced-JSON entry override a validated flat field on save', async () => {
    // buildRestConfig must apply the advanced-JSON keys FIRST and the
    // validated flat fields ON TOP: a hand-typed advanced-JSON entry for a flat
    // field is unvalidated and must lose to the value the form just validated
    // (FAR-466 QA fix 3).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    await wrapper.find('[data-testid="rest-connector-advanced-json"]').setValue(JSON.stringify({
      base_url: 'https://evil.example.com',
      method: 'DELETE',
      on_unknown: 'off',
      timeout_seconds: 999,
      path: '/v2/items',
    }))
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const body = patchBody()
    expect(body?.config_json.base_url).toBe('https://api.example.com')
    expect(body?.config_json.method).toBe('GET')
    expect(body?.config_json.on_unknown).toBe('fail_open')
    expect(body?.config_json.timeout_seconds).toBe(30)
    // Non-flat advanced keys still survive the round-trip.
    expect(body?.config_json.path).toBe('/v2/items')
  })

  it('still sends the parsed allowed_hosts array when the allowlist is edited on a REST connector', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            allowed_hosts: ['api.example.com'],
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    await wrapper.find('[data-testid="rest-connector-allowed-hosts"]').setValue('host-a.com, host-b.com')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    expect(patchBody()?.config_json.allowed_hosts).toEqual(['host-a.com', 'host-b.com'])
  })

  it('sends an explicit empty allowed_hosts array so clearing the allowlist persists', async () => {
    // The backend PATCH config merge only overrides keys PRESENT in the
    // payload. Omitting allowed_hosts when the field is cleared would silently
    // keep the stored egress allowlist, making the restriction unremovable
    // from the form. buildRestConfig must ALWAYS send the parsed array — []
    // when cleared (FAR-466 QA fix).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            allowed_hosts: ['api.example.com', 'cdn.example.com'],
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    // The stored allowlist is prefilled, then cleared by the admin.
    const hostsField = wrapper.find('[data-testid="rest-connector-allowed-hosts"]')
    expect((hostsField.element as HTMLInputElement).value).toBe('api.example.com, cdn.example.com')
    await hostsField.setValue('')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    expect(patchBody()?.config_json.allowed_hosts).toEqual([])
  })

  it('remounts the edit form per target so switching A to B drops the stale baselines', async () => {
    // Without a :key on the edit block, switching Edit from connector A to B
    // reuses the component instance: B's form inherits A's onMounted baselines,
    // so a spurious modeChanged demands B's secret re-entry (or B's stored
    // credential is silently overwritten via a credentials re-send). Each
    // target must mount fresh (FAR-466 QA fix 4).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-a', 'Connector A', {
            base_url: 'https://a.example.com',
            auth_mode: 'api_key',
            in: 'header',
            header_name: 'X-API-Key',
          }),
          restConnectorItem('rest-b', 'Connector B', {
            base_url: 'https://b.example.com',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-a')
    await openEdit(wrapper, 'rest-b')

    // B's UNTOUCHED prefill must save cleanly: validate() passes without
    // demanding a re-entered secret (no spurious modeChanged from A's stale
    // baseline) and no credentials payload is re-sent.
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const init = mockPatch.mock.calls[0]?.[1] as {
      params: { path: { connector_id: string } }
      body?: { config_json?: Record<string, unknown>; credentials?: string }
    }
    expect(init.params.path.connector_id).toBe('rest-b')
    expect(init.body?.config_json?.auth_mode).toBe('bearer')
    expect(init.body?.credentials).toBeUndefined()
    expect(wrapper.text()).not.toContain('Please fix')
  })


  it('preserves a stored non-lowercase on_unknown value on edit-save (no silent downgrade)', async () => {
    // A stored 'FAIL_CLOSED' must match case-insensitively and be normalised on
    // prefill: pre-fix it fell through to the fail_open default and the next
    // save silently downgraded the stored policy (FAR-532).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'FAIL_CLOSED',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    const select = wrapper.find('[data-testid="rest-connector-on-unknown"]')
    expect((select.element as HTMLSelectElement).value).toBe('fail_closed')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    expect(patchBody()?.config_json.on_unknown).toBe('fail_closed')
  })

  it('preserves unknown config_json keys on a REST config-only edit round-trip (FAR-466 / FAR-504)', async () => {
    // A REST connector whose stored config carries GENUINELY UNKNOWN keys (not
    // surfaced as first-class form controls). The edit form must snapshot them
    // back into the JSON editor (prefillRestConfig -> advanced_json) and re-merge
    // them into the PATCH body's config_json (buildRestConfig), so an
    // edit-save never silently drops config (no data loss on edit).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            auth_mode: 'bearer',
            records_path: '',
            custom_unknown: { nested: true },
            custom_str: 'keep-me',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    // Unknown keys are snapshotted into the advanced JSON editor on prefill...
    const advancedJson = wrapper.find('[data-testid="rest-connector-advanced-json"]')
    expect((advancedJson.element as HTMLTextAreaElement).value).toContain('custom_unknown')

    // ...and a config-only save re-merges them into the PATCH config_json.
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const body = patchBody()
    // Unknown / legacy keys preserved through the round-trip.
    expect(body?.config_json.custom_unknown).toEqual({ nested: true })
    expect(body?.config_json.custom_str).toBe('keep-me')
    // First-class control keys also survive.
    expect(body?.config_json.base_url).toBe('https://api.example.com')
    expect(body?.config_json.method).toBe('GET')
  })

  it('preserves a stored non-number timeout_seconds instead of silently resetting it to 30', async () => {
    // A stored string "45" must pass through prefill verbatim (the backend
    // coerces numerics); pre-fix the typeof check reset it to 30 on edit-save
    // (FAR-532).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: '45',
            verify_tls: true,
            on_unknown: 'fail_open',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    const timeoutField = wrapper.find('[data-testid="rest-connector-timeout"]')
    expect((timeoutField.element as HTMLInputElement).value).toBe('45')
    await wrapper.find('form').trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    expect(patchBody()?.config_json.timeout_seconds).toBe(45)
  })

  it('shows the legacy auth-echo hint when the stored config has no auth_mode echo', async () => {
    // Rows stored before the config echo carry no auth_mode in config_json; the
    // bearer default may not match the stored credential, so the form must
    // surface an explicit hint instead of silently defaulting (FAR-532).
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-legacy', 'Legacy Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-legacy')
    const hint = wrapper.find('[data-testid="rest-connector-legacy-auth-hint"]')
    expect(hint.exists()).toBe(true)
    expect(hint.text()).toContain('may not match the stored credential')
  })

  it('hides the legacy auth-echo hint when the auth_mode echo is present', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          restConnectorItem('rest-1', 'REST Connector', {
            base_url: 'https://api.example.com',
            method: 'GET',
            timeout_seconds: 30,
            verify_tls: true,
            on_unknown: 'fail_open',
            auth_mode: 'bearer',
          }),
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await openEdit(wrapper, 'rest-1')
    expect(wrapper.find('[data-testid="rest-connector-legacy-auth-hint"]').exists()).toBe(false)
  })
})

describe('AdminConnectorsView — FAR-617 add/delete/error-state coverage', () => {
  // ErrorAlert is captured (not stubbed blind) so load-failure + retry flows are assertable.
  const capturingErrorAlert = {
    props: ['message', 'onRetry'],
    template:
      '<div data-testid="error-alert"><button type="button" data-testid="error-retry" @click="onRetry && onRetry()">Retry</button>{{ message }}</div>',
  }

  function mountWithCapturingError() {
    return mount(AdminConnectorsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: capturingErrorAlert,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: { items: [] }, error: undefined })
    mockPost.mockResolvedValue({ data: null, error: undefined })
    mockPatch.mockResolvedValue({ data: null, error: undefined })
    mockDelete.mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
  })

  it('shows the loading spinner while the initial GET is in flight', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(AdminConnectorsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.find('loading-spinner-stub').exists()).toBe(true)
    wrapper.unmount()
  })

  it('a failed connectors load renders the inline ErrorAlert and Retry re-fetches', async () => {
    // Key the rejection to the connectors endpoint — planStore.fetchPlan also
    // GETs on mount and must not swallow the rejection.
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/connectors') throw new Error('connectors backend down')
      return { data: { items: [] }, error: undefined }
    })
    const wrapper = mountWithCapturingError()
    await nextTick()
    await nextTick()

    const alert = wrapper.find('[data-testid="error-alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('connectors backend down')

    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/connectors') {
        return { data: { items: [{ id: 'c1', name: 'Recovered', connector_type: 'postgresql', description: null, tier: 'native' }] }, error: undefined }
      }
      return { data: { items: [] }, error: undefined }
    })
    await wrapper.find('[data-testid="error-retry"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Recovered')
  })

  it('empty list renders the no-connectors empty state', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('No connectors configured')
    expect(wrapper.find('table').exists()).toBe(false)
  })

  it('add flow (generic connector): form opens, POST carries the JSON config, and the row is appended', async () => {
    mockGet.mockResolvedValue({ data: { items: [] }, error: undefined })
    mockPost.mockResolvedValue({
      data: {
        id: 'pg-new',
        name: 'Postgres Prod',
        connector_type_id: 'postgresql',
        config_json: { description: 'Primary DB' },
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="admin-connectors-add"]').trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('New Connector')

    // Submit button is disabled while the name is empty.
    expect(wrapper.find('[data-testid="admin-connectors-submit"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-testid="admin-connectors-name-input"]').setValue('Postgres Prod')
    await wrapper.find('[data-testid="admin-connectors-description-input"]').setValue('Primary DB')
    await wrapper.find('[data-testid="admin-connectors-config-input"]').setValue('{ "host": "db.local" }')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPost).toHaveBeenCalledTimes(1)
    const [url, options] = mockPost.mock.calls[0]
    expect(url).toBe('/api/v1/connectors')
    expect(options.body).toEqual({
      name: 'Postgres Prod',
      connector_type_id: '',
      credentials: '{ "host": "db.local" }',
      config_json: { description: 'Primary DB' },
      allowed_operations: [],
      visibility: 'org',
      tier: 'native',
    })

    // Row appended, form closed.
    expect(wrapper.find('[data-testid="connector-row-pg-new"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('New Connector')
  })

  it('add flow (REST connector): structured fields build the config_json and credentials payloads', async () => {
    mockPost.mockResolvedValue({
      data: { id: 'rest-new', name: 'REST New', connector_type_id: 'rest', config_json: {} },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="admin-connectors-add"]').trigger('click')
    await nextTick()
    ;(wrapper.vm as unknown as { formData: { connector_type: string } }).formData.connector_type = 'rest'
    await nextTick()

    await wrapper.find('[data-testid="admin-connectors-name-input"]').setValue('REST New')
    await wrapper.find('[data-testid="rest-connector-base-url"]').setValue('https://api.example.com')
    await wrapper.find('[data-testid="rest-connector-token"]').setValue('tok_123')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPost).toHaveBeenCalledTimes(1)
    const [, options] = mockPost.mock.calls[0]
    expect(options.body.config_json.base_url).toBe('https://api.example.com')
    expect(options.body.config_json.auth_mode).toBe('bearer')
    expect(JSON.parse(options.body.credentials)).toEqual({ auth_mode: 'bearer', token: 'tok_123' })
  })

  it('add flow: a POST error payload surfaces the formatted error and keeps the form open', async () => {
    mockPost.mockResolvedValue({ data: null, error: { detail: 'name already exists' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="admin-connectors-add"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-connectors-name-input"]').setValue('Dup')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('name already exists')
    expect(wrapper.find('[data-testid="admin-connectors-name-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="connector-row-dup"]').exists()).toBe(false)
  })

  it('add flow: cancel closes the form without calling the API', async () => {
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="admin-connectors-add"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-connectors-cancel"]').trigger('click')
    await nextTick()

    expect(mockPost).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="admin-connectors-name-input"]').exists()).toBe(false)
  })

  it('delete flow: confirm deletes via the API and removes the row', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'c1', name: 'Doomed Connector', connector_type: 'postgresql', description: null, tier: 'native' },
        ],
      },
      error: undefined,
    })
    mockDelete.mockResolvedValue({ response: { status: 204, ok: true }, error: undefined })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    const row = wrapper.find('[data-testid="connector-row-c1"]')
    // TableActions renders Edit first, then Delete.
    await row.findAll('button')[1].trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('This action cannot be undone')

    await wrapper.find('[data-testid="admin-connectors-delete-confirm"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(mockDelete).toHaveBeenCalledTimes(1)
    const [url, options] = mockDelete.mock.calls[0]
    expect(url).toBe('/api/v1/connectors/{connector_id}')
    expect(options.params.path.connector_id).toBe('c1')
    expect(wrapper.find('[data-testid="connector-row-c1"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('No connectors configured')
  })

  it('delete flow: cancel dismisses the confirm panel without calling the API', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'c1', name: 'Kept Connector', connector_type: 'postgresql', description: null, tier: 'native' },
        ],
      },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="connector-row-c1"]').findAll('button')[1].trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-connectors-delete-cancel"]').trigger('click')
    await nextTick()

    expect(mockDelete).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="connector-row-c1"]').exists()).toBe(true)
  })

  it('delete flow: a DELETE error surfaces the delete error without removing the row', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'c1', name: 'Stuck Connector', connector_type: 'postgresql', description: null, tier: 'native' },
        ],
      },
      error: undefined,
    })
    mockDelete.mockResolvedValue({ response: { status: 409, ok: false }, error: { detail: 'connector in use' } })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="connector-row-c1"]').findAll('button')[1].trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="admin-connectors-delete-confirm"]').trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('connector in use')
    expect(wrapper.find('[data-testid="connector-row-c1"]').exists()).toBe(true)
  })

  it('edit flow (non-REST connector): PATCH sends name, null credentials and the description config', async () => {
    mockGet.mockResolvedValue({
      data: {
        items: [
          { id: 'pg-1', name: 'PG Old', connector_type: 'postgresql', description: 'old desc', tier: 'native' },
        ],
      },
      error: undefined,
    })
    mockPatch.mockResolvedValue({
      data: { id: 'pg-1', name: 'PG New', connector_type_id: 'postgresql', config_json: { description: 'new desc' } },
      error: undefined,
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    await wrapper.find('[data-testid="connector-row-pg-1"]').findAll('button')[0].trigger('click')
    await nextTick()
    // Non-REST targets use the JSON config textarea, not the REST structured form.
    expect(wrapper.find('[data-testid="admin-connectors-edit-config"]').exists()).toBe(true)

    await wrapper.find('[data-testid="admin-connectors-edit-name"]').setValue('PG New')
    await wrapper.find('[data-testid="admin-connectors-edit-description"]').setValue('new desc')
    await wrapper.find('form').trigger('submit')
    await nextTick()
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const [url, options] = mockPatch.mock.calls[0]
    expect(url).toBe('/api/v1/connectors/{connector_id}')
    expect(options.params.path.connector_id).toBe('pg-1')
    expect(options.body).toEqual({
      name: 'PG New',
      credentials: null,
      config_json: { description: 'new desc' },
    })
    // The edited row is refreshed in place and the edit form closes.
    expect(wrapper.text()).toContain('PG New')
    expect(wrapper.find('[data-testid="admin-connectors-edit-name"]').exists()).toBe(false)
  })

  it('connector types are fetched on mount for the add-form dropdown', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/connectors/types') {
        return { data: { items: [{ id: 'postgresql', display_name: 'PostgreSQL' }] }, error: undefined }
      }
      return { data: { items: [] }, error: undefined }
    })
    const wrapper = mountView()
    await nextTick()
    await nextTick()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/connectors/types')
    const vm = wrapper.vm as unknown as { connectorTypes: Array<{ id: string }> }
    expect(vm.connectorTypes).toEqual([{ id: 'postgresql', display_name: 'PostgreSQL' }])
  })
})
