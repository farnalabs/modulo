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
import LifecycleMapRenderer from '../components/lifecycle-map/LifecycleMapRenderer.vue'
import { usePlanStore } from '../stores/planStore'
import { useLifecycleMapsStore } from '../stores/lifecycleMaps'

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

const journeyItem = {
  kind: 'run',
  ref: 'run-1',
  canonical_work_item_id: 'run-1',
  current_stage: null,
  status: 'complete',
  provenance: 'execution',
  run_count: 3,
  unattributed: true,
  latest_run_id: 'run-1',
  updated_at: '2026-01-02T00:00:00Z',
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

let fetchMock: ReturnType<typeof vi.fn>
let journeysResponse: { items: unknown[]; next_cursor?: string | null }

// FAR-654: seed the plan store directly so the view never fires a plan fetch
// against the generic fetch stub. Flags default to OFF (absent = disabled).
function seedPlan(flags: Record<string, boolean> = {}) {
  const planStore = usePlanStore()
  planStore.features = flags
  planStore.loaded = true
}

function journeysEndpointCalled(): boolean {
  return fetchMock.mock.calls.some((call) => String(call[0]).includes('/journeys'))
}

beforeEach(() => {
  setActivePinia(createPinia())
  routerPushMock.mockClear()
  seedPlan()
  journeysResponse = { items: [] }
  fetchMock = vi.fn((url: string) => {
    if (url.includes('/journeys')) return Promise.resolve(okJson(journeysResponse))
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
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

function mountView() {
  return mount(LifecycleMapView, {
    global: {
      plugins: [i18n],
      mocks: {
        $router: { push: routerPushMock },
      },
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
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

describe('LifecycleMapView responsive layout (FAR-640)', () => {
  it('renders the lifecycle controls inside the PageHeader right slot', async () => {
    const wrapper = mountView()
    await flushPromises()

    const exportBtn = wrapper.find('[data-testid="lifecycle-map-export"]')
    const editBtn = wrapper.find('[data-testid="lifecycle-map-view-edit"]')
    const deleteBtn = wrapper.find('[data-testid="lifecycle-map-view-delete"]')
    expect(exportBtn.exists()).toBe(true)
    expect(editBtn.exists()).toBe(true)
    expect(deleteBtn.exists()).toBe(true)

    // PageHeader and the view's outer band both render <header>; closest()
    // resolves to PageHeader's own header, which must own all controls.
    const headerEl = exportBtn.element.closest('header')
    expect(headerEl).not.toBeNull()
    expect(headerEl?.querySelector('h1')?.textContent).toBe('Launch Flow')
    expect(headerEl?.contains(editBtn.element)).toBe(true)
    expect(headerEl?.contains(deleteBtn.element)).toBe(true)

    const rightSlot = exportBtn.element.parentElement
    expect(rightSlot).not.toBeNull()
    expect(rightSlot?.contains(editBtn.element)).toBe(true)
    expect(rightSlot?.textContent).toContain('v1')
  })

  it('routes back to the lifecycle maps list via the PageHeader back button', async () => {
    const wrapper = mountView()
    await flushPromises()

    const backBtn = wrapper.findAll('button').find(b => b.text().includes('Back'))
    expect(backBtn).toBeTruthy()

    await backBtn!.trigger('click')
    expect(routerPushMock).toHaveBeenCalledWith('/lifecycle-maps')
  })

  it('does not hand-roll the responsive header container at page level', async () => {
    const wrapper = mountView()
    await flushPromises()

    const bandWrapper = wrapper.find('header > div')
    expect(bandWrapper.exists()).toBe(true)
    const classes = bandWrapper.classes()
    expect(classes).not.toContain('flex-col')
    expect(classes).not.toContain('sm:flex-row')
    expect(classes).not.toContain('sm:items-center')
    expect(classes).not.toContain('sm:justify-between')
  })
})

describe('LifecycleMapView journey flag gating (FAR-654)', () => {
  it('flag off (default): makes no journeys API call and renders no journey UI', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(false)

    // The map (stages/edges) still renders; the renderer receives an empty
    // journeys list so no JourneyCards render on nodes.
    const renderer = wrapper.findComponent(LifecycleMapRenderer)
    expect(renderer.exists()).toBe(true)
    expect(renderer.props('journeys')).toEqual([])

    expect(wrapper.find('[aria-label="Unattributed journeys"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="lifecycle-map-journeys-pagination"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Journey details"]').exists()).toBe(false)
  })

  it('flag off hides stale journey state left by an earlier session', async () => {
    // Simulate a previous visit with the flag on: the singleton store still
    // holds journeys, a pagination cursor, and a selected journey.
    const store = useLifecycleMapsStore()
    store.journeys = [journeyItem]
    store.journeysCursor = 'cursor-2'
    store.selectedJourneyKey = 'run:run-1'

    const wrapper = mountView()
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(false)
    const renderer = wrapper.findComponent(LifecycleMapRenderer)
    expect(renderer.props('journeys')).toEqual([])
    expect(wrapper.find('[aria-label="Unattributed journeys"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="lifecycle-map-journeys-pagination"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Journey details"]').exists()).toBe(false)
  })

  it('flag on: fetches journeys and renders the unattributed section and pagination', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    journeysResponse = { items: [journeyItem], next_cursor: 'cursor-2' }

    const wrapper = mountView()
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(true)

    const renderer = wrapper.findComponent(LifecycleMapRenderer)
    expect(renderer.exists()).toBe(true)
    expect(renderer.props('journeys')).toHaveLength(1)

    expect(wrapper.find('[aria-label="Unattributed journeys"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="lifecycle-map-journeys-pagination"]').exists()).toBe(true)
  })

  it('flag on: renders the journey detail panel when a journey is selected', async () => {
    seedPlan({ lifecycle_map_journeys: true })
    const wrapper = mountView()
    await flushPromises()

    const store = useLifecycleMapsStore()
    store.selectedJourneyKey = 'run:run-1'
    await flushPromises()

    expect(wrapper.find('[aria-label="Journey details"]').exists()).toBe(true)
  })

  it('flag flipping on after mount triggers the journeys fetch', async () => {
    mountView()
    await flushPromises()
    expect(journeysEndpointCalled()).toBe(false)

    seedPlan({ lifecycle_map_journeys: true })
    await flushPromises()

    expect(journeysEndpointCalled()).toBe(true)
  })
})
