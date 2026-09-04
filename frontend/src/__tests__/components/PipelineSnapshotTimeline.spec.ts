import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const { apiGet, apiPost } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
}))

vi.mock('../../lib/api/client', () => ({
  getAccessToken: vi.fn(() => 'mock-token'),
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer mock-token' })),
  api: {
    GET: apiGet,
    POST: apiPost,
    PATCH: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
}))

// Faithful PrimeVue Select stub: without option-value the real component
// emits the WHOLE option object as the model value (see FAR-578 report).
const SelectStub = {
  name: 'SelectStub',
  props: ['modelValue', 'options', 'placeholder', 'optionLabel', 'optionValue', 'dataTestid'],
  emits: ['update:modelValue'],
  template: `
    <select data-testid="mock-select" @change="$emit('update:modelValue', options[Number($event.target.value)])">
      <option v-for="(o, i) in options" :key="i" :value="i">{{ o.label ?? o }}</option>
    </select>`,
}

import PipelineSnapshotTimeline from '../../components/pipeline/PipelineSnapshotTimeline.vue'

interface TimelineSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
  version_kind: string
  channel: string
  draft: boolean
}

function makeSnapshot(overrides: Partial<TimelineSnapshot> = {}): TimelineSnapshot {
  return {
    id: 'snap-1',
    snapshot_version: 3,
    tag: 'release-b',
    created_at: '2026-08-01T12:00:00Z',
    version_kind: 'edit',
    channel: 'none',
    draft: false,
    ...overrides,
  }
}

function mountTimeline(items: TimelineSnapshot[] | null, rejects = false) {
  if (rejects) {
    apiGet.mockRejectedValue(new Error('network down'))
  } else {
    apiGet.mockResolvedValue({ data: { items }, error: undefined })
  }
  return mount(PipelineSnapshotTimeline, {
    props: { pipelineId: 'pipe-1' },
    global: { stubs: { Select: SelectStub } },
  })
}

async function flushMount(items: TimelineSnapshot[] | null, rejects = false) {
  const wrapper = mountTimeline(items, rejects)
  await flushPromises()
  return wrapper
}

