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

// Faithful PrimeVue Select stub: with option-value/option-label the real
// component emits the option's value field; without them it emits the whole
// option object (FAR-606).
const SelectStub = {
  name: 'SelectStub',
  props: ['modelValue', 'options', 'placeholder', 'optionLabel', 'optionValue', 'dataTestid'],
  emits: ['update:modelValue'],
  template: `
    <select data-testid="mock-select" @change="$emit('update:modelValue', optionValue ? options[Number($event.target.value)]?.[optionValue] : options[Number($event.target.value)])">
      <option v-for="(o, i) in options" :key="i" :value="i">{{ optionLabel ? o[optionLabel] : (o.label ?? o) }}</option>
    </select>`,
}

import PipelineSnapshotTimeline from '../../components/pipeline/PipelineSnapshotTimeline.vue'

interface TimelineSnapshot {
  id: string
  snapshot_version: number
  tag: string | null
  created_at: string | null
  version_kind: string
  created_kind: string
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
    created_kind: 'edit',
    channel: 'none',
    draft: false,
    ...overrides,
  }
}

// A modern pipeline: an edit with no runs, an initial anchor and an edit, each
// with run snapshots frozen at run-start. Version-DESC on the wire.
function modernHistory(): TimelineSnapshot[] {
  return [
    makeSnapshot({ id: 'snap-6', snapshot_version: 6, created_kind: 'edit', version_kind: 'edit', tag: null }),
    makeSnapshot({ id: 'snap-5', snapshot_version: 5, created_kind: 'run', version_kind: 'run', tag: null, channel: 'stable' }),
    makeSnapshot({ id: 'snap-4', snapshot_version: 4, created_kind: 'edit', version_kind: 'edit' }),
    makeSnapshot({ id: 'snap-3', snapshot_version: 3, created_kind: 'run', version_kind: 'run', tag: null }),
    makeSnapshot({ id: 'snap-2', snapshot_version: 2, created_kind: 'run', version_kind: 'run', tag: null }),
    makeSnapshot({ id: 'snap-1', snapshot_version: 1, created_kind: 'initial', version_kind: 'edit', tag: null }),
  ]
}

