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

  it('preserves unknown config_json keys on a REST config-only edit round-trip (FAR-466 / FAR-504)', async () => {
    // A REST connector whose stored config carries GENUINELY UNKNOWN keys (not
    // surfaced as first-class form controls). The edit form must snapshot them
    // back into the JSON editor (prefillRestConfig -> advanced_json) and re-merge
    // them into the PATCH body's config_json (buildRestConfig), so an
    // edit-save never silently drops config (no data loss on edit).
    mockGet.mockResolvedValue({
      data: {
        items: [
          {
            id: 'rest-1',
            name: 'REST Connector',
            connector_type_id: 'rest',
            description: 'desc',
            tier: 'native',
            status: 'active',
            config_json: {
              description: 'desc',
              base_url: 'https://api.example.com',
              method: 'GET',
              timeout_seconds: 30,
              verify_tls: true,
              on_unknown: 'fail_open',
              records_path: '',
              custom_unknown: { nested: true },
              custom_str: 'keep-me',
            },
          },
        ],
      },
      error: undefined,
    })
    mockPatch.mockImplementation((_url: string, opts: { body?: Record<string, unknown> } | undefined) =>
      Promise.resolve({ data: { ...(opts?.body ?? {}), id: 'rest-1', connector_type_id: 'rest' }, error: undefined }),
    )

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

    const editBtn = wrapper.findAll('button').find(b => b.text() === 'Edit')
    expect(editBtn).toBeTruthy()
    await editBtn!.trigger('click')
    await nextTick()

    const saveBtn = wrapper.find('[data-testid="admin-connectors-save"]')
    expect(saveBtn.exists()).toBe(true)

    const editForm = wrapper.findAll('form').find(f => f.find('[data-testid="admin-connectors-save"]').exists())
    expect(editForm).toBeTruthy()
    await editForm!.trigger('submit')
    await nextTick()

    expect(mockPatch).toHaveBeenCalledTimes(1)
    const patchBody = mockPatch.mock.calls[0][1].body as Record<string, unknown>
    const cfg = patchBody.config_json as Record<string, unknown>
    // Unknown / legacy keys preserved through the round-trip.
    expect(cfg.custom_unknown).toEqual({ nested: true })
    expect(cfg.custom_str).toBe('keep-me')
    // First-class control keys also survive.
    expect(cfg.base_url).toBe('https://api.example.com')
    expect(cfg.method).toBe('GET')
  })
})
