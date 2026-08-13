import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createI18n } from 'vue-i18n'

vi.mock('vue-router', () => ({
  useRoute: vi.fn(() => ({ params: { id: 'map-1' }, query: {}, meta: {}, name: 'lifecycle-map-detail' })),
  useRouter: vi.fn(() => ({ push: routerPushMock })),
}))

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

vi.mock('../lib/api/formatError', () => ({
  formatApiError: (e: unknown) => (e instanceof Error ? e.message : 'Request failed'),
}))

import LifecycleMapView from '../views/lifecycle-map/LifecycleMapView.vue'

const routerPushMock = vi.fn()

function okJson(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

const mapDetail = {
  id: 'map-1',
  name: 'Launch Flow',
  description: null,
  owner: null,
  owner_team_id: null,
  stages: [{ id: 'stage-1', name: 'Build', description: null, type: 'modulo', owner_badge: null, graduated: false, pipeline_id: null, external_url: null }],
  transitions: [],
  versions: [{ version: 1, created_at: '2026-01-01T00:00:00Z', created_by: null }],
  current_version: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        LifecycleMapView: {
          edit: 'Edit',
          version_label: 'Version:',
          export_map: 'Export',
          exporting: 'Exporting...',
          import_map: 'Import',
          import_dialog_title: 'Import Lifecycle Map',
          import_paste_hint: 'Paste an exported lifecycle map (JSON) to create a new map in this organisation.',
          import_placeholder: 'Paste the exported lifecycle map JSON here',
          import_payload_label: 'Lifecycle map export JSON',
          import_invalid_json: 'The pasted content is not valid JSON.',
          importing: 'Importing...',
          cancel: 'Cancel',
        },
      },
    },
  },
})

beforeEach(() => {
  setActivePinia(createPinia())
  routerPushMock.mockClear()
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson({ items: [] }))
    if (url.includes('/export')) {
      return Promise.resolve(okJson({
        primitive_type: 'lifecycle_map',
        format_version: '1',
        name: 'Launch Flow',
        description: null,
        content_json: { stages: [{ id: 'stage-1', name: 'Build', type: 'manual' }], edges: [] },
      }))
    }
    return Promise.resolve(okJson(mapDetail))
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function mountView() {
  return mount(LifecycleMapView, {
    global: {
      plugins: [i18n],
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        PageHeader: true,
        ErrorAlert: true,
        LifecycleMapRenderer: true,
        JourneyCard: true,
        ProvenanceBadge: true,
      },
    },
  })
}

describe('LifecycleMapView', () => {
  it('renders an Edit button that routes to the editor', async () => {
    const wrapper = mountView()
    await flushPromises()

    const editBtn = wrapper.find('[data-testid="lifecycle-map-view-edit"]')
    expect(editBtn.exists()).toBe(true)

    await editBtn.trigger('click')
    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-editor', params: { id: 'map-1' } })
  })

  it('renders an Export button that downloads the envelope and copies to clipboard', async () => {
    const urlCreate = vi.fn(() => 'blob:fake-url')
    const urlRevoke = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL: urlCreate, revokeObjectURL: urlRevoke })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const writeText = vi.fn(async () => undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })

    const wrapper = mountView()
    await flushPromises()

    const exportBtn = wrapper.find('[data-testid="lifecycle-map-export"]')
    expect(exportBtn.exists()).toBe(true)

    await exportBtn.trigger('click')
    await flushPromises()

    expect(urlCreate).toHaveBeenCalled()
    expect(clickSpy).toHaveBeenCalled()
    expect(urlRevoke).toHaveBeenCalled()
    expect(writeText).toHaveBeenCalled()
    const writeTextMock = writeText.mock as unknown as { calls: Array<[string]> }
    const json = writeTextMock.calls[0][0] as string
    const envelope = JSON.parse(json)
    expect(envelope.primitive_type).toBe('lifecycle_map')
    expect(envelope.content_json.stages).toBeDefined()

    clickSpy.mockRestore()
  })

  it('opens the Import dialog, imports a valid envelope, and navigates to the new map', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    // reka-ui Dialog teleports content to document.body.
    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement | null
    expect(payload).not.toBeNull()
    payload!.value = JSON.stringify({
      primitive_type: 'lifecycle_map',
      format_version: '1',
      name: 'Imported SDLC',
      content_json: { stages: [], edges: [] },
    })
    await payload!.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement | null
    expect(confirmBtn).not.toBeNull()
    await confirmBtn!.click()
    await flushPromises()

    expect(routerPushMock).toHaveBeenCalledWith({ name: 'lifecycle-map-detail', params: { id: 'map-1' } })
  })

  it('shows a validation error for non-JSON import payloads', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.find('[data-testid="lifecycle-map-import"]').trigger('click')
    await flushPromises()

    // reka-ui Dialog teleports content to document.body.
    const payload = document.body.querySelector('[data-testid="lifecycle-map-import-payload"]') as HTMLTextAreaElement | null
    expect(payload).not.toBeNull()
    payload!.value = 'not-json{'
    await payload!.dispatchEvent(new Event('input'))
    await flushPromises()

    const confirmBtn = document.body.querySelector('[data-testid="lifecycle-map-import-confirm"]') as HTMLButtonElement | null
    expect(confirmBtn).not.toBeNull()
    await confirmBtn!.click()
    await flushPromises()

    expect(document.body.textContent).toContain('The pasted content is not valid JSON.')
  })
})
