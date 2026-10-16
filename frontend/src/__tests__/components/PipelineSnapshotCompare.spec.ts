import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

// FAR-1818 — semantic-led version compare presentation.
//
// Proves the three contract points the ticket calls out: (1) the semantic
// layer (breaking changes + impacted downstream nodes) is presented FIRST;
// (2) structural changes are grouped per node and unchanged nodes collapse;
// (3) the raw JSON diff is LAST and hidden behind a toggle that is OFF by
// default. It also pins the empty/error states and the "compare defaults to
// latest" rule.

const { apiPost } = vi.hoisted(() => ({ apiPost: vi.fn() }))

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
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
    port_changes: [{ node_id: 'n1', direction: 'output', port: 'out', change: 'removed', old: 'x', new: null }],
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
  })

  it('fetches the diff for the given pair and posts the real snapshot ids', async () => {
    await flushCompare()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
      params: { path: { pipeline_id: 'pipe-1' } },
      body: { snapshot_a_id: 'snap-4', snapshot_b_id: 'snap-9' },
    })
  })

  it('shows a loading status region while the diff is in flight', () => {
    const wrapper = mountCompare()
    const loading = wrapper.find('[data-testid="pipeline-compare-loading"]')
    expect(loading.exists()).toBe(true)
    expect(loading.attributes('role')).toBe('status')
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

  it('summarises the change as a narrative, not a JSON wall', async () => {
    const wrapper = await flushCompare()
    const summary = wrapper.find('[data-testid="pipeline-compare-summary"]')
    expect(summary.text()).toContain('1 nodes added, 1 removed, 1 modified')
    expect(summary.text()).toContain('1 edges added, 0 removed, 0 modified')
    expect(summary.text()).toContain('port signature changed on 1 nodes')
    expect(summary.text()).toContain('these 2 downstream nodes are impacted')
    // The raw JSON never leaks into the default presentation.
    expect(wrapper.find('[data-testid="pipeline-compare-raw"]').exists()).toBe(false)
    expect(summary.text()).not.toContain('nodes_added')
  })

  it('groups structural changes per node and collapses unchanged nodes', async () => {
    const wrapper = await flushCompare()
    expect(wrapper.find('[data-testid="pipeline-compare-nodes-added"]').text()).toContain('New Node')
    expect(wrapper.find('[data-testid="pipeline-compare-nodes-removed"]').text()).toContain('Old Node')
    const modified = wrapper.find('[data-testid="pipeline-compare-node-modified"]')
    expect(modified.text()).toContain('Start')
    expect(modified.text()).toContain('Label: Start → Begin')
    // The removed port renders as a port change, not raw JSON.
    expect(modified.text()).toContain("output port 'out' removed")
    // n3 is in both versions and unchanged -> collapsed count of 1.
    expect(wrapper.find('[data-testid="pipeline-compare-unchanged"]').text()).toContain('1 unchanged nodes')
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
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(false)

    apiPost.mockResolvedValueOnce({ data: DIFF, error: undefined })
    await wrapper.find('[data-testid="pipeline-compare-retry"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="pipeline-compare-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="pipeline-compare-summary"]').exists()).toBe(true)
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
