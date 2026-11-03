import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockRoute = vi.hoisted(() => ({ params: { id: 'map-1' } as Record<string, string>, query: {} as Record<string, string> }))
const mockIsOperator = vi.hoisted(() => ({ value: true }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
const mockPlanStore = vi.hoisted(() => ({
  loaded: true,
  currentTier: 'community',
  devMode: true,
  userPermissions: ['system_maps_access', 'system.config.manage'],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet, POST: mockPost },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))
vi.mock('vue-router', () => ({
  useRoute: () => mockRoute,
  useRouter: () => ({ push: vi.fn() }),
  RouterLink: { template: '<a><slot /></a>' },
}))

import SystemMapDetailView from '../views/SystemMapDetailView.vue'
import { useSystemMapsStore } from '../stores/systemMaps'
import enUS from '../locales/en-US.js'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

// PrimeVue's Dialog teleports its body to <body>; this stub keeps the confirm
// control inside the tree so the operator's explicit restore action is
// reachable without asserting on teleported DOM.
const FormDialogStub = {
  name: 'FormDialog',
  props: ['open', 'title', 'description', 'confirmText', 'loading', 'confirmDisabled'],
  emits: ['confirm'],
  template: '<div v-if="open"><p data-testid="form-dialog-description">{{ description }}</p><button data-testid="form-dialog-confirm" @click="$emit(\'confirm\')">{{ confirmText }}</button></div>',
}

const HeaderStub = {
  name: 'HeaderBar',
  props: ['map', 'revision', 'saving', 'readOnly'],
  emits: ['toggle-sidebar', 'save', 'publish', 'archive', 'discover'],
  template: '<div data-testid="header-stub" />',
}
const GraphStub = {
  name: 'GraphEditor',
  props: ['nodes', 'relationships', 'loopConfigs', 'nodeTypes', 'relationshipTypes', 'readOnly'],
  emits: ['nodes-change', 'relationships-change'],
  template: '<div data-testid="graph-stub" />',
}

function mountView() {
  return mount(SystemMapDetailView, {
    global: {
      plugins: [i18n],
      stubs: {
        BackLink: true,
        HeaderBar: HeaderStub,
        GraphEditor: GraphStub,
        DiscoveryPanel: true,
        FormDialog: FormDialogStub,
      },
    },
  })
}

function revision(n: number, status = 'published', extra: Record<string, unknown> = {}) {
  const day = String(n).padStart(2, '0')
  return {
    id: `rev-${n}`,
    organisation_id: 'org-1',
    system_map_id: 'map-1',
    revision_number: n,
    status,
    content_json: {},
    published_at: status === 'draft' ? null : `2026-01-${day}T10:00:00Z`,
    effective_from: status === 'draft' ? null : `2026-01-${day}T10:00:00Z`,
    effective_to: null,
    deleted_at: null,
    deleted_by: null,
    created_at: `2026-01-${day}T09:00:00Z`,
    updated_at: `2026-01-${day}T09:00:00Z`,
    ...extra,
  }
}

function graphFor(revisionId: string) {
  const number = Number(revisionId.replace('rev-', ''))
  return { revision: revision(number), nodes: [], relationships: [], loop_configs: [] }
}

type ApiResult = { data?: unknown; error?: unknown; response?: unknown }

interface Routes {
  map: ApiResult
  history: () => ApiResult
  current: ApiResult
  byTime: ApiResult
  diff: ApiResult
  restore: ApiResult
  revisions: Record<string, ApiResult>
}

const CURRENT_REVISION_ID = 'rev-11'

function historyPayload(numbers: number[], currentId = CURRENT_REVISION_ID): ApiResult {
  return {
    data: {
      system_map_id: 'map-1',
      revisions: numbers.map((n) => ({ revision: revision(n), is_current: `rev-${n}` === currentId })),
    },
  }
}

let routes: Routes

function defaultRoutes(numbers: number[]): Routes {
  return {
    map: { data: { id: 'map-1', name: 'Map one', published_revision_id: CURRENT_REVISION_ID } },
    history: () => historyPayload(numbers),
    current: { data: { revision: revision(11) } },
    byTime: { data: { revision: revision(5), requested_at: '2026-02-01T10:00:00Z', matched: true } },
    diff: {
      data: {
        base_revision_id: 'rev-11',
        base_revision_number: 11,
        target_revision_id: 'rev-9',
        target_revision_number: 9,
        nodes_added: ['agent:new'],
        nodes_removed: ['agent:gone'],
        nodes_modified: ['pipeline:main'],
        relationships_added: ['a->b:flow'],
        relationships_removed: ['b->c:data'],
        relationships_modified: ['c->d:trigger'],
        loop_configs_added: ['loop:new'],
        loop_configs_removed: ['loop:gone'],
        loop_configs_modified: ['loop:main'],
        content_json_changed: true,
      },
    },
    restore: { data: { new_revision: revision(13, 'draft'), source_revision_id: 'rev-9', source_revision_number: 9 } },
    revisions: {},
  }
}

function installRoutes(next: Routes): void {
  routes = next
  mockGet.mockImplementation((path: string, options?: { params?: { path?: Record<string, string> } }) => {
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    }
    if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve(routes.map)
    if (path === '/api/v1/system-maps/{map_id}/revisions/history') return Promise.resolve(routes.history())
    if (path === '/api/v1/system-maps/{map_id}/revisions/current') return Promise.resolve(routes.current)
    if (path === '/api/v1/system-maps/{map_id}/revisions/by-time') return Promise.resolve(routes.byTime)
    if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}') {
      const revisionId = options?.params?.path?.rev_id ?? ''
      const override = routes.revisions[revisionId]
      return Promise.resolve(override ?? { data: graphFor(revisionId) })
    }
    return Promise.resolve({ data: null })
  })
  mockPost.mockImplementation((path: string) => {
    if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}/diff') return Promise.resolve(routes.diff)
    if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}/restore-as-new') return Promise.resolve(routes.restore)
    if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}/publish') return Promise.resolve({ data: {} })
    return Promise.resolve({ data: null })
  })
}

