import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

// FAR-1818 — semantic-led version compare presentation.
//
// Proves the three contract points the ticket calls out: (1) the semantic
// layer (breaking changes + impacted downstream nodes) is presented FIRST;
// (2) structural changes are grouped per node and unchanged nodes collapse;
// (3) the raw JSON diff is LAST and hidden behind a toggle that is OFF by
// default. It also pins the empty/error states, the "compare defaults to
// latest" rule, the request sequencing/timeout behaviour, the whole-history
// picker, and the corrected port-signature headline.

const { apiGet, apiPost } = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }))

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: apiGet,
    POST: apiPost,
    PATCH: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
  getAccessToken: vi.fn(() => 'mock-token'),
}))

// Faithful PrimeVue Select stub: forwards attrs (so data-testid/aria-label
// land on the select) and emits the option's value field when option-value is
// set (the AppSelect contract).
const SelectStub = {
  name: 'SelectStub',
  inheritAttrs: false,
  props: ['modelValue', 'options', 'placeholder', 'optionLabel', 'optionValue', 'label'],
  emits: ['update:modelValue'],
  template: `
    <select v-bind="$attrs" @change="$emit('update:modelValue', optionValue ? options[Number($event.target.value)]?.[optionValue] : options[Number($event.target.value)])">
      <option v-for="(o, i) in options" :key="i" :value="i">{{ optionLabel ? o[optionLabel] : (o.label ?? o) }}</option>
    </select>`,
}

import PipelineSnapshotCompare from '../../components/pipeline/PipelineSnapshotCompare.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'

const SNAPSHOTS = [
  { id: 'snap-9', snapshot_version: 9, tag: null, created_at: '2026-08-02T12:00:00Z' },
  { id: 'snap-4', snapshot_version: 4, tag: 'release-b', created_at: '2026-08-01T12:00:00Z' },
]

const DIFF = {
  snapshot_a: {
    version: 4,
    graph: {
      nodes: [
        { id: 'n1', label: 'Start', node_type: 'manual' },
        { id: 'n2', label: 'Old Node', node_type: 'agent' },
        { id: 'n3', label: 'Steady', node_type: 'agent' },
      ],
      edges: [],
    },
  },
  snapshot_b: {
    version: 9,
    graph: {
      nodes: [
        { id: 'n1', label: 'Start', node_type: 'manual' },
        { id: 'n3', label: 'Steady', node_type: 'agent' },
        { id: 'n4', label: 'New Node', node_type: 'agent' },
      ],
      edges: [],
    },
  },
  nodes_added: [{ id: 'n4', label: 'New Node', node_type: 'agent' }],
  nodes_removed: [{ id: 'n2', label: 'Old Node', node_type: 'agent' }],
  nodes_modified: [
    {
      node_id: 'n1',
      changes: {
        label: { old: 'Start', new: 'Begin' },
        ports: [{ node_id: 'n1', direction: 'output', port: 'out', change: 'removed', old: 'x', new: null }],
      },
    },
  ],
  edges_added: [{ source: 'n1', target: 'n4' }],
  edges_removed: [],
  edges_modified: [],
  semantic: {
    port_changes: [
      { kind: 'node_signature', node_id: 'n1', direction: 'output', port: 'out', change: 'removed', old: 'x', new: null },
    ],
    impacted_nodes: ['n3', 'n4'],
    breaking_changes: [
      { severity: 'block', node_id: 'n1', direction: 'output', port: 'out', reason: 'output port removed' },
    ],
  },
}

function mountCompare(overrides: Record<string, unknown> = {}) {
  return mount(PipelineSnapshotCompare, {
    props: {
      pipelineId: 'pipe-1',
      baseSnapshotId: 'snap-4',
      compareSnapshotId: 'snap-9',
      snapshots: SNAPSHOTS,
      latestSnapshotId: 'snap-9',
      ...overrides,
    },
    global: { stubs: { Select: SelectStub } },
  })
}

async function flushCompare(overrides: Record<string, unknown> = {}) {
  const wrapper = mountCompare(overrides)
  await flushPromises()
  return wrapper
}

