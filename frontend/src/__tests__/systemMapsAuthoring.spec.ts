import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
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

import {
  useSystemMapsStore,
  NODE_TYPES,
  RELATIONSHIP_TYPES,
} from '../stores/systemMaps'
import GraphEditor from '../components/system-maps/SystemMapGraphEditor.vue'
import DiscoveryPanel from '../components/system-maps/SystemMapDiscoveryPanel.vue'
import { mergeDiscoverySelections } from '../components/system-maps/discoveryMerge'
import enUS from '../locales/en-US.js'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

function node(overrides: Record<string, unknown> = {}) {
  return {
    id: 'n1',
    organisation_id: 'org',
    revision_id: 'rev',
    node_key: 'pipeline:a',
    node_type: 'pipeline',
    display_name: 'Pipeline A',
    config_json: {},
    pipeline_id: null,
    connector_instance_id: null,
    owner_team_id: null,
    eval_id: null,
    position_x: '40',
    position_y: '40',
    deleted_at: null,
    deleted_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function relationship(overrides: Record<string, unknown> = {}) {
  return {
    id: 'r1',
    organisation_id: 'org',
    revision_id: 'rev',
    source_node_key: 'pipeline:a',
    target_node_key: 'connector:b',
    relationship_type: 'flow',
    config_json: {},
    deleted_at: null,
    deleted_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('System Maps authoring vocabularies', () => {
  it('mirrors the backend node and relationship type vocabularies', () => {
    expect(NODE_TYPES).toEqual(['pipeline', 'agent', 'connector', 'improvement_loop', 'data_store', 'external_system'])
    expect(RELATIONSHIP_TYPES).toEqual(['flow', 'data', 'trigger', 'loop_back'])
  })
})

describe('GraphEditor', () => {
  const nodes = [node(), node({ id: 'n2', node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B', position_x: '300', position_y: '200' })]
  const relationships = [relationship()]

  function mountEditor() {
    return mount(GraphEditor, {
      props: {
        nodes,
        relationships,
        loopConfigs: [],
        nodeTypes: NODE_TYPES,
        relationshipTypes: RELATIONSHIP_TYPES,
      },
      global: { plugins: [i18n] },
    })
  }

  it('draws each edge between its own node centres rather than fixed coordinates', () => {
    const wrapper = mountEditor()
    const line = wrapper.get('line')

    expect(line.attributes('x1')).toBe('160')
    expect(line.attributes('y1')).toBe('112')
    expect(line.attributes('x2')).toBe('420')
    expect(line.attributes('y2')).toBe('272')
  })

  it('adds a relationship using the selected source and target endpoints', async () => {
    const wrapper = mountEditor()

    await wrapper.get('[data-testid="system-map-relationship-source"]').setValue('connector:b')
    await wrapper.get('[data-testid="system-map-relationship-target"]').setValue('pipeline:a')
    await wrapper.get('[data-testid="system-map-relationship-type"]').setValue('data')
    await wrapper.get('[data-testid="system-map-add-relationship"]').trigger('click')

    const emitted = wrapper.emitted('relationships-change')
    expect(emitted).toBeTruthy()
    const payload = emitted![emitted!.length - 1][0] as Array<Record<string, unknown>>
    const added = payload[payload.length - 1]
    expect(added.source_node_key).toBe('connector:b')
    expect(added.target_node_key).toBe('pipeline:a')
    expect(added.relationship_type).toBe('data')
  })
})

describe('DiscoveryPanel', () => {
  const discovery = {
    sources_inspected: [],
    node_count: 2,
    edge_count: 2,
    inferred_nodes: [
      { node_key: 'pipeline:a', node_type: 'pipeline', display_name: 'Pipeline A' },
      { node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B' },
    ],
    inferred_edges: [
      { source_node_key: 'pipeline:a', target_node_key: 'connector:b', relationship_type: 'flow' },
      { source_node_key: 'connector:b', target_node_key: 'pipeline:a', relationship_type: 'data' },
    ],
    skipped: [],
    warnings: [],
    draft_map_id: 'map',
    draft_revision_id: 'rev',
    draft_revision_number: 1,
    published: false,
  }

  it('only includes accepted suggestions in the save payload', async () => {
    const wrapper = mount(DiscoveryPanel, {
      props: { discovery },
      global: { plugins: [i18n] },
    })

    await wrapper.get('[data-testid="system-map-accept-node-pipeline:a"]').trigger('click')
    await wrapper.get('[data-testid="system-map-reject-node-connector:b"]').trigger('click')
    await wrapper.get('[data-testid="system-map-accept-edge-1"]').trigger('click')
    await wrapper.get('[data-testid="system-map-discovery-save"]').trigger('click')

    const emitted = wrapper.emitted('save')
    expect(emitted).toBeTruthy()
    expect(emitted![0][0]).toEqual({ acceptedNodes: [0], acceptedEdges: [1], rejectedNodes: [1], rejectedEdges: [] })
  })

  it('toggles a decision off when the same action is pressed twice', async () => {
    const wrapper = mount(DiscoveryPanel, {
      props: { discovery },
      global: { plugins: [i18n] },
    })

    const accept = wrapper.get('[data-testid="system-map-accept-node-pipeline:a"]')
    await accept.trigger('click')
    await accept.trigger('click')
    await wrapper.get('[data-testid="system-map-discovery-save"]').trigger('click')

    expect(wrapper.emitted('save')![0][0]).toEqual({ acceptedNodes: [], acceptedEdges: [], rejectedNodes: [], rejectedEdges: [] })
  })
})

describe('mergeDiscoverySelections', () => {
  const discovery = {
    sources_inspected: [],
    node_count: 2,
    edge_count: 2,
    inferred_nodes: [
      { node_key: 'pipeline:a', node_type: 'pipeline', display_name: 'Pipeline A', position_x: 0, position_y: 0 },
      { node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B', position_x: 0, position_y: 0 },
    ],
    inferred_edges: [
      { source_node_key: 'pipeline:a', target_node_key: 'connector:b', relationship_type: 'flow' },
      { source_node_key: 'connector:b', target_node_key: 'pipeline:a', relationship_type: 'data' },
    ],
    skipped: [],
    warnings: [],
    draft_map_id: 'map',
    draft_revision_id: 'rev',
    draft_revision_number: 1,
    published: false,
  }

  it('merges only accepted suggestions and lays them out in a grid', () => {
    const merged = mergeDiscoverySelections([], [], discovery as never, { acceptedNodes: [0, 1], acceptedEdges: [0], rejectedNodes: [], rejectedEdges: [] })

    expect(merged.nodes.map((item) => item.node_key)).toEqual(['pipeline:a', 'connector:b'])
    expect(merged.nodes[0].position_x).toBe('40')
    expect(merged.nodes[1].position_x).toBe('260')
    expect(merged.relationships).toHaveLength(1)
    expect(merged.relationships[0].relationship_type).toBe('flow')
  })

  it('never duplicates a node or edge that is already in the draft', () => {
    const existingRel = relationship()
    const existingNodes = [node(), node({ id: 'n2', node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B', position_x: '300', position_y: '200' })]
    const merged = mergeDiscoverySelections(existingNodes, [existingRel], discovery as never, { acceptedNodes: [0], acceptedEdges: [0], rejectedNodes: [], rejectedEdges: [] })

    expect(merged.nodes).toHaveLength(2)
    expect(merged.relationships).toHaveLength(1)
  })

  it('removes a rejected inferred node and the edges that reference it', () => {
    const existingNodes = [node(), node({ id: 'n2', node_key: 'connector:b', node_type: 'connector', display_name: 'Connector B', position_x: '300', position_y: '200' })]
    const merged = mergeDiscoverySelections(existingNodes, [relationship()], discovery as never, {
      acceptedNodes: [0],
      acceptedEdges: [],
      rejectedNodes: [1],
      rejectedEdges: [],
    })

    expect(merged.nodes.map((item) => item.node_key)).toEqual(['pipeline:a'])
    expect(merged.relationships).toHaveLength(0)
  })

  it('returns the graph unchanged when there is no discovery report', () => {
    const merged = mergeDiscoverySelections([node()], [], null, { acceptedNodes: [0], acceptedEdges: [0], rejectedNodes: [], rejectedEdges: [] })

    expect(merged.nodes).toHaveLength(1)
    expect(merged.relationships).toHaveLength(0)
  })
})

describe('useSystemMapsStore authoring', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
  })

  it('posts the edited graph and reloads the saved revision', async () => {
    const revision = { id: 'rev-2', revision_number: 2, status: 'draft' }
    mockPost.mockResolvedValue({ data: revision })
    mockGet.mockResolvedValue({ data: { revision, nodes: [], relationships: [], loop_configs: [] } })

    const store = useSystemMapsStore()
    await store.saveDraft('map-1', [{
      node_key: 'pipeline:a',
      node_type: 'pipeline',
      display_name: 'Pipeline A',
      position_x: '10',
      position_y: '20',
    }], [], 'Manual save')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions', {
      params: { path: { map_id: 'map-1' } },
      body: {
        nodes: [{ node_key: 'pipeline:a', node_type: 'pipeline', display_name: 'Pipeline A', position_x: '10', position_y: '20' }],
        relationships: [],
        loop_configs: [],
        notes: 'Manual save',
      },
    })
    expect(store.activeRevision?.id).toBe('rev-2')
    expect(store.graph?.revision.id).toBe('rev-2')
  })

  it('surfaces a discovery report and loads its draft map', async () => {
    const report = { draft_map_id: 'map-2', inferred_nodes: [], inferred_edges: [] }
    mockPost.mockResolvedValue({ data: report })
    mockGet.mockResolvedValue({ data: { id: 'map-2', name: 'Discovered', published_revision_id: null } })

    const store = useSystemMapsStore()
    await store.discover('Discovered', 'desc')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/discover', { body: { name: 'Discovered', description: 'desc' } })
    expect(store.discovery).toEqual(report)
    expect(store.activeMap?.id).toBe('map-2')
  })
})