function twelveRevisions(): number[] {
  return Array.from({ length: 12 }, (_, index) => index + 1)
}

describe('SystemMap revision history, point-in-time and restore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockRoute.params = { id: 'map-1' }
    mockRoute.query = {}
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    installRoutes(defaultRoutes(twelveRevisions()))
  })

  it('loads the real revision history, marks the current revision and paginates it', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/history', {
      params: { path: { map_id: 'map-1' } },
    })

    const store = useSystemMapsStore()
    expect(store.revisionHistory).toHaveLength(12)
    expect(store.revisionHistoryPageRevisions.map((item) => item.revision_number)).toEqual([12, 11, 10, 9, 8, 7, 6, 5, 4, 3])
    expect(wrapper.find('[data-testid="system-map-history-page-status"]').text()).toContain('Page 1 of 2')
    expect(wrapper.find('[data-testid="system-map-history-prev"]').attributes('disabled')).toBeDefined()

    const currentRow = wrapper.get('[data-testid="system-map-revision-11"]')
    expect(currentRow.find('[data-testid="system-map-revision-current-badge"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-revision-12"] [data-testid="system-map-revision-current-badge"]').exists()).toBe(false)

    await wrapper.get('[data-testid="system-map-history-next"]').trigger('click')
    await flushPromises()

    expect(store.revisionHistoryPage).toBe(2)
    expect(store.revisionHistoryPageRevisions.map((item) => item.revision_number)).toEqual([2, 1])
    expect(wrapper.find('[data-testid="system-map-history-page-status"]').text()).toContain('Page 2 of 2')
    expect(wrapper.find('[data-testid="system-map-history-next"]').attributes('disabled')).toBeDefined()
  })

  it('shows the history skeleton until the request settles instead of a false empty state', async () => {
    let resolveHistory!: (value: unknown) => void
    const historyPromise = new Promise((resolve) => { resolveHistory = resolve })
    installRoutes(defaultRoutes(twelveRevisions()))
    const baseImplementation = mockGet.getMockImplementation()!
    mockGet.mockImplementation((path: string, options?: { params?: { path?: Record<string, string> } }) => {
      if (path === '/api/v1/system-maps/{map_id}/revisions/history') return historyPromise
      return baseImplementation(path, options)
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="system-map-history-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-history-empty"]').exists()).toBe(false)

    resolveHistory(historyPayload(twelveRevisions()))
    await flushPromises()

    expect(wrapper.find('[data-testid="system-map-history-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-history-empty"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-revision-12"]').exists()).toBe(true)
  })

  it('reports a history failure with an inline retry that reloads the history', async () => {
    let historyCalls = 0
    installRoutes({
      ...defaultRoutes(twelveRevisions()),
      history: () => {
        historyCalls += 1
        return historyCalls === 1 ? { error: { detail: 'history unavailable' } } : historyPayload(twelveRevisions())
      },
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="system-map-history-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-history-empty"]').exists()).toBe(false)

    await wrapper.get('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(historyCalls).toBe(2)
    expect(wrapper.find('[data-testid="system-map-history-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-revision-12"]').exists()).toBe(true)
  })

  it('loads a selected revision as a read-only preview', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-9"]').trigger('click')
    await flushPromises()

    const store = useSystemMapsStore()
    expect(store.activeRevision?.id).toBe('rev-9')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}', {
      params: { path: { map_id: 'map-1', rev_id: 'rev-9' }, query: { include_graph: true } },
    })
    expect(wrapper.findComponent(HeaderStub).props('readOnly')).toBe(true)
  })

  it('renders every diff axis with counts and per-item detail', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-9"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-revision-compare"]').trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/current', {
      params: { path: { map_id: 'map-1' } },
    })
    // Default base is the currently published revision; the path revision is
    // the "before" side and the selected revision the "after" side.
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/diff', {
      params: { path: { map_id: 'map-1', rev_id: 'rev-11' } },
      body: { against_revision_id: 'rev-9' },
    })

    expect(wrapper.find('[data-testid="system-map-revision-diff-summary"]').text()).toContain('revision 11 to revision 9')
    expect(wrapper.get('[data-testid="system-map-revision-diff-nodes-added"]').text()).toContain('agent:new')
    expect(wrapper.get('[data-testid="system-map-revision-diff-nodes-removed"]').text()).toContain('agent:gone')
    expect(wrapper.get('[data-testid="system-map-revision-diff-nodes-modified"]').text()).toContain('pipeline:main')
    expect(wrapper.get('[data-testid="system-map-revision-diff-relationships-added"]').text()).toContain('a->b:flow')
    expect(wrapper.get('[data-testid="system-map-revision-diff-relationships-removed"]').text()).toContain('b->c:data')
    expect(wrapper.get('[data-testid="system-map-revision-diff-relationships-modified"]').text()).toContain('c->d:trigger')
    expect(wrapper.get('[data-testid="system-map-revision-diff-loop-configs-added"]').text()).toContain('loop:new')
    expect(wrapper.get('[data-testid="system-map-revision-diff-loop-configs-removed"]').text()).toContain('loop:gone')
    expect(wrapper.get('[data-testid="system-map-revision-diff-loop-configs-modified"]').text()).toContain('loop:main')
    expect(wrapper.find('[data-testid="system-map-revision-diff-content-changed"]').exists()).toBe(true)
  })

  it('reports nothing to compare when the map has a single revision', async () => {
    installRoutes({ ...defaultRoutes([11]), history: () => historyPayload([11]) })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-11"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-revision-compare"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="system-map-revision-diff-empty"]').exists()).toBe(true)
    expect(mockPost).not.toHaveBeenCalledWith(
      '/api/v1/system-maps/{map_id}/revisions/{rev_id}/diff',
      expect.anything(),
    )
  })

  it('restores a revision as a new draft and navigates to it without publishing', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-9"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-revision-compare"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-restore"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="form-dialog-description"]').text()).toContain('revision 9')

    await wrapper.get('[data-testid="form-dialog-confirm"]').trigger('click')
    await flushPromises()

    const store = useSystemMapsStore()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/restore-as-new', {
      params: { path: { map_id: 'map-1', rev_id: 'rev-9' } },
    })
    expect(store.activeRevision?.id).toBe('rev-13')
    expect(wrapper.get('[data-testid="system-map-revision-restore-success"]').text()).toContain('Draft revision 13')
    expect(mockPost.mock.calls.some((call) => String(call[0]).endsWith('/publish'))).toBe(false)
  })

  it('surfaces a restore failure inline and keeps the selected revision', async () => {
    installRoutes({ ...defaultRoutes(twelveRevisions()), restore: { error: { detail: 'restore unavailable' } } })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-9"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-revision-compare"]').trigger('click')
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-restore"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="form-dialog-confirm"]').trigger('click')
    await flushPromises()

    const store = useSystemMapsStore()
    expect(wrapper.get('[data-testid="system-map-revision-restore-error"]').text()).toContain('restore unavailable')
    expect(store.activeRevision?.id).toBe('rev-9')
  })

  it('loads the revision active at a chosen time and shows the historical banner', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-at"]').setValue('2026-02-01T10:00')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const expectedAt = new Date('2026-02-01T10:00').toISOString()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/by-time', {
      params: { path: { map_id: 'map-1' }, query: { at: expectedAt } },
    })

    const store = useSystemMapsStore()
    expect(store.byTimeRevision?.id).toBe('rev-5')
    expect(store.activeRevision?.id).toBe('rev-5')
    expect(wrapper.find('[data-testid="system-map-revision-historical-banner"]').exists()).toBe(true)
    expect(wrapper.findComponent(HeaderStub).props('readOnly')).toBe(true)
  })

  it('renders an honest empty state, not an error, when nothing was published at that time', async () => {
    installRoutes({
      ...defaultRoutes(twelveRevisions()),
      byTime: { error: { status: 404, detail: 'No revision active at that time' }, response: { status: 404 } },
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-at"]').setValue('2025-01-01T00:00')
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    const store = useSystemMapsStore()
    expect(store.byTimeUnavailable).toBe(true)
    expect(store.byTimeError).toBeNull()
    expect(wrapper.find('[data-testid="system-map-revision-as-of-unavailable"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-revision-as-of-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-revision-historical-banner"]').exists()).toBe(false)
  })

  it('returns from a historical view to the currently published revision', async () => {
    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[data-testid="system-map-revision-at"]').setValue('2026-02-01T10:00')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-historical-banner"]').exists()).toBe(true)

    await wrapper.get('[data-testid="system-map-revision-current"]').trigger('click')
    await flushPromises()

    const store = useSystemMapsStore()
    expect(store.byTimeRevision).toBeNull()
    expect(store.activeRevision?.id).toBe(CURRENT_REVISION_ID)
    expect(wrapper.find('[data-testid="system-map-revision-historical-banner"]').exists()).toBe(false)
    // The published revision is intrinsically read-only; what changed is that
    // the host is no longer pinned to a historical point in time.
    expect(wrapper.findComponent(HeaderStub).props('revision').id).toBe(CURRENT_REVISION_ID)
  })
})