// Two substantive versions (the minimum needed to diff); no runs.
function twoSubstantive(): TimelineSnapshot[] {
  return [
    makeSnapshot({ id: 'snap-2', snapshot_version: 2, created_kind: 'edit' }),
    makeSnapshot({ id: 'snap-1', snapshot_version: 1, created_kind: 'edit', tag: null }),
  ]
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

function selectFor(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="snapshot-timeline-select-${id}"]`)
}

function toggleFor(wrapper: ReturnType<typeof mount>, id: string) {
  return wrapper.find(`[data-testid="snapshot-timeline-runs-toggle-${id}"]`)
}

async function selectVersion(wrapper: ReturnType<typeof mount>, id: string) {
  await selectFor(wrapper, id).setValue(true)
}

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

  it('renders only substantive versions as primary rows and groups runs as an annotation (FAR-1817)', async () => {
    const wrapper = await flushMount(modernHistory())
    // Primary rows: the run-less edit (v6), the initial anchor (v1) and the
    // edit (v4) only.
    expect(rowFor(wrapper, 'snap-1').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-4').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-6').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-2').exists()).toBe(false)
    expect(rowFor(wrapper, 'snap-3').exists()).toBe(false)
    expect(rowFor(wrapper, 'snap-5').exists()).toBe(false)
    // Run counts annotate the version they executed.
    expect(toggleFor(wrapper, 'snap-1').text()).toBe('ran 2x')
    expect(toggleFor(wrapper, 'snap-4').text()).toBe('ran 1x')
    // The kind badge reads the created_kind, not version_kind.
    expect(rowFor(wrapper, 'snap-1').text()).toContain('initial')
    expect(rowFor(wrapper, 'snap-4').text()).toContain('edit')
    // M7: a version with NO runs has no annotation toggle (snap-6 is edit v6
    // with no run snapshots; snap-1/snap-4 above DO have one).
    expect(toggleFor(wrapper, 'snap-6').exists()).toBe(false)
  })

  it('renders an unrecognised created_kind as a VISIBLE substantive row, not a run (FAR-1817)', async () => {
    // An unknown/future kind must not be silently folded into the run bucket
    // (which would hide a real version) — nor mislabelled as a run.
    const future = [
      makeSnapshot({ id: 'snap-2', snapshot_version: 2, created_kind: 'imported', version_kind: 'edit', tag: null }),
      makeSnapshot({ id: 'snap-1', snapshot_version: 1, created_kind: 'edit', version_kind: 'edit', tag: null }),
    ]
    const wrapper = await flushMount(future)
    expect(rowFor(wrapper, 'snap-2').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-2').text()).toContain('imported')
  })

  it('expands and collapses the run annotation on demand (FAR-1817)', async () => {
    const wrapper = await flushMount(modernHistory())
    const listId = 'snapshot-timeline-runs-snap-1'
    // The container is ALWAYS mounted (so aria-controls always resolves) and
    // hidden with the `hidden` attribute — present, not absent, while collapsed.
    expect(wrapper.find(`#${listId}`).exists()).toBe(true)
    expect(wrapper.find(`#${listId}`).attributes('hidden')).toBeDefined()
    expect(toggleFor(wrapper, 'snap-1').attributes('aria-expanded')).toBe('false')
    expect(toggleFor(wrapper, 'snap-1').attributes('aria-controls')).toBe(listId)

    await toggleFor(wrapper, 'snap-1').trigger('click')
    expect(toggleFor(wrapper, 'snap-1').attributes('aria-expanded')).toBe('true')
    const list = wrapper.find(`#${listId}`)
    expect(list.exists()).toBe(true)
    expect(list.attributes('hidden')).toBeUndefined()
    expect(list.find('[data-testid="snapshot-timeline-run-snap-3"]').exists()).toBe(true)
    expect(list.find('[data-testid="snapshot-timeline-run-snap-2"]').exists()).toBe(true)

    await toggleFor(wrapper, 'snap-1').trigger('click')
    expect(toggleFor(wrapper, 'snap-1').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find(`#${listId}`).attributes('hidden')).toBeDefined()
  })

  it('opens the read-only historic version view when a primary row is clicked (FAR-1812/FAR-1817)', async () => {
    const wrapper = await flushMount(modernHistory())
    await rowFor(wrapper, 'snap-4').trigger('click')
    expect(wrapper.emitted('view')).toEqual([['snap-4']])
  })

  it('opens the historic view for an individual run snapshot from an expanded annotation (FAR-1817)', async () => {
    const wrapper = await flushMount(modernHistory())
    await toggleFor(wrapper, 'snap-1').trigger('click')
    await wrapper.find('[data-testid="snapshot-timeline-run-snap-3"]').trigger('click')
    expect(wrapper.emitted('view')).toEqual([['snap-3']])
  })

  it('renders a legacy pipeline under a single initial anchor with no synthetic versions (FAR-1817)', async () => {
    // Every snapshot is a run snapshot: no edit boundary exists to reconstruct.
    const legacy = [
      makeSnapshot({ id: 'snap-3', snapshot_version: 3, created_kind: 'run', version_kind: 'run', tag: null }),
      makeSnapshot({ id: 'snap-2', snapshot_version: 2, created_kind: 'run', version_kind: 'run', tag: null }),
      makeSnapshot({ id: 'snap-1', snapshot_version: 1, created_kind: 'run', version_kind: 'run', tag: null }),
    ]
    const wrapper = await flushMount(legacy)
    // Exactly one primary row — the oldest snapshot promoted to the initial anchor.
    expect(wrapper.findAll('[data-testid^="snapshot-timeline-row-"]')).toHaveLength(1)
    expect(rowFor(wrapper, 'snap-1').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-2').exists()).toBe(false)
    expect(rowFor(wrapper, 'snap-3').exists()).toBe(false)
    expect(rowFor(wrapper, 'snap-1').text()).toContain('initial')
    expect(toggleFor(wrapper, 'snap-1').text()).toBe('ran 2x')
  })

  it('buckets runs older than every substantive version as "earlier versions", never attributing them to the oldest row (M2)', async () => {
    // A modern pipeline whose oldest run predates its first edit boundary must
    // not claim that run executed the edit — it goes to an explicit earlier
    // bucket instead of asserting false provenance.
    const mixed = [
      makeSnapshot({ id: 'snap-3', snapshot_version: 3, created_kind: 'run', version_kind: 'run', tag: null }),
      makeSnapshot({ id: 'snap-2', snapshot_version: 2, created_kind: 'edit', version_kind: 'edit', tag: null }),
      makeSnapshot({ id: 'snap-1', snapshot_version: 1, created_kind: 'run', version_kind: 'run', tag: null }),
    ]
    const wrapper = await flushMount(mixed)
    expect(wrapper.findAll('[data-testid^="snapshot-timeline-row-"]')).toHaveLength(1)
    expect(rowFor(wrapper, 'snap-2').exists()).toBe(true)
    expect(rowFor(wrapper, 'snap-2').text()).toContain('edit')
    // Only the run that actually executed v2 is attributed to it.
    expect(toggleFor(wrapper, 'snap-2').text()).toBe('ran 1x')
    // The older run is surfaced, unattributed, in its own bucket.
    const earlier = wrapper.find('[data-testid="snapshot-timeline-earlier"]')
    expect(earlier.exists()).toBe(true)
    expect(wrapper.find('[data-testid="snapshot-timeline-earlier-toggle"]').text()).toBe('ran 1x on earlier versions')
    // Expands to the individual run, still outside any version row.
    await wrapper.find('[data-testid="snapshot-timeline-earlier-toggle"]').trigger('click')
    expect(wrapper.find('[data-testid="snapshot-timeline-earlier-run-snap-1"]').exists()).toBe(true)
  })

  it('does not emit view when the selection radio is used', async () => {
    const wrapper = await flushMount(modernHistory())
    await selectVersion(wrapper, 'snap-1')
    expect(wrapper.emitted('view')).toBeUndefined()
  })

  it('shows a dash for snapshots without a date', async () => {
    const wrapper = await flushMount([makeSnapshot({ created_at: null })])
    expect(rowFor(wrapper, 'snap-1').text()).toMatch(/[-\u2014]/)
  })

  it('hands the selected version to the host as a restore request (FAR-1816)', async () => {
    const wrapper = await flushMount(modernHistory())
    expect(rowFor(wrapper, 'snap-1').classes()).not.toContain('bg-primary')
    await selectVersion(wrapper, 'snap-1')
    const restoreBtn = wrapper.find('[data-testid="snapshot-timeline-rollback"]')
    expect(restoreBtn.attributes('disabled')).toBeUndefined()
    expect(restoreBtn.text()).toBe('Restore this version')
    await restoreBtn.trigger('click')
    await flushPromises()
    // The timeline no longer POSTs the rollback itself — the host editor owns
    // the shared restore dialog (diff summary + guard-refusal copy).
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.emitted('restore')).toEqual([['snap-1', 1]])
  })

  it('disables the restore action until a snapshot row is selected', async () => {
    const wrapper = await flushMount([makeSnapshot()])
    expect(wrapper.find('[data-testid="snapshot-timeline-rollback"]').attributes('disabled')).toBeDefined()
    await wrapper.find('[data-testid="snapshot-timeline-rollback"]').trigger('click')
    expect(wrapper.emitted('restore')).toBeUndefined()
  })

  it('re-fetches snapshots when the host asks it to reload (FAR-1816)', async () => {
    const wrapper = await flushMount([makeSnapshot()])
    expect(apiGet).toHaveBeenCalledTimes(1)
    ;(wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    await flushPromises()
    expect(apiGet).toHaveBeenCalledTimes(2)
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
    const wrapper = await flushMount(twoSubstantive())
    await selectVersion(wrapper, 'snap-1')
    const diffBtn = wrapper.find('[data-testid="snapshot-timeline-diff"]')
    expect(diffBtn.attributes('disabled')).toBeDefined()
    // Selecting the first compare option (snap-2, the newer substantive version).
    await wrapper.find('[data-testid="mock-select"]').setValue('0')
    expect(diffBtn.attributes('disabled')).toBeUndefined()
    await diffBtn.trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/v1/pipelines/{pipeline_id}/snapshots/diff', expect.objectContaining({
      params: { path: { pipeline_id: 'pipe-1' } },
      // The compare Select passes option-value="value", so compareB holds the
      // snapshot id (not the whole {value,label} option object) and the diff
      // request posts a real snapshot id (FAR-606).
      body: {
        snapshot_a_id: 'snap-1',
        snapshot_b_id: 'snap-2',
      },
    }))
    const result = wrapper.find('[data-testid="snapshot-timeline-diff-result"]')
    expect(result.exists()).toBe(true)
    // impactedLabel truncates each node id to 8 characters.
    expect(result.text()).toContain('node-aaa, node-bbb')
    expect(result.text()).toContain('Input port removed')
  })

  it('renders a non-block breaking change with the warning-text token', async () => {
    apiPost.mockResolvedValue({
      data: {
        semantic: {
          impacted_nodes: [],
          breaking_changes: [{ severity: 'warn', reason: 'Port type widened' }],
        },
      },
      error: undefined,
    })
    const wrapper = await flushMount(twoSubstantive())
    await selectVersion(wrapper, 'snap-1')
    await wrapper.find('[data-testid="mock-select"]').setValue('0')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    const result = wrapper.find('[data-testid="snapshot-timeline-diff-result"]')
    expect(result.text()).toContain('warn')
    expect(result.html()).toContain('text-warning-text')
  })

  it('lists no impacted nodes and no breaking changes for a clean diff', async () => {
    apiPost.mockResolvedValue({ data: { semantic: { impacted_nodes: [], breaking_changes: [] } }, error: undefined })
    const wrapper = await flushMount(twoSubstantive())
    await selectVersion(wrapper, 'snap-1')
    await wrapper.find('[data-testid="mock-select"]').setValue('0')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    const result = wrapper.find('[data-testid="snapshot-timeline-diff-result"]')
    expect(result.text()).toContain('none')
    expect(result.text()).toContain('No breaking port changes')
  })

  it('shows an action error when the diff request fails', async () => {
    apiPost.mockResolvedValue({ data: null, error: { detail: 'diff unavailable' } })
    const wrapper = await flushMount(twoSubstantive())
    await selectVersion(wrapper, 'snap-1')
    await wrapper.find('[data-testid="mock-select"]').setValue('0')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    // Structured errors render via formatApiError (same as the rollback path).
    expect(wrapper.text()).toContain('diff unavailable')
    expect(wrapper.text()).not.toContain('[object Object]')
  })

  it('shows a retryable error (not a false empty state) when the snapshot load fails (M3)', async () => {
    const wrapper = await flushMount(null, true)
    expect(wrapper.find('[data-testid="snapshot-timeline-load-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Could not load this pipeline\'s version history')
    expect(wrapper.text()).not.toContain('No snapshots yet')
    // The retry re-issues the request.
    expect(apiGet).toHaveBeenCalledTimes(1)
    await wrapper.find('[data-testid="snapshot-timeline-load-retry"]').trigger('click')
    await flushPromises()
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('surfaces a truncation notice and a Load more action when history exceeds one page (M1)', async () => {
    const pageOne = Array.from({ length: 100 }, (_, i) =>
      makeSnapshot({ id: `snap-${101 - i}`, snapshot_version: 101 - i, created_kind: 'edit', tag: null }),
    )
    const older = makeSnapshot({ id: 'snap-oldest', snapshot_version: 1, created_kind: 'initial', tag: null })
    apiGet.mockImplementation((_url: string, init?: any) => {
      const page = (init?.params?.query?.page ?? 1) as number
      if (page === 1) return Promise.resolve({ data: { items: pageOne, total: 101 }, error: undefined })
      return Promise.resolve({ data: { items: [older], total: 101 }, error: undefined })
    })
    const wrapper = mount(PipelineSnapshotTimeline, {
      props: { pipelineId: 'pipe-1' },
      global: { stubs: { Select: SelectStub } },
    })
    await flushPromises()

    const notice = wrapper.find('[data-testid="snapshot-timeline-truncated"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('Showing the most recent 100 of 101 versions')

    await wrapper.find('[data-testid="snapshot-timeline-load-more"]').trigger('click')
    await flushPromises()
    // The older page is appended and the truncation notice disappears.
    expect(wrapper.find('[data-testid="snapshot-timeline-row-snap-oldest"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="snapshot-timeline-truncated"]').exists()).toBe(false)
  })

  it('reports a failed "load more" without blanking the rows already shown (M1/M3)', async () => {
    const pageOne = Array.from({ length: 100 }, (_, i) =>
      makeSnapshot({ id: `snap-${101 - i}`, snapshot_version: 101 - i, created_kind: 'edit', tag: null }),
    )
    let call = 0
    apiGet.mockImplementation(() => {
      call += 1
      if (call === 1) return Promise.resolve({ data: { items: pageOne, total: 101 }, error: undefined })
      return Promise.resolve({ data: undefined, error: { detail: 'page two unavailable' } })
    })
    const wrapper = mount(PipelineSnapshotTimeline, {
      props: { pipelineId: 'pipe-1' },
      global: { stubs: { Select: SelectStub } },
    })
    await flushPromises()
    await wrapper.find('[data-testid="snapshot-timeline-load-more"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="snapshot-timeline-load-more-error"]').exists()).toBe(true)
    // Existing rows survive a failed page.
    expect(wrapper.find('[data-testid="snapshot-timeline-row-snap-101"]').exists()).toBe(true)
  })

  it('drops a stale diff result when the request throws, showing the error instead (M4)', async () => {
    const wrapper = await flushMount(twoSubstantive())
    // A first successful diff populates the result.
    apiPost.mockResolvedValueOnce({ data: { semantic: { impacted_nodes: ['n1'], breaking_changes: [] } }, error: undefined })
    await selectVersion(wrapper, 'snap-1')
    await wrapper.find('[data-testid="mock-select"]').setValue('0')
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="snapshot-timeline-diff-result"]').exists()).toBe(true)

    // A later request THROWS (network/abort) — no unhandled rejection, the
    // stale result is cleared and the error surfaces.
    apiPost.mockRejectedValueOnce(new Error('socket down'))
    await wrapper.find('[data-testid="snapshot-timeline-diff"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="snapshot-timeline-diff-result"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('socket down')
  })

  it('applies only the newest snapshot response, ignoring a slower earlier one (M6)', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    const first = new Promise((resolve) => { resolveFirst = resolve })
    let call = 0
    apiGet.mockImplementation(() => {
      call += 1
      if (call === 1) return first
      return Promise.resolve({
        data: { items: [makeSnapshot({ id: 'newer', snapshot_version: 5, created_kind: 'edit', tag: null })], total: 1 },
        error: undefined,
      })
    })
    const wrapper = mount(PipelineSnapshotTimeline, {
      props: { pipelineId: 'pipe-1' },
      global: { stubs: { Select: SelectStub } },
    })
    // A host-triggered reload supersedes the slow mount load.
    ;(wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    await flushPromises()
    // Now let the OLD (first) request resolve with stale rows.
    resolveFirst({ data: { items: [makeSnapshot({ id: 'stale', snapshot_version: 1, created_kind: 'edit', tag: null })], total: 1 }, error: undefined })
    await flushPromises()

    expect(wrapper.find('[data-testid="snapshot-timeline-row-newer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="snapshot-timeline-row-stale"]').exists()).toBe(false)
  })

  it('emits close from the close button', async () => {
    const wrapper = await flushMount([makeSnapshot()])
    await wrapper.find('[data-testid="snapshot-timeline-close"]').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