describe('PipelineSnapshotCompare', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiPost.mockResolvedValue({ data: DIFF, error: undefined })
    // The component pages the snapshot history itself; a default empty history
    // keeps the picker on the host-provided props.
    apiGet.mockResolvedValue({ data: { items: [], total: 0 }, error: undefined })
  })

  it('fetches the diff for the given pair and posts the real snapshot ids', async () => {
    await flushCompare()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/diff',
      expect.objectContaining({
        params: { path: { pipeline_id: 'pipe-1' } },
        body: { snapshot_a_id: 'snap-4', snapshot_b_id: 'snap-9' },
      }),
    )
  })

  it('shows a loading status region while the diff is in flight', () => {
    const wrapper = mountCompare()
    const loading = wrapper.find('[data-testid="pipeline-compare-loading"]')
    expect(loading.exists()).toBe(true)
    // <output> carries the implicit status role (Sonar Web:S6819 prefers the
    // native element over an explicit role="status").
    expect(loading.element.tagName).toBe('OUTPUT')
  })

  it('defaults the compare side to the latest version when no id is in the query', async () => {
    await flushCompare({ compareSnapshotId: null, latestSnapshotId: 'snap-9' })
    expect(apiPost).toHaveBeenCalledWith(
      '/api/v1/pipelines/{pipeline_id}/snapshots/diff',
      expect.objectContaining({ body: { snapshot_a_id: 'snap-4', snapshot_b_id: 'snap-9' } }),
    )
  })

  it('does not request a diff (and explains why) when there is no compare target', async () => {
    const wrapper = await flushCompare({ compareSnapshotId: null, latestSnapshotId: null })
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="pipeline-compare-no-target"]').exists()).toBe(true)
  })

  it('leads with the semantic layer: breaking changes and impacted nodes BEFORE structural changes', async () => {
    const wrapper = await flushCompare()
    const semantic = wrapper.find('[data-testid="pipeline-compare-semantic"]')
    const structural = wrapper.find('[data-testid="pipeline-compare-structural"]')
    const raw = wrapper.find('[data-testid="pipeline-compare-raw-section"]')
    expect(semantic.exists()).toBe(true)
    expect(structural.exists()).toBe(true)
    expect(raw.exists()).toBe(true)

    // Presentation ORDER: semantic -> structural -> raw.
    const following = Node.DOCUMENT_POSITION_FOLLOWING
    expect(semantic.element.compareDocumentPosition(structural.element) & following).toBeTruthy()
    expect(structural.element.compareDocumentPosition(raw.element) & following).toBeTruthy()

    // The breaking change and the impact are inside the semantic block.
    expect(semantic.find('[data-testid="pipeline-compare-breaking-item"]').text()).toContain('output port removed')
    expect(semantic.find('[data-testid="pipeline-compare-breaking-item"]').text()).toContain('block')
    expect(semantic.find('[data-testid="pipeline-compare-impacted"]').text()).toContain('Impacted downstream nodes (2)')
    expect(semantic.find('[data-testid="pipeline-compare-impacted"]').text()).toContain('Steady')
    expect(semantic.find('[data-testid="pipeline-compare-impacted"]').text()).toContain('New Node')
  })

  it('summarises the change as a pluralised narrative, not a JSON wall', async () => {
    const wrapper = await flushCompare()
    const summary = wrapper.find('[data-testid="pipeline-compare-summary"]')
    expect(summary.text()).toContain('1 node added')
    expect(summary.text()).toContain('1 node removed')
    expect(summary.text()).toContain('1 node modified')
    expect(summary.text()).toContain('1 edge added')
    expect(summary.text()).toContain('0 edges removed')
    expect(summary.text()).toContain('0 edges modified')
    expect(summary.text()).toContain('port signature changed on 1 node')
    expect(summary.text()).toContain('2 downstream nodes are impacted')
    // The raw JSON never leaks into the default presentation.
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)
    expect(summary.text()).not.toContain('nodes_added')
  })

  it('groups structural changes per node and populates the unchanged collapse', async () => {
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-nodes-added"]').text()).toContain('New Node')
    expect(wrapper.find('[data-testid="pipeline-compare-nodes-removed"]').text()).toContain('Old Node')
    const modified = wrapper.find('[data-testid="pipeline-compare-node-modified"]')
    expect(modified.text()).toContain('Start')
    expect(modified.text()).toContain('Label: Start → Begin')
    // The removed port renders as a port change, not raw JSON.
    expect(modified.text()).toContain("output port 'out' removed")
    // n3 is in both versions and unchanged -> a singular, non-empty collapse.
    const unchanged = wrapper.find('[data-testid="pipeline-compare-unchanged"]')
    expect(unchanged.text()).toContain('1 unchanged node')
    expect(unchanged.find('[data-testid="pipeline-compare-unchanged-list"]').text()).toContain('Steady')
  })

  it('keeps the raw JSON diff behind a toggle that is off by default', async () => {
    const wrapper = await flushCompare()
    const toggle = wrapper.find('[data-testid="pipeline-compare-raw-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)

    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    const raw = wrapper.find('[data-testid="pipeline-compare-raw"]')
    expect(raw.exists()).toBe(true)
    expect(raw.text()).toContain('nodes_added')

    // Hiding it again returns to the semantic-led presentation.
    await toggle.trigger('click')
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)
  })

  it('points aria-controls at a raw region that exists while collapsed', async () => {
    const wrapper = await flushCompare()
    const toggle = wrapper.find('[data-testid="pipeline-compare-raw-toggle"]')
    const rawRegionId = toggle.attributes('aria-controls')
    expect(rawRegionId).toBeTruthy()
    // The referenced region is in the DOM even though the payload is hidden.
    expect(wrapper.find(`#${rawRegionId}`).exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)
  })

  it('reports a clean diff without pretending there are breaking changes', async () => {
    apiPost.mockResolvedValue({
      data: { nodes_added: [], nodes_removed: [], nodes_modified: [], edges_added: [], edges_removed: [], edges_modified: [], semantic: { impacted_nodes: [], breaking_changes: [], port_changes: [] } },
      error: undefined,
    })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-breaking-none"]').text()).toContain('No breaking port changes')
    expect(wrapper.find('[data-testid="pipeline-compare-structural-none"]').text()).toContain('No structural changes')
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').text()).toContain('no downstream nodes are impacted')
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)
  })

  it('renders a diff with an absent semantic layer without inventing breaking changes', async () => {
    apiPost.mockResolvedValue({
      data: { snapshot_a: DIFF.snapshot_a, snapshot_b: DIFF.snapshot_b, nodes_added: [], nodes_removed: [], nodes_modified: [], edges_added: [], edges_removed: [], edges_modified: [] },
      error: undefined,
    })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-breaking-none"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').text()).toContain('no downstream nodes are impacted')
  })

  it('surfaces a backend error cleanly (never a crash) and retries on demand', async () => {
    apiPost.mockResolvedValueOnce({
      data: undefined,
      error: { type: 'urn:problem:modulo:internal_error', title: 'Internal Error', status: 500, detail: 'node is missing an id' },
    })
    const wrapper = await flushCompare()
    const error = wrapper.find('[data-testid="pipeline-compare-error"]')
    expect(error.exists()).toBe(true)
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toContain('node is missing an id')
    // VIS-3: the region is the shared ErrorAlert (compact), not a bespoke box.
    const alert = wrapper.findComponent(ErrorAlert)
    expect(alert.exists()).toBe(true)
    expect(alert.props('compact')).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(false)

    apiPost.mockResolvedValueOnce({ data: DIFF, error: undefined })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="pipeline-compare-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(true)
  })

  it.each([
    ['snap-gone', 404, 'snapshot no longer exists'],
    ['not-a-uuid', 422, 'snapshot_id is not a valid uuid'],
  ])('renders a clean error state for a %s diff (%i)', async (id, status, detail) => {
    apiPost.mockResolvedValue({
      data: undefined,
      error: { type: 'urn:problem:modulo:error', title: 'Error', status, detail },
    })
    const wrapper = await flushCompare({ baseSnapshotId: id, compareSnapshotId: 'snap-9' })
    await flushPromises()
    const error = wrapper.find('[data-testid="pipeline-compare-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain(detail)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(false)
  })

  it('reports a self-compare truthfully instead of an all-zero diff', async () => {
    const wrapper = await flushCompare({ baseSnapshotId: 'snap-9', compareSnapshotId: 'snap-9' })
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="pipeline-compare-self"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(false)
  })

  it('ignores a superseded diff response so a stale pair cannot overwrite a newer one', async () => {
    const resolvers: Array<(value: unknown) => void> = []
    apiPost.mockImplementation(
      () => new Promise((resolve) => { resolvers.push(resolve as (value: unknown) => void) }),
    )
    const wrapper = mountCompare()
    await flushPromises()
    expect(resolvers).toHaveLength(1)

    // Flip the pair -> a second request supersedes the first while it is still
    // in flight.
    await wrapper.setProps({ baseSnapshotId: 'snap-9', compareSnapshotId: 'snap-4' })
    await flushPromises()
    expect(resolvers).toHaveLength(2)

    const fresh = { ...DIFF, semantic: { ...DIFF.semantic, breaking_changes: [{ severity: 'block', reason: 'fresh-reason' }] } }
    const stale = { ...DIFF, semantic: { ...DIFF.semantic, breaking_changes: [{ severity: 'block', reason: 'stale-reason' }] } }

    // The NEWER request resolves first, then the STALE one — which must not win.
    resolvers[1]({ data: fresh, error: undefined })
    await flushPromises()
    expect(wrapper.find('[data-testid="pipeline-compare-semantic"]').text()).toContain('fresh-reason')

    resolvers[0]({ data: stale, error: undefined })
    await flushPromises()
    const semantic = wrapper.find('[data-testid="pipeline-compare-semantic"]')
    expect(semantic.text()).toContain('fresh-reason')
    expect(semantic.text()).not.toContain('stale-reason')
  })

  it('times a stalled diff out into the error state with a Retry', async () => {
    // Only the deadline timer is faked — setImmediate stays real so
    // flushPromises() still resolves.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      apiPost.mockImplementation(() => new Promise(() => {}))
      const wrapper = mountCompare()
      await vi.advanceTimersByTimeAsync(15000)
      await flushPromises()
      const error = wrapper.find('[data-testid="pipeline-compare-error"]')
      expect(error.exists()).toBe(true)
      expect(error.text()).toContain('too long')
      expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('counts a port-signature change only on nodes present in BOTH versions', async () => {
    apiPost.mockResolvedValue({
      data: {
        ...DIFF,
        semantic: {
          port_changes: [
            // A genuine signature change on a node in both versions.
            { kind: 'node_signature', node_id: 'n1', direction: 'output', port: 'out', change: 'removed' },
            // An edge's port binding names a node present in BOTH graphs, so
            // ONLY the `kind` discriminator excludes it — this test would fail
            // if the kind filter were deleted. (n2 would have passed for the
            // wrong reason: it exists only in snapshot_a, so the both-graphs
            // check alone would already exclude it.)
            { kind: 'edge_port', node_id: 'n3', direction: 'output', port: 'out', change: 'edge_source' },
            // A node present only in the newer version has no signature delta.
            { kind: 'node_signature', node_id: 'n4', direction: 'input', port: 'in', change: 'added' },
          ],
          impacted_nodes: [],
          breaking_changes: [],
        },
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').text()).toContain('port signature changed on 1 node')
  })

  it('surfaces an edge port-only change rather than a bare A → B', async () => {
    apiPost.mockResolvedValue({
      data: {
        ...DIFF,
        edges_modified: [
          { edge: { source: 'n1', target: 'n3' }, changes: { ports: { source_port: { old: 'out-a', new: 'out-b' } } } },
        ],
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    const modified = wrapper.find('[data-testid="pipeline-compare-edges-modified"]')
    expect(modified.exists()).toBe(true)
    expect(modified.text()).toContain('Source port: out-a → out-b')
    expect(modified.text()).toContain('Start → Steady')
  })

  it('offers versions beyond the host catalog page by paging the history itself', async () => {
    apiGet.mockImplementation((url: string, options?: { params?: { query?: { page?: number } } }) => {
      if (url.endsWith('/snapshots')) {
        const page = options?.params?.query?.page ?? 1
        if (page === 1) return Promise.resolve({ data: { items: SNAPSHOTS, total: 3 }, error: undefined })
        return Promise.resolve({
          data: { items: [{ id: 'snap-1', snapshot_version: 1, tag: null, created_at: null }], total: 3 },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [], total: 0 }, error: undefined })
    })
    const wrapper = await flushCompare()
    await flushPromises()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((option) => option.text())
    expect(labels.some((label) => label.includes('v1'))).toBe(true)
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
  })

  it('labels a deep-linked version the loaded history does not carry', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/snapshots/{snapshot_id}')) {
        // Belongs to another pipeline / no longer exists -> the diff errors,
        // but the picker still needs a meaningful (non-blank) label.
        return Promise.resolve({
          data: undefined,
          error: { type: 'urn:problem:modulo:not_found', title: 'Not Found', status: 404, detail: 'no such snapshot' },
        })
      }
      return Promise.resolve({ data: { items: SNAPSHOTS, total: 2 }, error: undefined })
    })
    const wrapper = await flushCompare({ baseSnapshotId: 'snap-missing', compareSnapshotId: 'snap-9' })
    await flushPromises()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((option) => option.text())
    expect(labels.some((label) => label.includes('#snap-mis'))).toBe(true)
  })

  it('re-fetches a snapshot whose targeted lookup failed transiently, so a retry yields the real label', async () => {
    let snapshotLookupAttempts = 0
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/snapshots/{snapshot_id}')) {
        snapshotLookupAttempts += 1
        if (snapshotLookupAttempts === 1) {
          // Transient failure: the id must NOT be marked as fetched.
          return Promise.resolve({
            data: undefined,
            error: { type: 'urn:problem:modulo:internal_error', title: 'Internal Error', status: 500, detail: 'transient' },
          })
        }
        return Promise.resolve({
          data: { id: 'snap-deep', snapshot_version: 7, tag: null, created_at: null },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: SNAPSHOTS, total: SNAPSHOTS.length }, error: undefined })
    })
    // The diff fails on first attempt too, so the Retry control is reachable.
    apiPost.mockResolvedValueOnce({
      data: undefined,
      error: { type: 'urn:problem:modulo:internal_error', title: 'Internal Error', status: 500, detail: 'transient' },
    })
    const wrapper = await flushCompare({ baseSnapshotId: 'snap-deep', compareSnapshotId: 'snap-9' })
    await flushPromises()

    // First (failed) lookup leaves the deep link on the fallback label.
    const labelsBefore = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labelsBefore.some((label) => label.includes('Version #snap-dee'))).toBe(true)
    expect(labelsBefore.some((label) => label.includes('v7'))).toBe(false)

    // Retry: the unmarked id is looked up again and now resolves to the real
    // version label instead of staying stranded on the fallback.
    apiPost.mockResolvedValueOnce({ data: DIFF, error: undefined })
    await wrapper.find('[data-testid="error-alert-retry"]').trigger('click')
    await flushPromises()

    expect(snapshotLookupAttempts).toBe(2)
    const labelsAfter = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labelsAfter.some((label) => label.includes('v7'))).toBe(true)
    expect(labelsAfter.some((label) => label.includes('Version #snap-dee'))).toBe(false)
  })

  it('handles an empty snapshot list without crashing', async () => {
    const wrapper = await flushCompare({ snapshots: [] })
    expect(wrapper.find('[data-testid="pipeline-compare"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(true)
  })

  it('handles a single-entry snapshot list', async () => {
    const wrapper = await flushCompare({ snapshots: [SNAPSHOTS[0]], baseSnapshotId: 'snap-9', compareSnapshotId: 'snap-9' })
    expect(wrapper.find('[data-testid="pipeline-compare-self"]').exists()).toBe(true)
  })

  it('emits the chosen pair when either picker changes', async () => {
    const wrapper = await flushCompare()
    // SNAPSHOTS[0] = snap-9, SNAPSHOTS[1] = snap-4.
    await wrapper.find('[data-testid="pipeline-compare-base"]').setValue('0') // snap-9
    expect(wrapper.emitted('change')).toEqual([[{ base: 'snap-9', compare: 'snap-9' }]])

    await wrapper.find('[data-testid="pipeline-compare-against"]').setValue('1') // snap-4
    expect(wrapper.emitted('change')).toEqual([
      [{ base: 'snap-9', compare: 'snap-9' }],
      [{ base: 'snap-4', compare: 'snap-4' }],
    ])
  })

  it('emits close from the close control, which carries an aria-label', async () => {
    const wrapper = await flushCompare()
    const close = wrapper.find('[data-testid="pipeline-compare-close"]')
    expect(close.attributes('aria-label')).toBeTruthy()
    await close.trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})

// Sparse/edge-shaped payloads. Every optional collection in the diff response
// is genuinely optional on the wire, so the presentation must render every
// shape without a warning or a blank pane — not just the fully-populated one.
describe('PipelineSnapshotCompare — sparse and edge-shaped diffs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiPost.mockResolvedValue({ data: DIFF, error: undefined })
    apiGet.mockResolvedValue({ data: { items: [], total: 0 }, error: undefined })
  })

  it('renders a diff that omits every optional collection', async () => {
    apiPost.mockResolvedValue({
      data: { snapshot_a: { graph: { nodes: [] } }, snapshot_b: { graph: { nodes: [] } } },
      error: undefined,
    })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').text()).toContain('0 nodes added')
    expect(wrapper.find('[data-testid="pipeline-compare-structural-none"]').exists()).toBe(true)
  })

  it('renders a diff with no frozen graphs at all', async () => {
    apiPost.mockResolvedValue({ data: {}, error: undefined })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-impacted"]').text()).toContain('None')
  })

  it('renders removed edges and a modified entry that carries no node id', async () => {
    apiPost.mockResolvedValue({
      data: {
        snapshot_a: { graph: { nodes: [{ id: 'n1', label: 'Start' }, { id: 'n2', label: 'Steady' }] } },
        snapshot_b: { graph: { nodes: [{ id: 'n1', label: 'Start' }, { id: 'n2', label: 'Steady' }, { id: 'n9' }] } },
        nodes_modified: [{ changes: { label: { old: 'Steady', new: 'Steady 2' } } }],
        edges_removed: [{ source: 'n1', target: 'n2' }],
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-edges-removed"]').text()).toContain('− Start → Steady')
    // No node id: the label falls back truthfully instead of an "undefined".
    const modified = wrapper.find('[data-testid="pipeline-compare-node-modified"]')
    expect(modified.text()).toContain('—')
    expect(modified.text()).toContain('Label: Steady → Steady 2')
  })

  it('labels nodes and edges that omit ids, labels or types', async () => {
    apiPost.mockResolvedValue({
      data: {
        snapshot_a: { graph: {} },
        snapshot_b: { graph: { nodes: [{ id: 'sparse' }, {}] } },
        nodes_added: [{}, { id: 'known', label: 'Known' }, { id: 'unlabelled' }],
        edges_added: [{}, { source_node_id: 'known', target_node_id: 'ghost' }],
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    const added = wrapper.find('[data-testid="pipeline-compare-nodes-added"]')
    expect(added.text()).toContain('Known')
    expect(added.text()).toContain('#unlabell')
    // An id-less edge renders the no-value glyph for each missing endpoint.
    const edges = wrapper.find('[data-testid="pipeline-compare-edges-added"]')
    expect(edges.text()).toContain('— → —')
    expect(edges.text()).toContain('#known → #ghost')
  })

  it('renders every field-value shape without leaking raw JSON', async () => {
    const circular: Record<string, unknown> = {}
    circular.self = circular
    apiPost.mockResolvedValue({
      data: {
        snapshot_a: { graph: { nodes: [{ id: 'n1', label: 'Start' }] } },
        snapshot_b: { graph: { nodes: [{ id: 'n1', label: 'Start' }] } },
        nodes_modified: [
          {
            node_id: 'n1',
            changes: {
              label: { old: 42, new: true },
              bare: 'added-value',
              nullish: null,
              arr: [1, 2],
              obj: { unrelated: 1 },
              empty: '',
              onlyNew: { new: 'fresh' },
              nested: { old: { a: 1 }, new: { b: 2 } },
              cyclic: { old: circular, new: 'x' },
              ports: [
                { direction: 'input', port: null, change: 'added' },
                { direction: 'output', change: 'modified' },
                { change: 'added' },
                { direction: 'sideways', port: 'p', change: 'weird' },
                { direction: 'sideways', port: 'p' },
              ],
            },
          },
          // No changes at all: the field reader must tolerate an absent map.
          { node_id: 'n2' },
        ],
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    const modified = wrapper.find('[data-testid="pipeline-compare-node-modified"]')
    expect(modified.text()).toContain('Label: 42 → true')
    expect(modified.text()).toContain('bare: added-value → —')
    expect(modified.text()).toContain('nullish: — → —')
    expect(modified.text()).toContain('arr: [1,2] → —')
    expect(modified.text()).toContain('obj: {"unrelated":1} → —')
    expect(modified.text()).toContain('empty: — → —')
    expect(modified.text()).toContain('onlyNew: — → fresh')
    expect(modified.text()).toContain('nested: {"a":1} → {"b":2}')
    // A value that cannot be serialised is rendered with String(), never thrown.
    expect(modified.text()).toContain('[object Object]')
    // Port deltas: input/added, output/modified, an absent direction, and an
    // unknown change kind each get a truthful label.
    expect(modified.text()).toContain("input port '' added")
    expect(modified.text()).toContain("output port '' schema changed")
    expect(modified.text()).toContain(" port '' added")
    expect(modified.text()).toContain("sideways port 'p' (weird)")
    expect(modified.text()).toContain("sideways port 'p' ()")
  })

  it('renders modified edges with missing or malformed port deltas', async () => {
    apiPost.mockResolvedValue({
      data: {
        ...DIFF,
        edges_modified: [
          { changes: { source_port: { old: 'a', new: 'b' } } },
          { edge: { source: 'n1', target: 'n3' }, changes: { ports: [] } },
          { edge: { source: 'n1', target: 'n3' }, changes: { ports: { source_port: 'bare' } } },
          { edge: { source: 'n1', target: 'n3' }, changes: { ports: { target_port: { old: 'x', new: 'y' }, added_port: { new: 'z' } } } },
        ],
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    const modified = wrapper.find('[data-testid="pipeline-compare-edges-modified"]')
    // No `edge` payload: the endpoints fall back to the no-value glyphs.
    expect(modified.text()).toContain('— → — (Source port: a → b)')
    // An array or a bare value in `ports` is not a port delta dict -> bare ends.
    expect(modified.text()).toContain('Start → Steady')
    expect(modified.text()).toContain('Target port: x → y')
  })

  it('labels breaking-change severities and unknown impacted nodes', async () => {
    apiPost.mockResolvedValue({
      data: {
        ...DIFF,
        semantic: {
          breaking_changes: [
            { severity: 'warning', reason: 'warn-reason' },
            { severity: 'info', reason: 'info-reason' },
            { reason: 'plain-reason' },
          ],
          impacted_nodes: ['ghost', 'n1'],
          port_changes: [],
        },
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    const breaking = wrapper.find('[data-testid="pipeline-compare-breaking"]')
    expect(breaking.text()).toContain('warning — warn-reason')
    expect(breaking.text()).toContain('info — info-reason')
    expect(breaking.text()).toContain('warning — plain-reason')
    expect(wrapper.find('[data-testid="pipeline-compare-impacted"]').text()).toContain('#ghost')
  })

  it('treats a 200 with no body as an error state rather than a blank diff', async () => {
    apiPost.mockResolvedValue({ data: undefined, error: undefined })
    const wrapper = await flushCompare()
    const error = wrapper.find('[data-testid="pipeline-compare-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('no comparison data')
  })

  it('surfaces a non-timeout rejection through the API error formatter', async () => {
    apiPost.mockRejectedValueOnce(new Error('network exploded'))
    const wrapper = await flushCompare()
    const error = wrapper.find('[data-testid="pipeline-compare-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('network exploded')
  })

  it('drops a superseded request that rejects after a newer pair was requested', async () => {
    const pending: Array<{ resolve: (value: unknown) => void; reject: (reason: unknown) => void }> = []
    apiPost.mockImplementation(
      () => new Promise((resolve, reject) => { pending.push({ resolve, reject }) }),
    )
    const wrapper = mountCompare()
    await flushPromises()
    expect(pending).toHaveLength(1)

    await wrapper.setProps({ baseSnapshotId: 'snap-9', compareSnapshotId: 'snap-4' })
    await flushPromises()
    expect(pending).toHaveLength(2)

    pending[0].reject(new Error('stale boom'))
    await flushPromises()
    // The stale rejection must not surface: the newer request is still in flight.
    expect(wrapper.find('[data-testid="pipeline-compare-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="pipeline-compare-error"]').exists()).toBe(false)

    pending[1].resolve({ data: DIFF, error: undefined })
    await flushPromises()
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(true)
  })

  it('ignores a picker update that is not a non-empty string', async () => {
    const wrapper = await flushCompare()
    const [base, against] = wrapper.findAllComponents(SelectStub)
    base.vm.$emit('update:modelValue', 42)
    base.vm.$emit('update:modelValue', '')
    against.vm.$emit('update:modelValue', { id: 'snap-1' })
    against.vm.$emit('update:modelValue', '')
    expect(wrapper.emitted('change')).toBeUndefined()
  })

  it('leaves the history picker intact when the catalog request errors', async () => {
    apiGet.mockResolvedValue({
      data: undefined,
      error: { type: 'urn:problem:modulo:internal_error', title: 'Internal Error', status: 500, detail: 'catalog down' },
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
  })

  it('leaves the history picker intact when the catalog returns no body', async () => {
    apiGet.mockResolvedValue({ data: undefined, error: undefined })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
  })

  it('accepts a catalog page without an items array or with invalid rows', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) {
        return Promise.resolve({
          data: {
            total: 4,
            items: [
              { id: null, snapshot_version: 2 },
              { id: 'zero', snapshot_version: 0 },
              { id: 'ok', snapshot_version: 2, tag: null, created_at: null },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v2'))).toBe(true)
    expect(labels.some((label) => label.includes('zero'))).toBe(false)
  })

  it('stops paging when the server reports no total and returns a short page', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) {
        return Promise.resolve({ data: { items: SNAPSHOTS }, error: undefined })
      }
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
    expect(labels.some((label) => label.includes('v1'))).toBe(false)
  })

  it('pages past a full catalog page when the server reports no total', async () => {
    const fullPage = Array.from({ length: 100 }, (_, i) => ({
      id: `bulk-${i}`,
      snapshot_version: 200 - i,
      tag: null,
      created_at: null,
    }))
    apiGet.mockImplementation((url: string, options?: { params?: { query?: { page?: number } } }) => {
      if (url.endsWith('/snapshots')) {
        const page = options?.params?.query?.page ?? 1
        if (page === 1) return Promise.resolve({ data: { items: fullPage }, error: undefined })
        return Promise.resolve({
          data: { items: [{ id: 'bulk-last', snapshot_version: 1, tag: null, created_at: null }] },
          error: undefined,
        })
      }
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v1'))).toBe(true)
  })

  it('cancels an in-flight catalog load on unmount', async () => {
    let resolveCatalog: ((value: unknown) => void) | undefined
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) {
        return new Promise((resolve) => { resolveCatalog = resolve })
      }
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = mountCompare()
    await flushPromises()
    wrapper.unmount()
    expect(resolveCatalog).toBeDefined()
    resolveCatalog!({ data: { items: SNAPSHOTS, total: 2 }, error: undefined })
    await flushPromises()
  })

  it('aborts the in-flight diff on unmount', async () => {
    const pending: Array<{ resolve: (value: unknown) => void }> = []
    let capturedSignal: AbortSignal | undefined
    apiPost.mockImplementation((_url: string, options?: { signal?: AbortSignal }) => {
      capturedSignal = options?.signal
      return new Promise((resolve) => { pending.push({ resolve }) })
    })
    const wrapper = mountCompare()
    await flushPromises()
    expect(capturedSignal?.aborted).toBe(false)
    wrapper.unmount()
    expect(capturedSignal?.aborted).toBe(true)
    pending[0].resolve({ data: DIFF, error: undefined })
    await flushPromises()
  })

  it('resolves shared nodes when the newer frozen graph is absent', async () => {
    apiPost.mockResolvedValue({
      data: {
        snapshot_a: { graph: { nodes: [{ id: 'n1' }] } },
        nodes_added: [{ id: 'n2', label: 'New' }],
        semantic: {
          port_changes: [{ node_id: 'n1', direction: 'output', port: 'out', change: 'removed' }],
          impacted_nodes: [],
          breaking_changes: [],
        },
      },
      error: undefined,
    })
    const wrapper = await flushCompare()
    // port_changes names n1, but only a graph present in BOTH versions can
    // prove a signature change — a missing B graph proves nothing.
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').text()).not.toContain('port signature')
    expect(wrapper.find('[data-testid="pipeline-compare-structural"]').text()).toContain('New')
  })

  it('accepts a catalog page that omits the items array', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) return Promise.resolve({ data: { total: 2 }, error: undefined })
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
  })

  it('keeps the picker usable when the catalog request rejects', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) return Promise.reject(new Error('catalog down'))
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = await flushCompare()
    const labels = wrapper.find('[data-testid="pipeline-compare-base"]').findAll('option').map((o) => o.text())
    expect(labels.some((label) => label.includes('v9'))).toBe(true)
    expect(warn.mock.calls.some((args) => String(args[0]).includes('Failed to load the full snapshot history'))).toBe(true)
    warn.mockRestore()
  })

  it('ignores a rejecting catalog request that was superseded by unmount', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const pending: Array<{ reject: (reason: unknown) => void }> = []
    apiGet.mockImplementation((url: string) => {
      if (url.endsWith('/snapshots')) return new Promise((_resolve, reject) => { pending.push({ reject }) })
      return Promise.resolve({ data: undefined, error: undefined })
    })
    const wrapper = mountCompare()
    await flushPromises()
    wrapper.unmount()
    pending[0].reject(new Error('late catalog failure'))
    await flushPromises()
    expect(warn.mock.calls.some((args) => String(args[0]).includes('Failed to load the full snapshot history'))).toBe(false)
    warn.mockRestore()
  })
})
