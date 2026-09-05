import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

type ApiResult = { data?: unknown; error?: Record<string, unknown> | undefined }

let mockRecords: Array<Record<string, unknown>> = []
let mockInboxError: Record<string, unknown> | undefined
let mockPipelines: Array<Record<string, unknown>> = []
let mockPipelinesError: Record<string, unknown> | undefined
let mockDetail: Record<string, unknown> | null = null
let mockDetailError: Record<string, unknown> | undefined
let mockReviewResult: Record<string, unknown> | null = null
let mockReviewError: Record<string, unknown> | undefined

function res(data: unknown, error?: Record<string, unknown>): ApiResult {
  return { data, error }
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/feedback/inbox') {
        if (mockInboxError) return Promise.resolve(res(null, mockInboxError))
        return Promise.resolve(res({ items: mockRecords }))
      }
      if (url === '/api/v1/feedback/inbox/{record_id}') {
        if (mockDetailError) return Promise.resolve(res(null, mockDetailError))
        return Promise.resolve(res(mockDetail))
      }
      if (url === '/api/v1/pipelines') {
        if (mockPipelinesError) return Promise.resolve(res(null, mockPipelinesError))
        return Promise.resolve(res({ items: mockPipelines }))
      }
      return Promise.resolve(res(null))
    }),
    POST: vi.fn().mockImplementation(() => {
      if (mockReviewError) return Promise.resolve(res(null, mockReviewError))
      return Promise.resolve(res(mockReviewResult))
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import FeedbackInboxView from '../views/FeedbackInboxView.vue'

function record(overrides: Record<string, unknown> = {}) {
  return {
    id: 'rec-1',
    created_at: '2026-08-01T10:00:00Z',
    pipeline_name: 'Deploy Pipeline',
    rejection_reason: 'output did not match schema',
    feedback_handler_type: 'human',
    feedback_status: 'pending',
    ...overrides,
  }
}

async function mountWithList(items: Array<Record<string, unknown>>) {
  mockRecords = items
  const wrapper = mount(FeedbackInboxView)
  await flushPromises()
  await nextTick()
  return wrapper
}

function firstRowToggle(wrapper: ReturnType<typeof mount>) {
  return wrapper.find('[data-testid="feedback-inbox-toggle-expand"]')
}

describe('FeedbackInboxView', () => {
  beforeEach(() => {
    mockRecords = []
    mockInboxError = undefined
    mockPipelines = []
    mockPipelinesError = undefined
    mockDetail = null
    mockDetailError = undefined
    mockReviewResult = null
    mockReviewError = undefined
    vi.clearAllMocks()
  })

  it('renders the empty state when there are no feedback records', async () => {
    const wrapper = await mountWithList([])
    expect(wrapper.find('[data-testid="feedback-inbox-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('No feedback yet')
    wrapper.unmount()
  })

  it('renders feedback records with status badges and handler labels', async () => {
    const wrapper = await mountWithList([
      record(),
      record({ id: 'rec-2', pipeline_name: 'Incident Triage', feedback_status: 'escalated', feedback_handler_type: 'ai_correction', rejection_reason: null }),
    ])
    expect(wrapper.text()).toContain('Deploy Pipeline')
    expect(wrapper.text()).toContain('Incident Triage')
    expect(wrapper.text()).toContain('pending')
    expect(wrapper.text()).toContain('escalated')
    expect(wrapper.text()).toContain('Human')
    expect(wrapper.text()).toContain('AI Correction')
    // unknown handler type falls back to the raw type string
    const wrapper2 = await (async () => {
      wrapper.unmount()
      return mountWithList([record({ feedback_handler_type: 'exotic' })])
    })()
    expect(wrapper2.text()).toContain('exotic')
    wrapper2.unmount()
  })

  it('shows an error alert when the inbox list fails to load', async () => {
    mockInboxError = { detail: 'inbox_disabled' }
    const wrapper = mount(FeedbackInboxView)
    await flushPromises()
    expect(wrapper.text()).toContain('inbox_disabled')
    wrapper.unmount()
  })

  it('shows an error alert when the pipelines lookup fails', async () => {
    mockPipelinesError = { detail: 'pipelines_500' }
    const wrapper = mount(FeedbackInboxView)
    await flushPromises()
    expect(wrapper.text()).toContain('pipelines_500')
    wrapper.unmount()
  })

  it('expands a record and loads its detail with rejection output and proposal', async () => {
    mockDetail = {
      ...record(),
      annotation: 'looks wrong',
      rejected_output: { answer: 42 },
      correction_proposal: { fix: 'retry' },
    }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    expect(vi.mocked(vi.mocked((await import('../lib/api/client')).api.GET)).mock.calls.some((c) => c[0] === '/api/v1/feedback/inbox/{record_id}')).toBe(true)
    expect(wrapper.text()).toContain('Rejection Reason')
    expect(wrapper.text()).toContain('Rejected Output')
    expect(wrapper.text()).toContain('Correction Proposal')
    // annotation textarea is pre-filled from the saved annotation
    const textarea = wrapper.find('[data-testid="feedback-inbox-annotation"]')
    expect((textarea.element as HTMLTextAreaElement).value).toBe('looks wrong')
    wrapper.unmount()
  })

  it('collapsing an expanded record hides the detail panel', async () => {
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    const toggle = firstRowToggle(wrapper)
    await toggle.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Rejected Output')
    await toggle.trigger('click')
    await nextTick()
    expect(wrapper.text()).not.toContain('Rejected Output')
    wrapper.unmount()
  })

  it('shows the detail error with a Retry button and reloads on retry', async () => {
    mockDetailError = { detail: 'detail_404' }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Failed to load detail')
    expect(wrapper.text()).toContain('detail_404')

    mockDetailError = undefined
    mockDetail = { ...record(), rejected_output: { ok: true } }
    const retry = wrapper.find('[data-testid="feedback-inbox-retry"]')
    expect(retry.exists()).toBe(true)
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Rejected Output')
    wrapper.unmount()
  })

  it('triggers a correction run for a pending record', async () => {
    mockReviewResult = { ...record(), feedback_status: 'correcting' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    const btn = wrapper.find('[data-testid="feedback-inbox-trigger-correction"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(vi.mocked((await import('../lib/api/client')).api.POST)).mock.calls[0]
    expect(post[0]).toBe('/api/v1/feedback/inbox/{record_id}/review')
    expect((post[1] as any).body.action).toBe('create_correction_run')
    expect(wrapper.text()).toContain('Correction run triggered.')
    // NOTE: the list badge flip (`rec.feedback_status = 'correcting'`) is
    // asserted separately as a BUG characterisation below.
    wrapper.unmount()
  })

  it('saves an annotation on a record', async () => {
    mockReviewResult = { ...record(), annotation: 'reviewed' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="feedback-inbox-annotation"]').setValue('reviewed')
    const save = wrapper.find('[data-testid="feedback-inbox-save-annotation"]')
    await save.trigger('click')
    await flushPromises()
    await nextTick()

    const post = vi.mocked(vi.mocked((await import('../lib/api/client')).api.POST)).mock.calls[0]
    expect((post[1] as any).body.action).toBe('mark_reviewed')
    expect((post[1] as any).body.annotation).toBe('reviewed')
    expect(wrapper.text()).toContain('Annotation saved.')
    wrapper.unmount()
  })

  it('marks a record resolved via the resolve button', async () => {
    mockReviewResult = { ...record(), feedback_status: 'resolved' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    const resolve = wrapper.find('[data-testid="feedback-inbox-mark-resolved"]')
    await resolve.trigger('click')
    await flushPromises()
    await nextTick()
    const post = vi.mocked(vi.mocked((await import('../lib/api/client')).api.POST)).mock.calls[0]
    expect((post[1] as any).body.action).toBe('mark_reviewed')
    expect(wrapper.text()).toContain('Marked as resolved.')
    wrapper.unmount()
  })

  it('dismisses a record via the dismiss button', async () => {
    mockReviewResult = { ...record(), feedback_status: 'dismissed' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    const dismiss = wrapper.find('[data-testid="feedback-inbox-dismiss"]')
    await dismiss.trigger('click')
    await flushPromises()
    await nextTick()
    const post = vi.mocked(vi.mocked((await import('../lib/api/client')).api.POST)).mock.calls[0]
    expect((post[1] as any).body.action).toBe('dismiss')
    expect(wrapper.text()).toContain('Dismissed.')
    wrapper.unmount()
  })

  it('shows an error message when the review action fails', async () => {
    mockReviewError = { detail: 'review_not_allowed' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="feedback-inbox-save-annotation"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.text()).toContain('Save failed')
    expect(wrapper.text()).toContain('review_not_allowed')
    wrapper.unmount()
  })

  it('BUG: resolve/dismiss/correction do not update the list badge (readonly vue-query data)', async () => {
    // Production bug characterisation. resolveRecord()/dismissRecord()/
    // triggerCorrection() write `rec.feedback_status = '...'` on an item that
    // comes from @tanstack/vue-query's deep-readonly query state. Vue drops
    // the write, so the row badge keeps showing the OLD status.
    mockReviewResult = { ...record(), feedback_status: 'resolved' }
    mockDetail = { ...record(), rejected_output: { a: 1 } }
    const wrapper = await mountWithList([record()])
    await firstRowToggle(wrapper).trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="feedback-inbox-mark-resolved"]').trigger('click')
    await flushPromises()
    await nextTick()

    // the row badge STILL says pending even though the review succeeded
    const badge = wrapper.findAll('span').find((s) => s.classes().includes('badge'))
    expect(badge?.text()).toBe('pending')
    wrapper.unmount()
  })

  it('applies the status filter to the inbox query', async () => {
    const wrapper = await mountWithList([])
    await flushPromises()
    const get = vi.mocked(vi.mocked((await import('../lib/api/client')).api.GET))
    get.mockClear()

    const FilterBar = (await import('../components/shared/FilterBar.vue')).default
    const bar = wrapper.findComponent(FilterBar)
    ;(bar.vm as unknown as { $emit: (e: string, ...args: unknown[]) => void }).$emit('update:filter', 'status', 'pending')
    await flushPromises()

    const call = get.mock.calls[0]
    expect(call[0]).toBe('/api/v1/feedback/inbox')
    expect((call[1] as any).params.query).toEqual({ status: 'pending' })
    wrapper.unmount()
  })

  it('applies date range and pipeline filters to the inbox query', async () => {
    const wrapper = await mountWithList([])
    await flushPromises()
    const get = vi.mocked(vi.mocked((await import('../lib/api/client')).api.GET))
    get.mockClear()

    await wrapper.find('[data-testid="feedback-inbox-date-from"]').setValue('2026-08-01')
    await wrapper.find('[data-testid="feedback-inbox-date-to"]').setValue('2026-08-31')
    await flushPromises()

    const calls = get.mock.calls.filter((c) => c[0] === '/api/v1/feedback/inbox')
    expect(calls.length).toBeGreaterThan(0)
    const last = calls[calls.length - 1]
    expect((last[1] as any).params.query.date_from).toBe('2026-08-01')
    expect((last[1] as any).params.query.date_to).toBe('2026-08-31')
    wrapper.unmount()
  })

  it('omits empty filters from the inbox query', async () => {
    const wrapper = await mountWithList([])
    await flushPromises()
    const get = vi.mocked(vi.mocked((await import('../lib/api/client')).api.GET))
    const inboxCalls = get.mock.calls.filter((c) => c[0] === '/api/v1/feedback/inbox')
    expect(inboxCalls.length).toBeGreaterThan(0)
    // no filters active → empty query object
    expect((inboxCalls[0][1] as any)?.params?.query).toEqual({})
    wrapper.unmount()
  })

  it('BUG: the pipeline filter Select is never imported and renders as a broken native select', async () => {
    // Production bug characterisation. FeedbackInboxView.vue uses <Select>
    // (the pipeline filter) but its <script setup> never imports it from
    // 'primevue/select'. Vue fails to resolve the component and falls back
    // to a native <select> element: the :options binding becomes a plain
    // attribute and NO option elements are ever rendered, so the pipeline
    // filter dropdown is empty and unusable in production.
    mockRecords = []
    mockPipelines = [{ id: 'p1', name: 'Deploy Pipeline' }]
    const wrapper = mount(FeedbackInboxView)
    await flushPromises()
    await nextTick()

    const nativeSelect = wrapper.find('select[data-testid="feedback-inbox-pipeline-select"]')
    expect(nativeSelect.exists()).toBe(true)
    expect(nativeSelect.findAll('option').length).toBe(0)
    wrapper.unmount()
  })
})
