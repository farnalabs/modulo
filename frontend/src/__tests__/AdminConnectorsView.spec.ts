import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick as vueNextTick } from 'vue'

async function nextTick() { await vueNextTick(); await flushPromises() }

const { mockGet, mockPatch } = vi.hoisted(() => ({
  mockGet: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
  mockPatch: vi.fn().mockResolvedValue({ data: null, error: undefined }),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: mockPatch,
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
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
})
