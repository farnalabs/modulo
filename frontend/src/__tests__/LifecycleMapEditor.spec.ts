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
import { formatApiError } from '../lib/api/formatError'

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

const putMock = vi.fn()
const postMock = vi.fn()

vi.mock('../composables/useApi', () => ({
  useApi: vi.fn(() => ({
    get: vi.fn((url: string) => {
      if (url.endsWith('/versions')) return Promise.resolve(versions)
      if (url.includes('/pipelines')) return Promise.resolve({ items: [] })
      return Promise.resolve({ id: 'map-1', name: 'Launch Flow' })
    }),
    post: postMock,
    put: putMock,
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
    expect(dropdown.props('currentVersionId')).toBe('ver-1')

    dropdown.vm.$emit('select', 'ver-2')
    await flushPromises()

    expect(wrapper.findComponent(VersionHistoryDropdown).props('currentVersionId')).toBe('ver-2')

    const flow = wrapper.findComponent(VueFlowStub)
    const nodes = flow.props('nodes') as { data: { name: string } }[]
    const edges = flow.props('edges') as unknown[]
    expect(nodes).toHaveLength(1)
    expect(nodes[0].data.name).toBe('Stage B')
    expect(edges).toHaveLength(0)
  })

  it('surfaces the backend 422 validation detail in saveError', async () => {
    // FastAPI returns a Pydantic 422 as { detail: [{loc, msg, type}, ...] };
    // useApi collapses it to readable text and rejects with an Error whose
    // message is that text. The editor must surface the real Error message
    // rather than the raw "[object Object]" the API error body would stringify to.
    const validationDetail = { detail: [
      { loc: ['body', 'stages', 1, 'id'], msg: 'lifecycle-map stage #1: duplicate stage id', type: 'value_error' },
    ] }
    putMock.mockRejectedValueOnce(new Error(formatApiError(validationDetail)))

    const wrapper = mountEditor()
    await flushPromises()

    await (wrapper.vm as unknown as { handleSave: () => Promise<void> }).handleSave()
    await flushPromises()

    expect(wrapper.text()).toContain('lifecycle-map stage #1: duplicate stage id')
    expect(wrapper.text()).not.toContain('"detail"')
  })

  it('formats FastAPI array-typed 422 detail into readable messages', () => {
    expect(
      formatApiError({
        detail: [
          { loc: ['body', 'name'], msg: 'String should have at least 1 character', type: 'string_too_short' },
          { loc: ['body', 'visibility'], msg: 'String should match pattern', type: 'string_pattern_mismatch' },
        ],
      }),
    ).toBe('String should have at least 1 character; String should match pattern')
  })
})
