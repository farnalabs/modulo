import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive } from 'vue'
import { useRoute } from 'vue-router'

// FAR-1812 — read-only historic version view + version indicator chip.
//
// Like PipelineEditorViewLoad.spec.ts this mounts the view with the REAL
// useDataFetch, so the mount-time loader chain (loadPipeline / loadGraph /
// loadCatalog / ...) runs exactly as in the browser, and the snapshot-detail
// fetch that drives viewing mode is observed end to end.

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

const LIVE_NODE_ID = 'node-live'
const FROZEN_NODE_ID = 'node-frozen'

const LIVE_GRAPH = {
  nodes: [
    {
      id: LIVE_NODE_ID,
      node_type: 'agent',
      label: 'Live Node',
      description: 'the live graph',
      position: { x: 0, y: 0 },
    },
  ],
  edges: [],
}

const FROZEN_GRAPH = {
  nodes: [
    {
      id: FROZEN_NODE_ID,
      node_type: 'agent',
      label: 'Frozen Node',
      description: 'the frozen snapshot graph',
      position: { x: 40, y: 40 },
    },
  ],
  edges: [],
}

const SNAPSHOT_LIST = [
  {
    id: 'snap-9',
    pipeline_id: 'test-pipeline-id',
    snapshot_version: 9,
    tag: null,
    notes: null,
    created_at: '2026-08-02T12:00:00Z',
    created_by: 'acct-0123456789abcdef',
    version_kind: 'edit',
    created_kind: 'edit',
    draft: false,
    channel: 'none',
  },
  {
    id: 'snap-4',
    pipeline_id: 'test-pipeline-id',
    snapshot_version: 4,
    tag: 'release-b',
    notes: 'fourth version',
    created_at: '2026-08-01T12:00:00Z',
    created_by: 'acct-fedcba9876543210',
    version_kind: 'edit',
    created_kind: 'rollback',
    draft: false,
    channel: 'none',
  },
]

const SNAPSHOT_DETAIL = {
  ...SNAPSHOT_LIST[1],
  graph_json: FROZEN_GRAPH,
}

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/pipelines/:id/editor', name: 'pipeline-editor', component: PipelineEditorView }],
})

// The global setup mocks `useRoute` to return a PLAIN object, whose query
// reads are not reactive. vue-router itself provides a `shallowReactive` route
// (getters into currentRoute), so this suite points the mocked useRoute at a
// REACTIVE route instead: reading `route.query` is then tracked, and replacing
// the query object re-evaluates the mode computeds — mirroring real in-app
// navigation (View latest / the timeline's View action) rather than only
// testing cold deep-link entry.
const sharedRoute = reactive<Record<string, any>>({
  params: {} as Record<string, string>,
  path: '',
  query: {} as Record<string, string>,
})

function seedRoute(query: Record<string, string>) {
  ;(useRoute as unknown as { mockImplementation: (fn: () => unknown) => void }).mockImplementation(
    () => sharedRoute,
  )
  sharedRoute.params = { id: 'test-pipeline-id' }
  sharedRoute.path = '/pipelines/test-pipeline-id/editor'
  sharedRoute.query = { ...query }
}

