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
const apiPutMock = vi.hoisted(() => vi.fn())
const useApiFns = vi.hoisted(() => ({
  get: vi.fn().mockResolvedValue([]),
  post: vi.fn().mockResolvedValue({}),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: apiGetMock,
    POST: apiPostMock,
    PATCH: apiPatchMock,
    PUT: apiPutMock.mockResolvedValue({ data: {}, error: undefined }),
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

// FAR-1812 QA: a sandbox_agent node is the shape that mounts the
// runner-binding panel (it lives under the inspector's sandbox_agent block),
// so the write-affordance assertions need a graph carrying one.
const SANDBOX_NODE_ID = 'node-sandbox'
const SANDBOX_GRAPH = {
  nodes: [
    {
      id: SANDBOX_NODE_ID,
      node_type: 'sandbox_agent',
      label: 'Runner Node',
      agent_id: 'agent-frozen',
      position: { x: 80, y: 80 },
    },
  ],
  edges: [],
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

/** Any graph fixture shape (LIVE/FROZEN/SANDBOX graphs all satisfy it). */
type TestGraph = { nodes: Array<Record<string, unknown>>; edges: unknown[] }

// Every payload is CLONED: the editor holds the response objects by
// reference (rawNodes IS result.nodes), so a test that mutates the editable
// state must never poison the shared fixture for the next mount.
function seedApi(opts: { liveGraph?: TestGraph; frozenGraph?: TestGraph } = {}) {
  const liveGraph = opts.liveGraph ?? LIVE_GRAPH
  const frozenGraph = opts.frozenGraph ?? FROZEN_GRAPH
  apiGetMock.mockImplementation((url: string) => {
    if (url.includes('/snapshots/{snapshot_id}')) {
      return Promise.resolve({
        data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: structuredClone(frozenGraph) },
        error: undefined,
        response: { status: 200 },
      })
    }
    if (url.endsWith('/snapshots')) {
      return Promise.resolve({ data: { items: structuredClone(SNAPSHOT_LIST) }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: structuredClone(liveGraph), error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
  apiPostMock.mockResolvedValue({ data: {}, error: undefined })
  apiPatchMock.mockResolvedValue({ data: {}, error: undefined })
}

/** `[from, from-1, …, to]` — a version-DESCENDING history. */
function versionRange(from: number, to = 1): number[] {
  const out: number[] = []
  for (let v = from; v >= to; v -= 1) out.push(v)
  return out
}

/**
 * A version-DESCENDING snapshot history served WITH the real endpoint's
 * pagination (`page`/`page_size`, default page_size=20, `total` = all rows)
 * so the `?version=` alias resolver is exercised exactly as in production.
 * `drop` removes versions to model admin-deleted rows (gaps in the range).
 */
function seedPagedSnapshots(versions: number[], opts: { drop?: number[] } = {}) {
  const dropped = new Set(opts.drop ?? [])
  const rows = versions
    .filter((v) => !dropped.has(v))
    .map((v) => ({
      id: `snap-v${v}`,
      pipeline_id: 'test-pipeline-id',
      snapshot_version: v,
      tag: null,
      notes: null,
      created_at: '2026-08-01T12:00:00Z',
      created_by: 'acct-0123456789abcdef',
      version_kind: 'edit',
      created_kind: 'edit',
      draft: false,
      channel: 'none',
    }))
  apiGetMock.mockImplementation((url: string, init?: any) => {
    if (url.includes('/snapshots/{snapshot_id}')) {
      const id = init?.params?.path?.snapshot_id
      const row = rows.find((r) => r.id === id)
      if (!row) {
        return Promise.resolve({
          data: undefined,
          error: { detail: 'Snapshot not found' },
          response: { status: 404 },
        })
      }
      return Promise.resolve({
        data: { ...structuredClone(row), graph_json: structuredClone(FROZEN_GRAPH) },
        error: undefined,
        response: { status: 200 },
      })
    }
    if (url.endsWith('/snapshots')) {
      const query = (init?.params?.query ?? {}) as { page?: number; page_size?: number }
      const page = query.page ?? 1
      const pageSize = query.page_size ?? 20
      const items = structuredClone(rows.slice((page - 1) * pageSize, page * pageSize))
      return Promise.resolve({ data: { items, total: rows.length }, error: undefined })
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

async function mountEditor(
  query: Record<string, string> = {},
  afterSeed?: () => void,
  features: Record<string, boolean> = { pipeline_delete: true },
) {
  seedRoute(query)
  seedApi()
  afterSeed?.()
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.currentTier = 'team'
  store.features = features
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

/**
 * FAR-1816: route the restore dialog's two POSTs independently — the diff
 * summary fetched on open and the rollback posted on confirm. `mockResolvedValue`
 * cannot express the two different responses, so drive them by URL.
 */
function mockRestoreEndpoints(opts: {
  diff?: unknown
  diffError?: unknown
  diffThrows?: Error
  rollback?: unknown
  rollbackError?: unknown
  rollbackThrows?: Error
} = {}) {
  apiPostMock.mockImplementation((url: string) => {
    if (url.includes('/snapshots/diff')) {
      if (opts.diffThrows) return Promise.reject(opts.diffThrows)
      if (opts.diffError) return Promise.resolve({ data: undefined, error: opts.diffError })
      return Promise.resolve({ data: opts.diff ?? {}, error: undefined })
    }
    if (url.includes('/rollback')) {
      if (opts.rollbackThrows) return Promise.reject(opts.rollbackThrows)
      if (opts.rollbackError) return Promise.resolve({ data: undefined, error: opts.rollbackError })
      return Promise.resolve({ data: opts.rollback ?? {}, error: undefined })
    }
    return Promise.resolve({ data: {}, error: undefined })
  })
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
    // The frozen graph here carries a sandbox_agent node — the shape that
    // mounts the runner-binding panel in the node inspector.
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () => seedApi({ frozenGraph: SANDBOX_GRAPH }))

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

    // Inspector surface: the runner-binding panel (add-row inputs, per-row
    // remove, Save → PUT /agents/{id}/bindings) must be ABSENT too — it
    // mutates the LIVE agent, and its data is the live agent's rows, which
    // the snapshot does not freeze. Open the node to mount the inspector.
    ;(wrapper.vm as any).onNodeClick({ node: { id: SANDBOX_NODE_ID } })
    await nextTick()
    for (const testid of [
      'pipeline-editor-runner-bindings',
      'pipeline-editor-runner-bindings-rows',
      'pipeline-editor-runner-binding-add',
      'pipeline-editor-runner-binding-save',
      'pipeline-editor-runner-binding-target-input',
      'pipeline-editor-runner-binding-source-input',
      'pipeline-editor-runner-binding-backend',
    ]) {
      expect(wrapper.find(`[data-testid="${testid}"]`).exists(), testid).toBe(false)
    }
    // No write issued, and no live-state READ either (the component never
    // mounted — absent, not disabled).
    expect(apiPutMock).not.toHaveBeenCalled()
    expect(
      apiGetMock.mock.calls.filter((call) => call[0] === '/api/v1/agents/{agent_id}/bindings'),
    ).toHaveLength(0)
    wrapper.unmount()

    // Non-vacuous check: the SAME node in latest mode mounts the full write
    // panel — proving the view-mode absence above is the v-if, not a fixture
    // that never rendered the surface in the first place.
    const latest = await mountEditor({}, () => seedApi({ liveGraph: SANDBOX_GRAPH }))
    ;(latest.vm as any).onNodeClick({ node: { id: SANDBOX_NODE_ID } })
    await nextTick()
    expect(latest.find('[data-testid="pipeline-editor-runner-bindings"]').exists()).toBe(true)
    expect(latest.find('[data-testid="pipeline-editor-runner-binding-save"]').exists()).toBe(true)
    latest.unmount()
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

  it('resolves a ?version= deeper than the first list page (bounded search, not the loaded page)', async () => {
    // 45 versions: loadCatalog's default first page (page_size=20) holds
    // v45…v26 — v3 EXISTS but is not on the page the old page-1-only
    // resolver searched, so it falsely reported "no longer available".
    const wrapper = await mountEditor({ version: '3' }, () => seedPagedSnapshots(versionRange(45)))
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(vm.versionMissing).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').text()).toContain('Viewing v3')
    wrapper.unmount()
  })

  it('resolves a ?version= beyond the 100-row lookup page via the bounded page search', async () => {
    // 250 versions: v42 sits at rank 209 — deeper than page 1 at
    // page_size=100, so a bigger-page-only fix still misses it. The bounded
    // search pages to it (<= log2(pageCount) probes, hard-capped) instead of
    // reporting it missing.
    const wrapper = await mountEditor({ version: '42' }, () => seedPagedSnapshots(versionRange(250)))
    const vm = wrapper.vm as any

    expect(vm.isViewing).toBe(true)
    expect(vm.versionMissing).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').text()).toContain('Viewing v42')
    wrapper.unmount()
  })

  it('still shows the graceful miss for a genuinely absent version beyond the first page', async () => {
    // v42 deleted from an otherwise contiguous 250-version history: the
    // search reaches the page whose range contains 42, finds no such row
    // (the list is sorted — absence there is absence everywhere), and
    // reports the graceful miss: not a false hit, not a raw error.
    const wrapper = await mountEditor(
      { version: '42' },
      () => seedPagedSnapshots(versionRange(250), { drop: [42] }),
    )
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

  it('opens the shared restore dialog with the old-vs-latest diff summary and confirms the rollback', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({
      diff: {
        nodes_added: [{ id: 'frozen-1' }, { id: 'frozen-2' }],
        nodes_removed: [{ id: 'live-1' }],
        semantic: {
          impacted_nodes: ['down-1'],
          breaking_changes: [{ severity: 'block', reason: 'Input port removed' }],
        },
      },
      rollback: { snapshot_version: 10 },
    })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()

    expect(vm.showRestoreConfirm).toBe(true)
    // The summary compares the LATEST version (snap-9, snapshot_a) against the
    // version being restored (snap-4, snapshot_b), so `nodes_added` are what
    // the restore brings back.
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/diff',
      expect.objectContaining({
        params: { path: { pipeline_id: 'test-pipeline-id' } },
        body: { snapshot_a_id: 'snap-9', snapshot_b_id: 'snap-4' },
      }),
    )
    // PrimeVue Dialog teleports its content to <body>, so read it there.
    const summaryText = document.querySelector('[data-testid="pipeline-editor-restore-diff"]')?.textContent ?? ''
    expect(summaryText).toContain('2 nodes will be restored from this version')
    expect(summaryText).toContain('1 node will stop being used')
    expect(summaryText).toContain('1 downstream node affected')
    expect(summaryText).toContain('Input port removed')
    // No rollback has been posted yet.
    expect(
      apiPostMock.mock.calls.some((c) => String(c[0]).includes('/rollback')),
    ).toBe(false)

    await vm.confirmRestore()
    await flushPromises()

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback',
      expect.objectContaining({
        params: { path: { pipeline_id: 'test-pipeline-id', snapshot_id: 'snap-4' } },
      }),
    )
    expect(vm.showRestoreConfirm).toBe(false)
    // Toast names the exact outcome: restored v4, created v10.
    expect(vm.restoreNotice).toBe('Restored v4. Created v10.')
    expect(wrapper.find('[data-testid="pipeline-editor-restore-notice"]').text()).toBe('Restored v4. Created v10.')
    // After a restore the live graph IS the restored version — land in latest.
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: {},
    })
    wrapper.unmount()
  })

  it('closing the dialog without confirming posts no rollback and shows no notice', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({ diff: { nodes_added: [], nodes_removed: [], semantic: {} } })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    expect(vm.showRestoreConfirm).toBe(true)

    // Cancel: close the dialog without confirming.
    vm.showRestoreConfirm = false
    await nextTick()
    expect(vm.restoreNotice).toBeNull()
    expect(apiPostMock.mock.calls.some((c) => String(c[0]).includes('/rollback'))).toBe(false)
    wrapper.unmount()
  })

  it('opens the shared restore dialog from the timeline restore action (FAR-1816)', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    mockRestoreEndpoints({ diff: { nodes_added: [], nodes_removed: [], semantic: {} } })

    await wrapper.find('[data-testid="pipeline-editor-version-timeline"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="snapshot-timeline-row-snap-4"]').trigger('click')
    await wrapper.find('[data-testid="snapshot-timeline-rollback"]').trigger('click')
    await flushPromises()

    expect(vm.showRestoreConfirm).toBe(true)
    // The timeline's selection (snap-4) drives the dialog, not the resolved
    // (latest-mode) snapshot.
    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/diff',
      expect.objectContaining({ body: { snapshot_a_id: 'snap-9', snapshot_b_id: 'snap-4' } }),
    )
    wrapper.unmount()
  })

  it('still confirms the restore when the diff summary cannot be loaded', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({ diffError: { detail: 'diff unavailable' }, rollback: { snapshot_version: 11 } })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    expect(document.querySelector('[data-testid="pipeline-editor-restore-diff"]')?.textContent ?? '').toContain(
      'Could not load the change summary: diff unavailable',
    )

    await vm.confirmRestore()
    await flushPromises()
    expect(vm.showRestoreConfirm).toBe(false)
    expect(vm.restoreNotice).toBe('Restored v4. Created v11.')
    wrapper.unmount()
  })

  it('surfaces a HITL-gate weakening refusal as human copy, never the raw server string', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({
      rollbackError: { detail: 'Gate weakening denied (legacy-snapshot-ambiguous). Affected edges: a→b' },
    })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    await vm.confirmRestore()
    await flushPromises()

    expect(vm.restoreError).toBe(
      "This version can't be restored: it would remove a human-review gate. Ask an admin, or restore a version that keeps the gate.",
    )
    expect(vm.restoreError).not.toContain('Gate weakening denied')
    expect(vm.showRestoreConfirm).toBe(true)
    wrapper.unmount()
  })

  it('surfaces a connector-team mismatch refusal as human copy', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({
      rollbackError: {
        detail: "connector_team_mismatch: connector 'gh' (id=abc) is team-private (owner team t1) but pipeline is owned by team t2",
      },
    })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    await vm.confirmRestore()
    await flushPromises()

    expect(vm.restoreError).toContain("it uses a connector owned by another team")
    expect(vm.restoreError).not.toContain('connector_team_mismatch')
    wrapper.unmount()
  })

  it('surfaces a guardrail-binding strip refusal as human copy', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({
      rollbackError: {
        detail: 'Non-admin cannot strip a guardrail binding: removing node(s) n1 would drop a node-bound guardrail.',
      },
    })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    await vm.confirmRestore()
    await flushPromises()

    expect(vm.restoreError).toContain('would strip a guardrail bound to a node')
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

// ---------------------------------------------------------------------------
// FAR-1812 follow-up: branch/line coverage for the historic-version seam.
// The suite above covers the happy paths; this one pins the defensive arms
// (superseded requests, load failures, absent pages, author/date fallbacks,
// restore failure, copy-link failure + reset, all write-guard early returns,
// and the read-only inspector authoring surfaces in latest mode).
// ---------------------------------------------------------------------------

const AGENT_NODE_ID = 'node-agent'
const AGENT_GRAPH = {
  nodes: [
    {
      id: AGENT_NODE_ID,
      node_type: 'sandbox_agent',
      label: 'Agent Node',
      agent_id: 'agent-1',
      position: { x: 0, y: 0 },
      capability_scope: {
        allowed_connectors: [],
        allowed_tools: ['tool-x'],
        context_scope: ['ctx-y'],
      },
      parameter_set_id: 'ps-1',
      parameter_overrides: { alpha: 'v' },
    },
  ],
  edges: [],
}

/** A two-node graph carrying one edge, for the edge-inspector panel. */
const EDGE_GRAPH = {
  nodes: [
    { id: 'node-a', node_type: 'agent', label: 'A', position: { x: 0, y: 0 } },
    { id: 'node-b', node_type: 'agent', label: 'B', position: { x: 40, y: 0 } },
  ],
  edges: [{ id: 'edge-1', source_node_id: 'node-a', target_node_id: 'node-b', edge_type: 'normal' }],
}

/** A detail/snapshot-list override injected via the `afterSeed` hook. */
function seedApiWith(overrides: {
  detail?: (init?: any) => unknown
  snapshots?: (init?: any) => unknown
  graph?: TestGraph
} = {}) {
  apiGetMock.mockImplementation((url: string, init?: any) => {
    if (url.includes('/snapshots/{snapshot_id}')) {
      if (overrides.detail) return overrides.detail(init)
      return Promise.resolve({
        data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: structuredClone(FROZEN_GRAPH) },
        error: undefined,
        response: { status: 200 },
      })
    }
    if (url.endsWith('/snapshots')) {
      if (overrides.snapshots) return overrides.snapshots(init)
      return Promise.resolve({ data: { items: structuredClone(SNAPSHOT_LIST) }, error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}/graph')) {
      return Promise.resolve({ data: structuredClone(overrides.graph ?? LIVE_GRAPH), error: undefined })
    }
    if (url.includes('/pipelines/{pipeline_id}')) {
      return Promise.resolve({ data: { id: 'test-pipeline-id', name: 'Test Pipeline' }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  })
  apiPostMock.mockResolvedValue({ data: {}, error: undefined })
  apiPatchMock.mockResolvedValue({ data: {}, error: undefined })
}

/** A deferred promise the test resolves at will (controls request ordering). */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('PipelineEditorView — FAR-1812 defensive arms', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('keeps every write entry point a no-op while a historic version is shown', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    vm.syncNodeToFlow()
    vm.addNode('agent')
    await vm.handleSaveAsComposite()
    await vm.convertToAgent()
    await vm.triggerRun()

    expect(apiPostMock).not.toHaveBeenCalled()
    expect(apiPatchMock).not.toHaveBeenCalled()
    expect(vm.rawNodes.map((n: any) => n.id)).toEqual([LIVE_NODE_ID])
    wrapper.unmount()
  })

  it('renders the capability-scope and parameter-set authoring surfaces in latest mode', async () => {
    const wrapper = await mountEditor({}, () => seedApi({ liveGraph: AGENT_GRAPH }))
    const vm = wrapper.vm as any

    vm.onNodeClick({ node: { id: AGENT_NODE_ID } })
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-capability-scope"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-scope-tool-remove-tool-x"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-scope-tool-input"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-save-param-set"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('hides those authoring controls when the same agent node is frozen in a historic version', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () =>
          Promise.resolve({
            data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: structuredClone(AGENT_GRAPH) },
            error: undefined,
            response: { status: 200 },
          }),
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(true)
    vm.onNodeClick({ node: { id: AGENT_NODE_ID } })
    await nextTick()

    // The panels still render (the frozen config is inspectable) …
    expect(wrapper.find('[data-testid="pipeline-editor-capability-scope"]').exists()).toBe(true)
    // … but every authoring control is absent, not merely disabled.
    expect(wrapper.find('[data-testid="pipeline-editor-scope-tool-remove-tool-x"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-scope-tool-input"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-editor-scope-context-remove-ctx-y"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-save-param-set"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the save-edge action only when an edge is selected in latest mode', async () => {
    const wrapper = await mountEditor({}, () => seedApi({ liveGraph: EDGE_GRAPH }))
    const vm = wrapper.vm as any
    expect(wrapper.find('[data-testid="pipeline-editor-save-edge"]').exists()).toBe(false)

    vm.onEdgeClick({ edge: { id: 'edge-1' } })
    await nextTick()
    // The edge write controls live behind the HITL-gate section.
    vm.edgeForm.hitl_enabled = true
    await nextTick()
    expect(wrapper.find('[data-testid="pipeline-editor-save-edge"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows the restore action regardless of plan features (FAR-1819: gate retired)', async () => {
    // The old pipeline_diff_rollback gate hid this button on Community-tier
    // plans; the flag is retired, so the action renders even when the plan
    // feature map has no entry for it.
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, undefined, {
      pipeline_delete: true,
    })
    expect(wrapper.find('[data-testid="pipeline-editor-historic-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-historic-restore"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a clipboard failure inline instead of a silent no-op', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })

    await wrapper.find('[data-testid="pipeline-editor-historic-copy-link"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="pipeline-editor-historic-copy-link-error"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('resets the copied label after the reset window and clears the prior timer', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })

    vi.useFakeTimers()
    try {
      await vm.copyHistoricLink()
      expect(vm.linkCopied).toBe(true)
      await vm.copyHistoricLink()
      expect(vm.linkCopied).toBe(true)
      vi.advanceTimersByTime(2100)
      await nextTick()
      expect(vm.linkCopied).toBe(false)
    } finally {
      vi.useRealTimers()
    }
    wrapper.unmount()
  })

  it('builds the share link without a snapshot id when none is resolved', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    const link = vm.historicShareLink()
    expect(link).not.toContain('?')
    expect(link).toContain('/editor')
    wrapper.unmount()
  })

  it('resolves a pending unsaved-changes prompt to "decline" on unmount', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    vm.rawNodes.push({ id: 'node-edited', node_type: 'agent', label: 'Edited' })
    await nextTick()
    expect(vm.isGraphDirty).toBe(true)

    const pending = vm.requestViewSnapshot('snap-4')
    await nextTick()
    expect(vm.unsavedConfirmVisible).toBe(true)

    wrapper.unmount()
    await expect(pending).resolves.toBeUndefined()
  })

  it('routes the display nodes/edges v-model setters to the frozen refs while viewing', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any

    vm.displayNodes = [{ id: 'frozen-node' }]
    vm.displayEdges = [{ id: 'frozen-edge' }]
    expect(vm.viewFlowNodes).toEqual([{ id: 'frozen-node' }])
    expect(vm.viewFlowEdges).toEqual([{ id: 'frozen-edge' }])
    // Reading the inspector sources while viewing must take the frozen arm.
    expect(vm.inspectSourceNodes.map((n: any) => n.id)).toEqual([FROZEN_NODE_ID])
    expect(vm.inspectSourceEdges).toEqual([])
    wrapper.unmount()

    const latest = await mountEditor()
    const latestVm = latest.vm as any
    latestVm.displayNodes = [{ id: 'live-node' }]
    latestVm.displayEdges = [{ id: 'live-edge' }]
    expect(latestVm.flowNodes).toEqual([{ id: 'live-node' }])
    expect(latestVm.flowEdges).toEqual([{ id: 'live-edge' }])
    latest.unmount()
  })

  it('treats a malformed ?version= alias as the graceful miss', async () => {
    const wrapper = await mountEditor({ version: 'abc' })
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-version-missing"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('accepts an array-valued query param and strips both version params (keeping others)', async () => {
    const wrapper = await mountEditor({ snapshot: ['snap-4'] as unknown as string })
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(true)
    wrapper.unmount()

    const mixed = await mountEditor({ snapshot: 'snap-4', version: '4', foo: 'bar' })
    await mixed.find('[data-testid="pipeline-editor-historic-view-latest"]').trigger('click')
    expect(router.push).toHaveBeenCalledWith({
      path: '/pipelines/test-pipeline-id/editor',
      query: { foo: 'bar' },
    })
    mixed.unmount()
  })

  it('renders the unknown-author and missing-date fallbacks for a snapshot without them', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () =>
          Promise.resolve({
            data: {
              ...structuredClone(SNAPSHOT_DETAIL),
              created_at: null,
              created_by: null,
              graph_json: structuredClone(FROZEN_GRAPH),
            },
            error: undefined,
            response: { status: 200 },
          }),
      }),
    )
    const banner = wrapper.find('[data-testid="pipeline-editor-historic-banner"]')
    expect(banner.exists()).toBe(true)
    // Unknown author renders the i18n label, not the raw `null`.
    expect(banner.text()).not.toContain('null')
    wrapper.unmount()
  })

  it('reports the graceful miss when the pipeline has no snapshots at all', async () => {
    const wrapper = await mountEditor({ version: '5' }, () => seedPagedSnapshots([]))
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-editor-version-missing"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports the graceful miss for a version inside page 1 that is absent', async () => {
    const wrapper = await mountEditor(
      { version: '15' },
      () => seedPagedSnapshots(versionRange(30), { drop: [15] }),
    )
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    wrapper.unmount()
  })

  it('searches upward in the snapshot history (version above the landed page)', async () => {
    const wrapper = await mountEditor(
      { version: '895' },
      () => seedPagedSnapshots(versionRange(1000), { drop: [895] }),
    )
    const vm = wrapper.vm as any
    // v895 was admin-deleted: the search must terminate on the graceful miss,
    // never a false hit.
    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(true)
    wrapper.unmount()
  })

  it('treats a lookup page that omits its items array as an empty page', async () => {
    const wrapper = await mountEditor({ version: '5' }, () =>
      seedApiWith({ snapshots: () => Promise.resolve({ data: { total: 1 }, error: undefined }) }),
    )
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    wrapper.unmount()
  })

  it('fails the alias resolution loudly when the lookup page cannot be loaded', async () => {
    const wrapper = await mountEditor({ version: '5' }, () =>
      seedApiWith({
        snapshots: () => Promise.resolve({ data: undefined, error: { detail: 'list down' } }),
      }),
    )
    const vm = wrapper.vm as any
    // A failed search is NOT the graceful miss — otherwise "no longer
    // available" would be a lie.
    expect(vm.versionMissing).toBe(false)
    expect(vm.isViewing).toBe(false)
    expect(wrapper.text()).toContain('could not be loaded')
    wrapper.unmount()
  })

  it('abandons a bounded search whose pages are empty (probe budget)', async () => {
    const wrapper = await mountEditor({ version: '5' }, () =>
      seedApiWith({
        snapshots: (init?: any) => {
          const page = init?.params?.query?.page ?? 1
          if (page === 1) {
            return Promise.resolve({
              data: { items: [{ ...structuredClone(SNAPSHOT_LIST[0]), snapshot_version: 1000000 }], total: 10000000 },
              error: undefined,
            })
          }
          return Promise.resolve({ data: { items: [], total: 10000000 }, error: undefined })
        },
      }),
    )
    const vm = wrapper.vm as any
    // The search refuses to guess "missing": an exhausted budget is a load error.
    expect(vm.versionMissing).toBe(false)
    wrapper.unmount()
  })

  it('abandons a bounded search that keeps landing below the target (probe budget)', async () => {
    const wrapper = await mountEditor({ version: '5' }, () =>
      seedApiWith({
        snapshots: (init?: any) => {
          const page = init?.params?.query?.page ?? 1
          if (page === 1) {
            return Promise.resolve({
              data: { items: [{ ...structuredClone(SNAPSHOT_LIST[0]), snapshot_version: 1000000 }], total: 10000000 },
              error: undefined,
            })
          }
          return Promise.resolve({
            data: { items: [{ ...structuredClone(SNAPSHOT_LIST[0]), snapshot_version: 1 }], total: 10000000 },
            error: undefined,
          })
        },
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(false)
    wrapper.unmount()
  })

  it('surfaces a non-404 snapshot-detail error as a load failure', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () =>
          Promise.resolve({ data: undefined, error: { detail: 'detail down' }, response: { status: 500 } }),
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(false)
    expect(wrapper.text()).toContain('detail down')
    wrapper.unmount()
  })

  it('treats a snapshot-detail response with no body as the graceful miss', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () => Promise.resolve({ data: undefined, error: undefined, response: { status: 200 } }),
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    wrapper.unmount()
  })

  it('treats a snapshot without a graph object as the graceful miss', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () =>
          Promise.resolve({
            data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: 'not-an-object' },
            error: undefined,
            response: { status: 200 },
          }),
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.versionMissing).toBe(true)
    wrapper.unmount()
  })

  it('defaults missing node/edge arrays in a snapshot graph to empty lists', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({
        detail: () =>
          Promise.resolve({
            data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: { nodes: 'x', edges: 'y' } },
            error: undefined,
            response: { status: 200 },
          }),
      }),
    )
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(true)
    expect(vm.viewRawNodes).toEqual([])
    expect(vm.viewRawEdges).toEqual([])
    wrapper.unmount()
  })

  it('ignores a snapshot-detail response superseded by newer navigation', async () => {
    const gate = deferred<any>()
    const wrapper = await mountEditor({}, () => seedApiWith({ detail: () => gate.promise }))
    const vm = wrapper.vm as any

    sharedRoute.query = { snapshot: 'snap-4' }
    await flushPromises()
    await nextTick()

    // Leave for latest before the detail request settles.
    sharedRoute.query = {}
    await flushPromises()
    await nextTick()

    gate.resolve({
      data: { ...structuredClone(SNAPSHOT_DETAIL), graph_json: structuredClone(FROZEN_GRAPH) },
      error: undefined,
      response: { status: 200 },
    })
    await flushPromises()

    expect(vm.isViewing).toBe(false)
    expect(vm.viewSnapshot).toBeNull()
    wrapper.unmount()
  })

  it('surfaces a thrown snapshot-detail request as a load failure', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' }, () =>
      seedApiWith({ detail: () => Promise.reject(new Error('socket down')) }),
    )
    const vm = wrapper.vm as any
    expect(vm.isViewing).toBe(false)
    expect(vm.versionMissing).toBe(false)
    wrapper.unmount()
  })

  it('surfaces a rollback error returned by the endpoint inside the dialog', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({ rollbackError: { detail: 'rollback-refused' } })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    await vm.confirmRestore()

    expect(vm.restoreError).toBe('Failed to restore this version: rollback-refused')
    expect(vm.showRestoreConfirm).toBe(true)
    expect(document.body.textContent).toContain('rollback-refused')
    wrapper.unmount()
  })

  it('surfaces a thrown rollback request inside the dialog', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    mockRestoreEndpoints({ rollbackThrows: new Error('socket down') })

    await wrapper.find('[data-testid="pipeline-editor-historic-restore"]').trigger('click')
    await flushPromises()
    await vm.confirmRestore()

    expect(vm.restoreError).toBe('Failed to restore this version: socket down')
    wrapper.unmount()
  })

  it('does nothing when restore is confirmed with no resolved snapshot', async () => {
    const wrapper = await mountEditor()
    const vm = wrapper.vm as any
    await vm.confirmRestore()
    expect(apiPostMock).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('renders the restore dialog body with an empty version when the snapshot vanishes', async () => {
    const wrapper = await mountEditor({ snapshot: 'snap-4' })
    const vm = wrapper.vm as any
    vm.openRestoreConfirm()
    await nextTick()

    sharedRoute.query = {}
    await flushPromises()
    await nextTick()

    expect(vm.showRestoreConfirm).toBe(true)
    expect(vm.viewSnapshot).toBeNull()
    wrapper.unmount()
  })
})
