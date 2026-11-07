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

function mountView() {
  return mount(SystemMapDetailView, {
    global: {
      plugins: [i18n],
      stubs: {
        BackLink: true,
        HeaderBar: true,
        GraphEditor: true,
        RevisionSidebar: true,
        DiscoveryPanel: true,
      },
    },
  })
}

describe('SystemMapDetailView load failures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockRoute.params = { id: 'map-1' }
    mockRoute.query = {}
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
  })

  it('offers an inline retry that re-runs the failed load', async () => {
    let mapCalls = 0
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      if (path === '/api/v1/system-maps/{map_id}/revisions/history') {
        return Promise.resolve({ data: { system_map_id: 'map-1', revisions: [] } })
      }
      if (path === '/api/v1/system-maps/{map_id}/attribution-bindings') {
        return Promise.resolve({ data: [] })
      }
      mapCalls += 1
      if (mapCalls === 1) return Promise.resolve({ error: { detail: 'map unavailable' } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const wrapper = mountView()
    await flushPromises()

    const retry = wrapper.find('[data-testid="error-alert-retry"]')
    expect(retry.exists()).toBe(true)

    await retry.trigger('click')
    await flushPromises()

    expect(mapCalls).toBe(2)
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(true)
  })

  it('renders the unavailable state without a retry on a 402 feature_required', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      return Promise.resolve({
        error: {
          type: 'urn:problem:modulo:feature_required',
          status: 402,
          detail: 'system_maps_read is not enabled for this organisation',
        },
        response: { status: 402 },
      })
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('This system map is not available on your plan')
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(false)
  })

  it('keeps the retryable error state for a genuine server failure', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      return Promise.resolve({ error: { detail: 'down' }, response: { status: 500 } })
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('This system map is not available on your plan')
  })
})

