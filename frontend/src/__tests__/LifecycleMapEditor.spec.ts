import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent } from 'vue'

vi.mock('../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import LifecycleMapEditor from '../components/lifecycle-map/editor/LifecycleMapEditor.vue'
import VersionHistoryDropdown from '../components/lifecycle-map/editor/VersionHistoryDropdown.vue'

const stageA = {
  id: 'stage-a',
  name: 'Stage A',
  description: '',
  stage_type: 'modulo',
  pipeline_id: null,
  external_url: null,
  owner: null,
  graduated: false,
}
const stageB = {
  id: 'stage-b',
  name: 'Stage B',
  description: '',
  stage_type: 'manual',
  pipeline_id: null,
  external_url: null,
  owner: null,
  graduated: false,
}
const edge = {
  id: 'edge-1',
  source_stage_id: 'stage-a',
  target_stage_id: 'stage-b',
  trigger_type: 'pipeline_completed',
  description: '',
  condition_expression: null,
  estimated_frequency: null,
  trigger_link: null,
}

const versions = [
  { id: 'ver-1', lifecycle_map_id: 'map-1', version_number: 1, stages: [stageA], edges: [edge], created_by: 'alice', created_at: '2026-01-01T00:00:00Z', notes: '' },
  { id: 'ver-2', lifecycle_map_id: 'map-1', version_number: 2, stages: [stageB], edges: [], created_by: 'alice', created_at: '2026-01-02T00:00:00Z', notes: '' },
]

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn((url: string) => {
      if (url.endsWith('/versions')) return Promise.resolve(versions)
      if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
      return Promise.resolve({ id: 'map-1', name: 'Launch Flow' })
    }),
    post: vi.fn(),
    put: vi.fn(),
  })),
}))

const VueFlowStub = defineComponent({
  name: 'VueFlow',
  props: {
    nodes: { type: Array, default: () => [] },
    edges: { type: Array, default: () => [] },
  },
  template: '<div data-testid="vue-flow-stub" />',
})

function mountEditor() {
  return mount(LifecycleMapEditor, {
    props: { mapId: 'map-1' },
    global: {
      stubs: {
        VueFlow: VueFlowStub,
        Background: true,
        Controls: true,
        StagePalette: true,
        StageConfigPanel: true,
        EdgeConfigPanel: true,
        GraduationDialog: true,
        Button: true,
      },
    },
  })
}

describe('LifecycleMapEditor version loading', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads the selected version content into the canvas', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const flow = wrapper.findComponent(VueFlowStub)
    const nodes = flow.props('nodes') as { data: { name: string } }[]
    expect(nodes).toHaveLength(1)
    expect(nodes[0].data.name).toBe('Stage A')

    const dropdown = wrapper.findComponent(VersionHistoryDropdown)
    dropdown.vm.$emit('select', 'ver-2')
    await flushPromises()

    const nodesAfter = flow.props('nodes') as { data: { name: string } }[]
    const edgesAfter = flow.props('edges') as unknown[]
    expect(nodesAfter).toHaveLength(1)
    expect(nodesAfter[0].data.name).toBe('Stage B')
    expect(edgesAfter).toHaveLength(0)
  })

  it('switches the current version id to the loaded version', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    const dropdown = wrapper.findComponent(VersionHistoryDropdown)
    dropdown.vm.$emit('select', 'ver-1')
    await flushPromises()

    const flow = wrapper.findComponent(VueFlowStub)
    const nodes = flow.props('nodes') as { data: { name: string } }[]
    const edges = flow.props('edges') as unknown[]
    expect(nodes[0].data.name).toBe('Stage A')
    expect(edges).toHaveLength(1)
  })
})
