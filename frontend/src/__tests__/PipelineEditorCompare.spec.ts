import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive } from 'vue'
import { useRoute } from 'vue-router'

// FAR-1818 — editor-route wiring for the semantic-led version compare mode.
//
// The presentation itself is covered by
// components/PipelineSnapshotCompare.spec.ts; this suite pins the ROUTE seam:
// `?snapshot=<base>&compare=<other>` selects compare mode, `compare` defaults
// to the latest version, the banner entry point deep-links old-vs-latest, the
// pair pickers keep the route query authoritative, and leaving compare keeps
// the historic base version.

const apiGetMock = vi.hoisted(() => vi.fn())
const apiPostMock = vi.hoisted(() => vi.fn())
const apiPatchMock = vi.hoisted(() => vi.fn())
const useApiFns = vi.hoisted(() => ({
  get: vi.fn().mockResolvedValue([]),
  post: vi.fn().mockResolvedValue({}),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: apiGetMock,
    POST: apiPostMock,
    PATCH: apiPatchMock,
    PUT: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => useApiFns,
}))

import PipelineEditorView from '../views/PipelineEditorView.vue'
import { usePlanStore } from '../stores/planStore'

const LIVE_GRAPH = {
  nodes: [{ id: 'node-live', node_type: 'agent', label: 'Live Node', position: { x: 0, y: 0 } }],
  edges: [],
}
const FROZEN_GRAPH = {
  nodes: [{ id: 'node-frozen', node_type: 'agent', label: 'Frozen Node', position: { x: 40, y: 40 } }],
  edges: [],
}

const SNAPSHOT_LIST = [
  { id: 'snap-9', pipeline_id: 'test-pipeline-id', snapshot_version: 9, tag: null, notes: null, created_at: '2026-08-02T12:00:00Z', created_by: 'acct-0123456789abcdef', version_kind: 'edit', created_kind: 'edit', draft: false, channel: 'none' },
  { id: 'snap-4', pipeline_id: 'test-pipeline-id', snapshot_version: 4, tag: 'release-b', notes: 'fourth version', created_at: '2026-08-01T12:00:00Z', created_by: 'acct-fedcba9876543210', version_kind: 'edit', created_kind: 'rollback', draft: false, channel: 'none' },
]
const SNAPSHOT_DETAIL = { ...SNAPSHOT_LIST[1], graph_json: FROZEN_GRAPH }

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView }],
})

const sharedRoute = reactive<Record<string, any>>({
  params: {} as Record<string, string>,
  path: '',
  query: {} as Record<string, string>,
})

function seedRoute(query: Record<string, string>) {
  ;(useRoute as unknown as { mockImplementation: (fn: () => unknown) => void }).mockImplementation(() => sharedRoute)
  sharedRoute.params = { id: 'test-pipeline-id' }
  sharedRoute.path = '/pipelines/test-pipeline-id/editor'
  sharedRoute.query = { ...query }
}

function seedApi() {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/snapshots/{snapshot_id}')) {
      return Promise.resolve({ data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: structuredClone(FROZEN_GRAPH) }, error: undefined, response: { status: 200 } })
    }
    if (url.endsWith('/snapshots')) {
      return Promise.resolve({ data: { items: structuredClone(SNAPSHOT_LIST) }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: structuredClone(LIVE_GRAPH), error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
  apiPostMock.mockResolvedValue({ data: {}, error: undefined })
  apiPatchMock.mockResolvedValue({ data: {}, error: undefined })
}

// Stub the compare presentation so this suite is about the route seam, not the
// component's own rendering. The props drive what it would have loaded.
const CompareStub = {
  name: 'PipelineSnapshotCompare',
  props: ['pipelineId', 'baseSnapshotId', 'compareSnapshotId', 'snapshots', 'latestSnapshotId'],
  emits: ['change', 'close'],
  template: `<div data-testid="pipeline-compare-stub" :data-base="baseSnapshotId" :data-compare="compareSnapshotId === null ? 'null' : compareSnapshotId" :data-latest="latestSnapshotId === null ? 'null' : latestSnapshotId"></div>`,
}

async function mountEditor(query: Record<string, string> = {}) {
  seedRoute(query)
  seedApi()
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true }
  const wrapper = mount(PipelineEditorView, {
    global: {
      plugins: [pinia, router],
      stubs: {
        PipelineSnapshotCompare: CompareStub,
        VueFlow: { template: '<div><slot /></div>' },
        Background: true,
        Controls: true,
      },
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('PipelineEditorView — version compare mode (FAR-1818)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('stays in the plain historic view without a compare key', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(vm.compareMode).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-compare-stub"]').exists()).toBe(false)
    // The banner carries the one-click compare entry point.
    expect(wrapper.find('[data-testid="pipeline-editor-historic-compare"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('enters compare mode from the deep link and passes the resolved base + compare ids', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', compare: 'snap-9' })
    const vm = wrapper.vm as any

    expect(vm.compareMode).toBe(true)
    const stub = wrapper.find('[data-testid="pipeline-compare-stub"]')
    expect(stub.exists()).toBe(true)
    expect(stub.attributes('data-base')).toBe('snap-4')
    expect(stub.attributes('data-compare')).toBe('snap-9')
    expect(stub.attributes('data-latest')).toBe('snap-9')
    // Compare is read-only: the historic write-affordance guard still holds.
    expect(wrapper.find('[data-testid="pipeline-editor-save"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-run"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('treats an empty compare id as "compare against latest"', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', compare: '' })
    const vm = wrapper.vm as any

    expect(vm.compareMode).toBe(true)
    // The null reaches the component, which resolves the latest itself.
    expect(wrapper.find('[data-testid="pipeline-compare-stub"]').attributes('data-compare')).toBe('null')
    expect(wrapper.find('[data-testid="pipeline-compare-stub"]').attributes('data-latest')).toBe('snap-9')
    wrapper.unmount()
  })

  it('opens old-vs-latest from the banner entry point', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    await wrapper.find('[data-testid="pipeline-editor-historic-compare"]').trigger('click')
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: { snapshot: 'snap-4', compare: 'snap-9' },
    })
    wrapper.unmount()
  })

  it('updates the deep link when the pair pickers change', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', compare: 'snap-9' })
    const vm = wrapper.vm as any

    vm.updateCompare({ base: 'snap-9', compare: 'snap-4' })
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: { snapshot: 'snap-9', compare: 'snap-4' },
    })
    wrapper.unmount()
  })

  it('leaves compare mode but keeps the historic base version', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', compare: 'snap-9' })
    const vm = wrapper.vm as any

    vm.exitCompare()
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: { snapshot: 'snap-4' },
    })
    wrapper.unmount()
  })

  it('copies a share link carrying the compare id while comparing', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', compare: 'snap-9' })
    const vm = wrapper.vm as any
    const link = vm.historicShareLink()
    expect(link).toContain('snapshot=snap-4')
    expect(link).toContain('compare=snap-9')
    wrapper.unmount()
  })
})