describe('SystemMapDetailView authoring actions', () => {
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
  const RevisionStub = {
    name: 'RevisionSidebar',
    props: ['mapId', 'revisions', 'activeRevisionId', 'activeMap', 'readOnly'],
    emits: ['select-revision', 'select-current', 'select-by-time', 'new-draft', 'restore-revision', 'diff-revision', 'discover'],
    template: '<div data-testid="revision-stub" />',
  }
  const DiscoveryStub = {
    name: 'DiscoveryPanel',
    props: ['discovery'],
    emits: ['close', 'save'],
    template: '<div data-testid="discovery-stub" />',
  }
  const AttributionStub = {
    name: 'SystemMapAttributionBindings',
    props: ['nodes', 'bindings', 'readOnly', 'createHandler', 'deleteHandler'],
    template: '<div data-testid="attribution-stub" />',
  }

  function mountWithStubs() {
    return mount(SystemMapDetailView, {
      global: {
        plugins: [i18n],
        stubs: {
          BackLink: true,
          HeaderBar: HeaderStub,
          GraphEditor: GraphStub,
          RevisionSidebar: RevisionStub,
          DiscoveryPanel: DiscoveryStub,
          SystemMapAttributionBindings: AttributionStub,
        },
      },
    })
  }

  function seed(mockPost: ReturnType<typeof vi.fn>) {
    const store = useSystemMapsStore()
    store.activeMap = { id: 'map-1', name: 'Map one', published_revision_id: null } as never
    store.activeRevision = { id: 'rev-1', revision_number: 1, status: 'draft' } as never
    store.graph = {
      revision: { id: 'rev-1', revision_number: 1, status: 'draft' },
      nodes: [{ node_key: 'pipeline-1', node_type: 'pipeline', display_name: 'Pipeline 1', config_json: { label: 'a' }, pipeline_id: 'p-1', connector_instance_id: 'c-1', owner_team_id: null, eval_id: 'e-1', position_x: '10', position_y: '20' }],
      relationships: [{ source_node_key: 'pipeline-1', target_node_key: 'connector-2', relationship_type: 'flow', config_json: { label: 'b' } }],
      loop_configs: [],
    } as never
    mockPost.mockResolvedValue({ data: { id: 'rev-2', revision_number: 2, status: 'draft' } })
    return store
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockRoute.params = { id: 'map-1' }
    mockRoute.query = {}
    if (mockPost) mockPost.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}') return Promise.resolve({ data: { revision: { id: 'rev-2', revision_number: 2, status: 'draft' }, nodes: [], relationships: [] } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })
  })

  it('toggles the revision sidebar', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()

    expect(wrapper.find('[data-testid="system-map-revision-sidebar"]').exists()).toBe(true)
    wrapper.findComponent(HeaderStub).vm.$emit('toggle-sidebar')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-sidebar"]').exists()).toBe(false)
  })

  it('saves the drafted graph with the full node and relationship payload', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()

    expect(store.graph?.nodes).toHaveLength(1)
    wrapper.findComponent(HeaderStub).vm.$emit('save')
    await flushPromises()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions', {
      params: { path: { map_id: 'map-1' } },
      body: {
        nodes: [{
          node_key: 'pipeline-1',
          node_type: 'pipeline',
          display_name: 'Pipeline 1',
          config_json: { label: 'a' },
          pipeline_id: 'p-1',
          connector_instance_id: 'c-1',
          owner_team_id: null,
          eval_id: 'e-1',
          position_x: '10',
          position_y: '20',
        }],
        relationships: [{ source_node_key: 'pipeline-1', target_node_key: 'connector-2', relationship_type: 'flow', config_json: { label: 'b' } }],
        loop_configs: [],
        notes: 'Manual save',
      },
    })
  })

  it('applies node and relationship changes emitted by the editor', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    wrapper.findComponent(GraphStub).vm.$emit('nodes-change', [{ node_key: 'agent-1', node_type: 'agent', display_name: 'Agent', config_json: {} }])
    wrapper.findComponent(GraphStub).vm.$emit('relationships-change', [{ source_node_key: 'agent-1', target_node_key: 'agent-1', relationship_type: 'data', config_json: {} }])
    wrapper.findComponent(HeaderStub).vm.$emit('save')
    await flushPromises()

    const body = mockPost.mock.calls[0][1].body
    expect(body.nodes[0].node_key).toBe('agent-1')
    expect(body.relationships[0].relationship_type).toBe('data')
  })

  it('reports save failures', async () => {
    seed(mockPost)
    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    const wrapper = mountWithStubs()
    await flushPromises()

    wrapper.findComponent(HeaderStub).vm.$emit('save')
    await flushPromises()

    expect(wrapper.text()).toContain('Could not save draft revision')
  })

  it('publishes and archives a revision', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    wrapper.findComponent(HeaderStub).vm.$emit('publish')
    await flushPromises()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/publish', { params: { path: { map_id: 'map-1', rev_id: 'rev-1' } } })

    wrapper.findComponent(HeaderStub).vm.$emit('archive')
    await flushPromises()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/archive', { params: { path: { map_id: 'map-1', rev_id: 'rev-1' } } })
  })

  it('reports publish and archive failures', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    wrapper.findComponent(HeaderStub).vm.$emit('publish')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not publish revision')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(HeaderStub).vm.$emit('archive')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not archive revision')
  })

  it('loads a selected revision and reports failures', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      return Promise.resolve({ error: { detail: 'missing' } })
    })
    wrapper.findComponent(RevisionStub).vm.$emit('select-revision', 'rev-9')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load map revision')

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      return Promise.resolve({ data: { revision: { id: 'rev-9', revision_number: 9, status: 'draft' }, nodes: [], relationships: [] } })
    })
    // A successful switch clears the previous failure banner without the
    // operator having to dismiss it first.
    wrapper.findComponent(RevisionStub).vm.$emit('select-revision', 'rev-9')
    await flushPromises()
    expect(store.activeRevision?.id).toBe('rev-9')
    expect(wrapper.text()).not.toContain('Could not load map revision')
  })

  it('switches to the current revision and reports failures', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/current') return Promise.resolve({ data: { revision: { id: 'rev-5', revision_number: 5 } } })
      return Promise.resolve({ data: { revision: { id: 'rev-5', revision_number: 5, status: 'published' }, nodes: [], relationships: [] } })
    })
    wrapper.findComponent(RevisionStub).vm.$emit('select-current')
    await flushPromises()
    expect(useSystemMapsStore().activeRevision?.id).toBe('rev-5')

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/current') return Promise.resolve({ error: {} })
      return Promise.resolve({ data: {} })
    })
    wrapper.findComponent(RevisionStub).vm.$emit('select-current')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load the current revision')
  })

  it('switches to the revision active at a timestamp and reports an unmatched time', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/by-time') return Promise.resolve({ data: { matched: true, requested_at: '2026-01-15T10:30:00Z', revision: { id: 'rev-4', revision_number: 4 } }, response: { status: 200 } })
      return Promise.resolve({ data: { revision: { id: 'rev-4', revision_number: 4, status: 'archived' }, nodes: [], relationships: [] } })
    })
    wrapper.findComponent(RevisionStub).vm.$emit('select-by-time', '2026-01-15T10:30:00Z')
    await flushPromises()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/by-time', {
      params: { path: { map_id: 'map-1' }, query: { at: '2026-01-15T10:30:00Z' } },
    })
    expect(useSystemMapsStore().activeRevision?.id).toBe('rev-4')

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      // The wire contract raises 404 (never matched=false) for an unmatched time.
      if (path === '/api/v1/system-maps/{map_id}/revisions/by-time') {
        return Promise.resolve({ error: { detail: 'No revision active at 2020-01-01T00:00:00+00:00' }, response: { status: 404 } })
      }
      return Promise.resolve({ data: {} })
    })
    wrapper.findComponent(RevisionStub).vm.$emit('select-by-time', '2020-01-01T00:00:00Z')
    await flushPromises()
    expect(wrapper.text()).toContain('No revision was active at that time')
  })

  it('ignores current and by-time switching when no map is loaded', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()
    store.activeMap = null
    const loadCurrent = vi.spyOn(store, 'loadCurrentRevision')
    const loadByTime = vi.spyOn(store, 'loadRevisionByTime')

    wrapper.findComponent(RevisionStub).vm.$emit('select-current')
    wrapper.findComponent(RevisionStub).vm.$emit('select-by-time', '2026-01-15T10:30:00Z')
    await flushPromises()

    expect(loadCurrent).not.toHaveBeenCalled()
    expect(loadByTime).not.toHaveBeenCalled()
  })

  it('falls back to the generic message when switching failures are not Error instances', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()

    vi.spyOn(store, 'loadCurrentRevision').mockRejectedValueOnce('boom')
    wrapper.findComponent(RevisionStub).vm.$emit('select-current')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load the current revision')

    vi.spyOn(store, 'loadRevisionByTime').mockRejectedValueOnce('boom')
    wrapper.findComponent(RevisionStub).vm.$emit('select-by-time', '2026-01-15T10:30:00Z')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load the revision for that time')
  })

  it('creates a new draft from the sidebar', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    wrapper.findComponent(RevisionStub).vm.$emit('new-draft')
    await flushPromises()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions', expect.objectContaining({ body: expect.objectContaining({ notes: 'Draft from revision 1' }) }))

    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    wrapper.findComponent(RevisionStub).vm.$emit('new-draft')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not save draft revision')
  })

  it('restores a historical revision as a new draft from the sidebar', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()
    const restore = vi.spyOn(store, 'restoreRevisionAsNew').mockResolvedValue({ id: 'rev-3', revision_number: 3, status: 'draft' } as never)

    wrapper.findComponent(RevisionStub).vm.$emit('restore-revision', 'rev-2')
    await flushPromises()

    expect(restore).toHaveBeenCalledWith('map-1', 'rev-2')
    expect(wrapper.text()).not.toContain('Could not restore revision')
  })

  it('reports restore failures and falls back for non-Error rejections', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()
    const restore = vi.spyOn(store, 'restoreRevisionAsNew')

    restore.mockRejectedValueOnce(new Error('Could not restore revision'))
    wrapper.findComponent(RevisionStub).vm.$emit('restore-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not restore revision')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    restore.mockRejectedValueOnce('offline')
    wrapper.findComponent(RevisionStub).vm.$emit('restore-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not restore revision')
  })

  it('diffs a revision against the published revision and renders the change counts', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    store.activeMap = { id: 'map-1', name: 'Map one', published_revision_id: 'rev-1' } as never
    await flushPromises()
    const diff = vi.spyOn(store, 'diffRevision').mockResolvedValue({
      base_revision_id: 'rev-2',
      base_revision_number: 2,
      target_revision_id: 'rev-1',
      target_revision_number: 1,
      nodes_added: ['node-a'],
      nodes_removed: [],
      content_json_changed: true,
    } as never)

    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()

    expect(diff).toHaveBeenCalledWith('map-1', 'rev-2', 'rev-1')
    const panel = wrapper.get('[data-testid="system-map-revision-diff-result"]')
    expect(panel.text()).toContain('Nodes added: 1')
    expect(panel.text()).not.toContain('Nodes removed')
    expect(panel.text()).toContain('Content changed')

    await panel.get('[data-testid="system-map-revision-diff-dismiss"]').trigger('click')
    expect(wrapper.find('[data-testid="system-map-revision-diff-result"]').exists()).toBe(false)
  })

  it('clears a rendered diff when switching revisions or time', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    store.activeMap = { id: 'map-1', name: 'Map one', published_revision_id: 'rev-1' } as never
    await flushPromises()
    vi.spyOn(store, 'diffRevision').mockResolvedValue({
      base_revision_id: 'rev-2',
      base_revision_number: 2,
      target_revision_id: 'rev-1',
      target_revision_number: 1,
      nodes_added: ['node-a'],
    } as never)

    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-diff-result"]').exists()).toBe(true)

    wrapper.findComponent(RevisionStub).vm.$emit('select-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-diff-result"]').exists()).toBe(false)

    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-diff-result"]').exists()).toBe(true)

    wrapper.findComponent(RevisionStub).vm.$emit('select-by-time', '2026-01-15T10:30:00Z')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-revision-diff-result"]').exists()).toBe(false)
  })

  it('skips diffing when the map has no published revision or is not loaded', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    const diff = vi.spyOn(store, 'diffRevision')

    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(diff).not.toHaveBeenCalled()

    store.activeMap = null
    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(diff).not.toHaveBeenCalled()
  })

  it('reports diff failures and falls back for non-Error rejections', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    store.activeMap = { id: 'map-1', name: 'Map one', published_revision_id: 'rev-1' } as never
    await flushPromises()
    const diff = vi.spyOn(store, 'diffRevision')

    diff.mockRejectedValueOnce(new Error('Could not compare revisions'))
    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not compare revisions')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    diff.mockRejectedValueOnce('offline')
    wrapper.findComponent(RevisionStub).vm.$emit('diff-revision', 'rev-2')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not compare revisions')
  })

  it('runs discovery, opens the review panel and saves selections', async () => {
    seed(mockPost)
    const report = { draft_map_id: 'map-1', draft_revision_id: 'rev-1', inferred_nodes: [{ node_key: 'agent-1', node_type: 'agent', display_name: 'Agent' }], inferred_edges: [] }
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/discover') return Promise.resolve({ data: report })
      return Promise.resolve({ data: { id: 'rev-3', revision_number: 3, status: 'draft' } })
    })
    const wrapper = mountWithStubs()
    await flushPromises()

    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-discovery-submit"]').exists()).toBe(true)
    await wrapper.get('[data-testid="system-map-discovery-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(true)

    wrapper.findComponent(DiscoveryStub).vm.$emit('save', { acceptedNodes: [0], acceptedEdges: [], rejectedNodes: [], rejectedEdges: [] })
    await flushPromises()
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions', expect.objectContaining({ body: expect.objectContaining({ nodes: expect.arrayContaining([expect.objectContaining({ node_key: 'agent-1' })]) }) }))
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(false)
  })

  it('reports discovery and discovery-save failures', async () => {
    const store = seed(mockPost)
    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    const wrapper = mountWithStubs()
    await flushPromises()

    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-discovery-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not discover system map')

    store.discovery = { inferred_nodes: [], inferred_edges: [] } as never
    wrapper.findComponent(RevisionStub).vm.$emit('discover')
    await flushPromises()
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(false)
  })

  it('closes the discovery panel without saving', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    seed(mockPost)
    await flushPromises()

    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-1', draft_revision_id: 'rev-1', inferred_nodes: [], inferred_edges: [] } })
    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    await flushPromises()
    await wrapper.get('[data-testid="system-map-discovery-submit"]').trigger('click')
    await flushPromises()
    wrapper.findComponent(DiscoveryStub).vm.$emit('close')
    await flushPromises()

    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(false)
  })

  it('opens the discovery review panel for the dashboard deep link', async () => {
    const store = useSystemMapsStore()
    await store.fetchAccess()
    store.discovery = { inferred_nodes: [], inferred_edges: [] } as never
    mockRoute.query = { discovery: '1' }

    const wrapper = mountWithStubs()
    await flushPromises()

    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(true)
  })

  it('passes read-only to the editor for a published revision', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    store.activeRevision = { id: 'rev-1', revision_number: 1, status: 'published' } as never
    await flushPromises()

    expect(wrapper.findComponent(GraphStub).props('readOnly')).toBe(true)
  })

  it('renders the published detail read-only for a viewer', async () => {
    mockIsOperator.value = false
    mockPlanStore.devMode = false
    mockPlanStore.userPermissions = ['system_maps.read']
    mockGet.mockImplementation(() =>
      Promise.resolve({ data: { id: 'map-1', name: 'Demo Delivery System', published_revision_id: 'rev-1' } }),
    )

    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()
    store.graph = { revision: { id: 'rev-1', revision_number: 1, status: 'published' }, nodes: [], relationships: [], loop_configs: [] } as never
    await flushPromises()

    expect(mockGet).not.toHaveBeenCalledWith('/api/v1/admin/feature-flags')
    expect(wrapper.findComponent(HeaderStub).props('readOnly')).toBe(true)
    expect(wrapper.findComponent(GraphStub).props('readOnly')).toBe(true)
    expect(wrapper.findComponent(RevisionStub).props('readOnly')).toBe(true)
  })

  it('keeps the operator authoring view editable', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()
    store.graph = { revision: { id: 'rev-1', revision_number: 1, status: 'draft' }, nodes: [], relationships: [], loop_configs: [] } as never
    await flushPromises()

    expect(wrapper.findComponent(HeaderStub).props('readOnly')).toBe(false)
    expect(wrapper.findComponent(GraphStub).props('readOnly')).toBe(false)
    expect(wrapper.findComponent(RevisionStub).props('readOnly')).toBe(false)
  })

  it('loads attribution bindings for the active map and reports load failures', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/attribution-bindings') return Promise.resolve({ data: [{ id: 'binding-1' }] })
      if (path === '/api/v1/system-maps/{map_id}/revisions/history') return Promise.resolve({ data: { system_map_id: 'map-1', revisions: [] } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    mountWithStubs()
    await flushPromises()

    expect(useSystemMapsStore().attributionBindings).toHaveLength(1)

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/attribution-bindings') return Promise.resolve({ error: { detail: 'bindings down' } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/history') return Promise.resolve({ data: { system_map_id: 'map-1', revisions: [] } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const failing = mountWithStubs()
    await flushPromises()
    expect(failing.text()).toContain('bindings down')
  })

  it('uses the localized fallback when attribution binding loading rejects with a non-Error', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [] } })
      if (path === '/api/v1/system-maps/{map_id}/revisions/history') return Promise.resolve({ data: { system_map_id: 'map-1', revisions: [] } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })
    const store = useSystemMapsStore()
    vi.spyOn(store, 'loadAttributionBindings').mockRejectedValue('offline')

    const wrapper = mountWithStubs()
    await flushPromises()

    expect(wrapper.text()).toContain('Could not load attribution bindings')
  })

  it('creates and deletes attribution bindings from the panel', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()

    const create = vi.spyOn(store, 'createAttributionBinding').mockResolvedValue({ id: 'binding-2' } as never)
    const remove = vi.spyOn(store, 'deleteAttributionBinding').mockResolvedValue(undefined)
    const attribution = wrapper.findComponent(AttributionStub)
    const createHandler = attribution.props('createHandler') as (binding: unknown) => Promise<void>
    const deleteHandler = attribution.props('deleteHandler') as (bindingId: string) => Promise<void>

    await createHandler({ node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1', binding_kind: 'trigger' })
    await flushPromises()
    expect(create).toHaveBeenCalledWith('map-1', { node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1', binding_kind: 'trigger' })

    await deleteHandler('binding-2')
    await flushPromises()
    expect(remove).toHaveBeenCalledWith('map-1', 'binding-2')
  })

  it('ignores attribution binding actions when no map is loaded', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()

    const create = vi.spyOn(store, 'createAttributionBinding')
    const remove = vi.spyOn(store, 'deleteAttributionBinding')
    const attribution = wrapper.findComponent(AttributionStub)
    const createHandler = attribution.props('createHandler') as (binding: unknown) => Promise<void>
    const deleteHandler = attribution.props('deleteHandler') as (bindingId: string) => Promise<void>
    store.activeMap = null

    await createHandler({ node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1', binding_kind: 'trigger' })
    await deleteHandler('binding-2')
    await flushPromises()

    expect(create).not.toHaveBeenCalled()
    expect(remove).not.toHaveBeenCalled()
  })

  it('propagates attribution binding action failures to the panel handler', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = seed(mockPost)
    await flushPromises()

    const create = vi.spyOn(store, 'createAttributionBinding')
    const remove = vi.spyOn(store, 'deleteAttributionBinding')
    const attribution = wrapper.findComponent(AttributionStub)
    const createHandler = attribution.props('createHandler') as (binding: unknown) => Promise<void>
    const deleteHandler = attribution.props('deleteHandler') as (bindingId: string) => Promise<void>
    const payload = { node_key: 'pipeline-1', pipeline_id: 'pipe-1', trigger_id: 'trig-1', binding_kind: 'trigger' }

    create.mockRejectedValueOnce(new Error('create exploded'))
    await expect(createHandler(payload)).rejects.toThrow('create exploded')

    create.mockRejectedValueOnce('offline')
    await expect(createHandler(payload)).rejects.toBe('offline')

    remove.mockRejectedValueOnce(new Error('delete exploded'))
    await expect(deleteHandler('binding-2')).rejects.toThrow('delete exploded')

    remove.mockRejectedValueOnce('offline')
    await expect(deleteHandler('binding-2')).rejects.toBe('offline')
  })
})

describe('SystemMapDetailView guards and non-Error failures', () => {
  const HeaderStub = { name: 'HeaderBar', emits: ['toggle-sidebar', 'save', 'publish', 'archive', 'discover'], template: '<div />' }
  const GraphStub = { name: 'GraphEditor', emits: ['nodes-change', 'relationships-change'], template: '<div />' }
  const RevisionStub = { name: 'RevisionSidebar', emits: ['select-revision', 'new-draft', 'restore-revision', 'discover'], template: '<div />' }
  const DiscoveryStub = { name: 'DiscoveryPanel', emits: ['close', 'save'], template: '<div />' }

  function mountWithStubs() {
    return mount(SystemMapDetailView, {
      global: {
        plugins: [i18n],
        stubs: { BackLink: true, HeaderBar: HeaderStub, GraphEditor: GraphStub, RevisionSidebar: RevisionStub, DiscoveryPanel: DiscoveryStub },
      },
    })
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockRoute.params = { id: 'map-1' }
    mockRoute.query = {}
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })
  })

  async function mountedWithMap() {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()
    store.activeMap = { id: 'map-1', name: 'Map one', published_revision_id: null } as never
    store.activeRevision = { id: 'rev-1', revision_number: 1, status: 'draft' } as never
    await flushPromises()
    return { wrapper, store }
  }

  it('returns early from every action when there is no active map or revision', async () => {
    const wrapper = mountWithStubs()
    await flushPromises()
    const store = useSystemMapsStore()
    store.activeMap = null
    store.activeRevision = null

    wrapper.findComponent(RevisionStub).vm.$emit('select-revision', 'rev-1')
    wrapper.findComponent(RevisionStub).vm.$emit('new-draft')
    wrapper.findComponent(RevisionStub).vm.$emit('restore-revision', 'rev-1')
    wrapper.findComponent(RevisionStub).vm.$emit('discover')
    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    wrapper.findComponent(HeaderStub).vm.$emit('save')
    wrapper.findComponent(HeaderStub).vm.$emit('publish')
    wrapper.findComponent(HeaderStub).vm.$emit('archive')
    await flushPromises()

    expect(mockPost).not.toHaveBeenCalled()

    store.activeMap = { id: 'map-1', name: 'Map one' } as never
    wrapper.findComponent(RevisionStub).vm.$emit('new-draft')
    wrapper.findComponent(HeaderStub).vm.$emit('publish')
    wrapper.findComponent(HeaderStub).vm.$emit('archive')
    await flushPromises()

    expect(mockPost).not.toHaveBeenCalled()
  })

  it('uses localized fallbacks for non-Error rejections', async () => {
    const { wrapper, store } = await mountedWithMap()
    vi.spyOn(store, 'loadRevision').mockRejectedValue('offline')
    vi.spyOn(store, 'saveDraft').mockRejectedValue('offline')
    vi.spyOn(store, 'discover').mockRejectedValue('offline')
    vi.spyOn(store, 'publish').mockRejectedValue('offline')
    vi.spyOn(store, 'archive').mockRejectedValue('offline')

    wrapper.findComponent(RevisionStub).vm.$emit('select-revision', 'rev-1')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load revision')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(RevisionStub).vm.$emit('new-draft')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not create draft')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    await flushPromises()
    expect(wrapper.find('[data-testid="system-map-discovery-submit"]').exists()).toBe(true)
    await wrapper.get('[data-testid="system-map-discovery-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Discovery failed')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(HeaderStub).vm.$emit('save')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not save draft')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(HeaderStub).vm.$emit('publish')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not publish revision')

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')
    wrapper.findComponent(HeaderStub).vm.$emit('archive')
    await flushPromises()
    expect(wrapper.text()).toContain('Could not archive revision')
  })

  it('uses the localized fallback when revision history loading rejects with a non-Error', async () => {
    const store = useSystemMapsStore()
    vi.spyOn(store, 'loadRevisionHistory').mockRejectedValue('offline')

    const wrapper = mountWithStubs()
    await flushPromises()

    expect(store.activeMap?.id).toBe('map-1')
    expect(wrapper.text()).toContain('Could not load revision history')
  })

  it('keeps the discovery panel open when applying selections fails to save', async () => {
    const { wrapper, store } = await mountedWithMap()
    vi.spyOn(store, 'discover').mockResolvedValue({ draft_map_id: 'map-1', inferred_nodes: [], inferred_edges: [] } as never)
    vi.spyOn(store, 'saveDraft').mockRejectedValue(new Error('save failed'))
    store.discovery = { inferred_nodes: [], inferred_edges: [] } as never

    wrapper.findComponent(HeaderStub).vm.$emit('discover')
    await flushPromises()
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(false)
    store.activeRevision = { id: 'rev-1', revision_number: 1, status: 'draft' } as never
    await wrapper.get('[data-testid="system-map-discovery-submit"]').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(true)

    wrapper.findComponent(DiscoveryStub).vm.$emit('save', { acceptedNodes: [], acceptedEdges: [], rejectedNodes: [], rejectedEdges: [] })
    await flushPromises()

    expect(wrapper.text()).toContain('save failed')
    expect(wrapper.findComponent(DiscoveryStub).exists()).toBe(true)
  })
})