function rowFor(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="snapshot-timeline-row-${id}"]`)
}

const EM_DASH = '\u2014'

describe('PipelineSnapshotTimeline', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiPost.mockResolvedValue({ data: {}, error: undefined })
  })

  it('shows the loading spinner before snapshots arrive', () => {
    const wrapper = mountTimeline([makeSnapshot()])
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('v3')
  })

  it('shows the empty state when the pipeline has no snapshots', async () => {
    const wrapper = await flushMount([])
    expect(wrapper.text()).toContain('No snapshots yet')
  })

  it('renders snapshot rows with version, kind badge, channel and tag', async () => {
    const wrapper = await flushMount([
      makeSnapshot(),
      makeSnapshot({ id: 'snap-2', snapshot_version: 2, version_kind: 'run', channel: 'stable', tag: null }),
    ])
    const row1 = rowFor(wrapper, 'snap-1')
    expect(row1.text()).toContain('v3')
    expect(row1.text()).toContain('edit')
    expect(row1.text()).toContain('release-b')
    const row2 = rowFor(wrapper, 'snap-2')
    expect(row2.text()).toContain('v2')
    expect(row2.text()).toContain('run')
    expect(row2.text()).toContain('stable')
    expect(row2.text()).not.toContain('release-b')
  })

  it('shows a dash for snapshots without a date', async () => {
    const wrapper = await flushMount([makeSnapshot({ created_at: null })])
    expect(rowFor(wrapper, 'snap-1').text()).toMatch(/[-\u2014]/)
  })

  it('rolls back to the selected snapshot and reloads', async () => {
    const wrapper = await flushMount([makeSnapshot(), makeSnapshot({ id: 'snap-2', snapshot_version: 2, version_kind: 'run', tag: null })])
    expect(rowFor(wrapper, 'snap-1').classes()).not.toContain('bg-primary')
    await rowFor(wrapper, 'snap-1').trigger('click')
    const rollbackBtn = wrapper.find('[data-testid="snapshot-timeline-rollback"]')
    expect(rollbackBtn.attributes('disabled')).toBeUndefined()
    await rollbackBtn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/snapshots/{snapshot_id}/rollback', {
      params: { path: { pipeline_id: 'pipe-1', snapshot_id: 'snap-1' } },
    })
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('disables rollback until a snapshot row is selected', async () => {
    const wrapper = await flushMount([makeSnapshot()])
    expect(wrapper.find('[data-testid="snapshot-timeline-rollback"]').attributes('disabled')).toBeDefined()
  })

  it('shows an action error when rollback fails', async () => {
    apiPost.mockResolvedValue({ data: null, error: { detail: 'rollback forbidden' } })
    const wrapper = await flushMount([makeSnapshot()])
    await rowFor(wrapper, 'snap-1').trigger('click')
    await wrapper.find('[data-testid="snapshot-timeline-rollback"]').trigger('click')
    await flushPromises()
    // BUG NOTE (FAR-578): the component renders String(error), so a structured
    // ProblemDetail error shows as "[object Object]" instead of the detail.
    expect(wrapper.text()).toContain('[object Object]')
  })

  it('diffs the selected snapshot against the compare base', async () => {
    apiPost.mockResolvedValue({
      data: {
        semantic: {
          impacted_nodes: ['node-aaaaaaaa-1', 'node-bbbbbbbb-2'],
          breaking_changes: [{ severity: 'block', reason: 'Input port removed' }],
        },
      },
      error: undefined,
    })
    const wrapper = await flushMount([makeSnapshot(), makeSnapshot({ id: 'snap-2', snapshot_version: 2, version_kind: 'run', tag: null })])
    await rowFor(wrapper, 'snap-1').trigger('click')
    const diffBtn = wrapper.find('[data-testid="snapshot-timeline-diff"]')
    expect(diffBtn.attributes('disabled')).toBeDefined()
    // Selecting the second option in the compare dropdown (snap-2).
    await wrapper.find('[data-testid="mock-select"]').setValue('1')
    expect(diffBtn.attributes('disabled')).toBeUndefined()
    await diffBtn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/snapshots/diff', {
      params: { path: { pipeline_id: 'pipe-1' } },
      // BUG NOTE (FAR-578): compareB holds the whole {value,label} option
      // object because the PrimeVue Select has no option-value prop, so the
      // diff request posts an object where the backend expects a snapshot id.
      // This assertion documents the current behaviour and must be updated
      // (to { snapshot_b_id: 'snap-2' }) together with the component fix.
      body: {
        snapshot_a_id: 'snap-1',
        snapshot_b_id: { value: 'snap-2', label: `v2 ${EM_DASH} run` },
      },
    })
    const result = wrapper.find('[data-testid="snapshot-timeline-diff-result"]')
    expect(result.exists()).toBe(true)
    // impactedLabel truncates each node id to 8 characters.
    expect(result.text()).toContain('node-aaa, node-bbb')
    expect(result.text()).toContain('Input port removed')
  })

  it('lists no impacted nodes and no breaking changes for a clean diff', async () => {
    apiPost.mockResolvedValue({ data: { semantic: { impacted_nodes: [], breaking_changes: [] } }, error: undefined })
    const wrapper = await flushMount([makeSnapshot(), makeSnapshot({ id: 'snap-2', snapshot_version: 2, version_kind: 'run', tag: null })])
    await rowFor(wrapper, 'snap-1').trigger('click')
    await wrapper.find('[data-testid="mock-select"]').setValue('1')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    const result = wrapper.find('[data-testid="snapshot-timeline-diff-result"]')
    expect(result.text()).toContain('none')
    expect(result.text()).toContain('No breaking port changes')
  })

  it('shows an action error when the diff request fails', async () => {
    apiPost.mockResolvedValue({ data: null, error: { detail: 'diff unavailable' } })
    const wrapper = await flushMount([makeSnapshot(), makeSnapshot({ id: 'snap-2', snapshot_version: 2, version_kind: 'run', tag: null })])
    await rowFor(wrapper, 'snap-1').trigger('click')
    await wrapper.find('[data-testid="mock-select"]').setValue('1')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    // BUG NOTE (FAR-578): String(error) on a structured error renders as
    // "[object Object]" - see the rollback-failure test.
    expect(wrapper.text()).toContain('[object Object]')
  })

  it('survives a failed snapshot load by showing the empty state', async () => {
    const wrapper = await flushMount(null, true)
    expect(wrapper.text()).toContain('No snapshots yet')
  })

  it('emits close from the close button', async () => {
    const wrapper = await flushMount([makeSnapshot()])
    await wrapper.find('[data-testid="snapshot-timeline-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