// Every payload is CLONED: the editor holds the response objects by
// reference (rawNodes IS result.nodes), so a test that mutates the editable
// state must never poison the shared fixture for the next mount.
function seedApi() {
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/snapshots/{snapshot_id}')) {
      return Promise.resolve({ data: structuredClone(SNAPSHOT_DETAIL), error: undefined, response: { status: 200 } })
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

async function mountEditor(query: Record<string, string> = {}) {
  seedRoute(query)
  seedApi()
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = { pipeline_delete: true, pipeline_diff_rollback: true }
  const wrapper = mount(PipelineEditorView, {
    global: {
      plugins: [pinia, router],
      stubs: {
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

describe('PipelineEditorView — historic version view (FAR-1812)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('stays a full editor in latest mode (no version query)', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-save"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-run"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-add-node"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar-group-settings"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').exists()).toBe(false)
    // The chip shows the CURRENT version (newest snapshot, version-descending).
    const chip = wrapper.find('[data-testid="pipeline-editor-version-chip"]')
    expect(chip.exists()).toBe(true)
    expect(chip.text()).toBe('v9')
    // Latest mode renders the live graph, not the snapshot's.
    expect(vm.rawNodes.map((n: any) => n.id)).toEqual([LIVE_NODE_ID])
    wrapper.unmount()
  })

  it('renders the frozen snapshot graph while the editable state keeps the LIVE graph', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(vm.versionMissing).toBe(false)
    // Banner carries the exact required copy shape (title + saved meta + body).
    const banner = wrapper.find('[data-testid="pipeline-editor-historic-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Viewing v4 — a historical version')
    expect(banner.text()).toContain('This version is read-only.')
    expect(banner.find('[data-testid="pipeline-editor-historic-restore"]').exists()).toBe(true)
    expect(banner.find('[data-testid="pipeline-editor-historic-view-latest"]').exists()).toBe(true)
    expect(banner.find('[data-testid="pipeline-editor-historic-copy-link"]').exists()).toBe(true)
    // THE hazard: the editable refs are untouched by the snapshot.
    expect(vm.rawNodes.map((n: any) => n.id)).toEqual([LIVE_NODE_ID])
    expect(vm.viewFlowNodes.map((n: any) => n.id)).toEqual([FROZEN_NODE_ID])
    expect(vm.displayNodes.map((n: any) => n.id)).toEqual([FROZEN_NODE_ID])
    // Chip reads the VIEWED version, not the newest one.
    expect(wrapper.find('[data-testid="pipeline-editor-version-chip"]').text()).toBe('v4')
    wrapper.unmount()
  })

  it('unmounts every write affordance while viewing (absent, not disabled)', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })

    for (const testid of [
      'pipeline-editor-save',
      'pipeline-editor-save-as-template',
      'pipeline-editor-run',
      'pipeline-editor-add-node',
      'pipeline-editor-node-type-select',
      'pipeline-editor-rename',
      'pipeline-editor-toolbar-group-run',
      'pipeline-editor-toolbar-group-settings',
      'pipeline-editor-legacy-hitl-banner',
    ]) {
      expect(wrapper.find(`[data-testid="${testid}"]`).exists(), testid).toBe(false)
    }
    // Read/inspect affordances stay.
    expect(wrapper.find('[data-testid="pipeline-editor-version-timeline"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-fit-view"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the inspector read-only while viewing and editable in latest mode', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    vm.onNodeClick({ node: { id: FROZEN_NODE_ID } })
    await nextTick()
    const labelInput = wrapper.find('[data-testid="pipeline-editor-node-label"]')
    expect(labelInput.exists()).toBe(true)
    expect(labelInput.attributes('readonly')).toBeDefined()
    // Write actions inside the inspector are absent entirely.
    expect(wrapper.find('[data-testid="pipeline-editor-convert-to-agent"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-revert-to-manual"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-scope-reset"]').exists()).toBe(false)
    wrapper.unmount()

    const latest = await mountEditor()
    const latestVm = latest.vm as any
    latestVm.onNodeClick({ node: { id: LIVE_NODE_ID } })
    await nextTick()
    expect(
      latest.find('[data-testid="pipeline-editor-node-label"]').attributes('readonly'),
    ).toBeUndefined()
    expect(latest.find('[data-testid="pipeline-editor-convert-to-agent"]').exists() ||
      latest.find('[data-testid="pipeline-editor-revert-to-manual"]').exists()).toBe(true)
    latest.unmount()
  })

  it('makes saveGraph a no-op while viewing — the live graph can never be clobbered', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    await vm.saveGraph()
    await vm.saveEdgeConfig()
    expect(apiPatchMock).not.toHaveBeenCalled()
    // Still the live graph, still un-hydrated.
    expect(vm.rawNodes.map((n: any) => n.id)).toEqual([LIVE_NODE_ID])
    wrapper.unmount()
  })

  it('shows the graceful miss for an unknown snapshot (404) with a link to the latest editor', async () => {
    seedRoute({ snapshot: 'snap-deleted' })
    seedApi()
    apiGetMock.mockImplementation((url: string) => {
      if (url.includes('/snapshots/{snapshot_id}')) {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Snapshot not found' },
          response: { status: 404 },
        })
      }
      if (url.endsWith('/snapshots')) {
        return Promise.resolve({ data: { items: SNAPSHOT_LIST }, error: undefined })
      }
      if (url.includes('/pipelines/{pipeline_id}/graph')) {
        return Promise.resolve({ data: structuredClone(LIVE_GRAPH), error: undefined })
      }
      if (url.includes('/pipelines/{pipeline_id}')) {
        return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.currentTier = 'team'
    const wrapper = mount(PipelineEditorView, {
      global: {
        plugins: [pinia, router],
        stubs: { VueFlow: { template: '<div><slot /></div>' }, Background: true, Controls: true },
      },
    })
    await flushPromises()
    await nextTick()

    const missing = wrapper.find('[data-testid="pipeline-editor-version-missing"]')
    expect(missing.exists()).toBe(true)
    expect(missing.text()).toContain('This version is no longer available')
    // Never a raw error page, never a dead end: the toolbar editor is replaced
    // by the miss state, which offers the route back to the latest editor.
    expect(wrapper.find('[data-testid="pipeline-editor-toolbar"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Snapshot not found')

    ;(wrapper.vm as any).viewLatest()
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: {},
    })
    wrapper.unmount()
  })

  it('resolves the ?version= alias client-side against the snapshot list', async () => {
    const wrapper = await mountEditor({ version: '4' })
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').text()).toContain('Viewing v4')
    wrapper.unmount()
  })

  it('treats a version alias with no match as the graceful miss', async () => {
    const wrapper = await mountEditor({ version: '999' })
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-version-missing"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('lets ?snapshot= win when both query params are present', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4', version: '999' })
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').text()).toContain('Viewing v4')
    wrapper.unmount()
  })

  it('strips the version params when returning to the latest editor', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })

    const viewLatestButton = wrapper.find('[data-testid="pipeline-editor-historic-view-latest"]')
    expect(viewLatestButton.exists()).toBe(true)
    await viewLatestButton.trigger('click')

    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: {},
    })
    wrapper.unmount()
  })

  it('wires the banner Restore button to the rollback endpoint behind a confirmation', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await nextTick()
    expect(vm.showRestoreConfirm).toBe(true)
    expect(apiPostMock).not.toHaveBeenCalled()

    await vm.confirmRestore()
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback',
      expect.objectContaining({
        params: { path: { pipeline_id: 'test-pipeline-id', snapshot_id: 'snap-4' } },
      }),
    )
    // After a restore the live graph IS the restored version — land in latest.
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: {},
    })
    expect(vm.showRestoreConfirm).toBe(false)
    wrapper.unmount()
  })

  it('warns before navigating to a historic version when the live graph is dirty', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    expect(vm.isGraphDirty).toBe(false)

    // Simulate an unsaved edit on the live graph.
    vm.rawNodes.push({ id: 'node-edited', node_type: 'agent', label: 'Edited' })
    await nextTick()
    expect(vm.isGraphDirty).toBe(true)

    // Decline: navigation is refused, nothing is discarded.
    const declined = vm.requestViewSnapshot('snap-4')
    await nextTick()
    expect(vm.unsavedConfirmVisible).toBe(true)
    vm.resolveUnsavedConfirm(false)
    await declined
    expect(router.push).not.toHaveBeenCalled()
    expect(vm.rawNodes.some((n: any) => n.id === 'node-edited')).toBe(true)

    // Confirm: navigation proceeds to the canonical deep link.
    const accepted = vm.requestViewSnapshot('snap-4')
    await nextTick()
    expect(vm.unsavedConfirmVisible).toBe(true)
    vm.resolveUnsavedConfirm(true)
    await accepted
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: { snapshot: 'snap-4' },
    })
    wrapper.unmount()
  })

  it('opens the version chip popover with the snapshot metadata', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any

    const chip = wrapper.find('[data-testid="pipeline-editor-version-chip"]')
    expect(chip.attributes('aria-label')).toContain('Version 9')
    expect(chip.attributes('aria-expanded')).toBe('false')
    await chip.trigger('click')
    await nextTick()

    const popover = wrapper.find('[data-testid="pipeline-editor-version-chip-popover"]')
    expect(popover.exists()).toBe(true)
    expect(chip.attributes('aria-expanded')).toBe('true')
    expect(popover.text()).toContain('Aug 2, 2026') // created date
    expect(popover.text()).toContain('edit') // created_kind
    expect(popover.text()).toContain('#acct-012') // author rendered as a short id
    expect(popover.text()).toContain('\u2014') // tag + notes fall back to an em dash

    // "Open in timeline" opens the timeline panel.
    const openTimeline = popover.find('[data-testid="pipeline-editor-version-chip-open-timeline"]')
    expect(openTimeline.exists()).toBe(true)
    await openTimeline.trigger('click')
    expect(vm.showVersionTimeline).toBe(true)
    wrapper.unmount()
  })

  it('flips into and out of viewing mode as the route query changes (in-app navigation)', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(false)

    // A node selected in the live graph must not survive the transition — the
    // inspector would otherwise show the LIVE config while the frozen graph
    // renders on the canvas.
    vm.onNodeClick({ node: { id: LIVE_NODE_ID } })
    await nextTick()
    expect(vm.selectedNodeData).not.toBeNull()

    // Enter the historic view — what the timeline's View action navigates to.
    sharedRoute.query = { snapshot: 'snap-4' }
    await flushPromises()
    await nextTick()
    expect(vm.isViewing).toBe(true)
    expect(vm.selectedNodeData).toBeNull()
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-save"]').exists()).toBe(false)

    // Leave for latest — what the banner's View latest navigates to.
    sharedRoute.query = {}
    await flushPromises()
    await nextTick()
    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-save"]').exists()).toBe(true)
    // The editable state still holds the LIVE graph through both transitions.
    expect(vm.rawNodes.map((n: any) => n.id)).toEqual([LIVE_NODE_ID])
    wrapper.unmount()
  })

  it('copies the canonical ?snapshot= link for the viewed version', async () => {
    const wrapper = await mountEditor({ version: '4' })
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(true)

    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    await wrapper.find('[data-testid="pipeline-editor-historic-copy-link"]').trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledTimes(1)
    expect(String(writeText.mock.calls[0][0])).toContain('snapshot=snap-4')
    expect(wrapper.find('[data-testid="pipeline-editor-historic-copy-link"]').text()).toContain('Link copied')
    wrapper.unmount()
  })
})