describe('SystemMapDetailView loading and graph state lifecycle (STATE-1 / STATE-2)', () => {
  let resolveFlags: (value: unknown) => void
  let resolveMap: (value: unknown) => void

  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
  })

  it('renders loading state while store.loading is true and does not render empty state', async () => {
    const flagsPromise = new Promise((resolve) => { resolveFlags = resolve })
    const mapPromise = new Promise((resolve) => { resolveMap = resolve })

    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return flagsPromise
      if (path === '/api/v1/system-maps/map-1' || path === '/api/v1/system-maps/{map_id}') return mapPromise
      return Promise.resolve({ data: {} })
    })

    const wrapper = mountView()
    const store = useSystemMapsStore()
    expect(store.loading).toBe(true)
    await wrapper.vm.$nextTick()

    const loadingIndicator = wrapper.find('[data-testid="system-map-detail-loading"]')
    expect(loadingIndicator.exists()).toBe(true)
    // <output> carries the implicit `status` role, so it must be announced as
    // a polite live region without an explicit role="status" (Web:S6819).
    const liveRegion = loadingIndicator.find('output')
    expect(liveRegion.exists()).toBe(true)
    expect(liveRegion.attributes('aria-live')).toBe('polite')
    expect(liveRegion.text()).toContain('Loading system map')

    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-graph-canvas"]').exists()).toBe(false)

    resolveFlags({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    resolveMap({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    await flushPromises()
  })

  it('renders empty-graph state once loaded and map genuinely has zero nodes', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useSystemMapsStore()
    expect(store.loading).toBe(false)
    expect(store.graph).toBeNull()

    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-graph-canvas"]').exists()).toBe(false)
  })

  it('renders graph editor when nodes exist', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      if (path === '/api/v1/system-maps/{map_id}/revisions/{rev_id}') {
        return Promise.resolve({
          data: {
            revision: { id: 'rev-1', revision_number: 1, status: 'published' },
            nodes: [{ node_key: 'pipeline-1', node_type: 'pipeline', display_name: 'Pipeline 1', config_json: {}, position_x: '0', position_y: '0' }],
            relationships: [],
          },
        })
      }
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: 'rev-1' } })
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useSystemMapsStore()
    expect(store.loading).toBe(false)
    expect(store.graph?.nodes).toHaveLength(1)

    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-graph-canvas"]').exists()).toBe(true)
  })

  it('keeps the graph editor mounted for a loaded draft with zero nodes so the first node can be added', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useSystemMapsStore()
    store.graph = {
      revision: { id: 'rev-1', revision_number: 1, status: 'draft' },
      nodes: [],
      relationships: [],
      loop_configs: [],
    } as never
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="system-map-graph-canvas"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(false)
  })

  it('toggles loading and empty states reactively when store.loading changes', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
      }
      return Promise.resolve({ data: { id: 'map-1', name: 'Map one', published_revision_id: null } })
    })

    const wrapper = mountView()
    await flushPromises()

    const store = useSystemMapsStore()
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(false)

    store.loading = true
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(false)

    store.loading = false
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="system-map-detail-loading"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-empty-state"]').exists()).toBe(true)
  })

})
