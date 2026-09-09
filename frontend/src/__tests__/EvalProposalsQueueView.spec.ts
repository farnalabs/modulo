import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { sampleResponse, mockPatch } = vi.hoisted(() => {
  const makeItem = (id: string, feedback_status: string) => ({
    id,
    run_id: 'run-1',
    gate_id: 'review',
    rejected_by: null,
    rejection_reason: '',
    rejected_output: {},
    producing_node_id: 'node-1',
    producing_node_name: 'Critic',
    producing_agent_id: null,
    feedback_status,
    feedback_handler_type: 'ai_correction',
    correction_run_id: null,
    eval_gap: false,
    needs_human_review: true,
    pipeline_name: 'Triage',
    created_at: '2026-01-01T00:00:00Z',
  })
  return {
    sampleResponse: {
      items: [makeItem('rec-1', 'pending'), makeItem('rec-2', 'pending')],
      total: 2,
      page: 1,
      page_size: 10,
    },
    mockGet: vi.fn(),
    mockPatch: vi.fn(),
  }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/feedback/proposals')) {
        return Promise.resolve({ data: sampleResponse, error: undefined })
      }
      return Promise.resolve({ data: null, error: undefined })
    }),
    PATCH: mockPatch.mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

// Mirror AdminUsersView.spec.ts: the writable `proposalsResp` computed is driven
// by useDataFetch's load() so the seeded proposal list is visible to the view.
vi.mock('../composables/useDataFetch', async () => {
  const { ref } = await import('vue')
  return {
    useDataFetch: (
      fetcher: () => Promise<{ data: unknown; error?: unknown }>,
      options?: { initialValue?: unknown },
    ) => {
      const data = ref(options?.initialValue)
      const load = async () => {
        const result = await fetcher()
        ;(data as { value: unknown }).value = result.data ?? options?.initialValue
      }
      void load()
      return { data, loading: ref(false), error: ref(''), load }
    },
  }
})

import EvalProposalsQueueView from '../views/EvalProposalsQueueView.vue'

describe('EvalProposalsQueueView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    // clearAllMocks() also strips mockPatch's resolved value; re-establish it so
    // publishProposal/dismissProposal take the success path.
    mockPatch.mockResolvedValue({ data: {}, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(EvalProposalsQueueView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.title')
  })

  it('publishProposal replaces the whole response through the writable computed (FAR-645)', async () => {
    const wrapper = mount(EvalProposalsQueueView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await flushPromises()
    await nextTick()

    const before = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(before.find((x) => x.id === 'rec-1')?.feedback_status).toBe('pending')

    await (wrapper.vm as unknown as { publishProposal: (p: { id: string }) => Promise<void> }).publishProposal({ id: 'rec-1' })
    await flushPromises()
    await nextTick()

    expect(mockPatch).toHaveBeenCalledWith('/api/v1/feedback/{record_id}/status', expect.objectContaining({ body: { status: 'resolved' }, params: { path: { record_id: 'rec-1' } } }))

    const after = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    // Whole-response reassignment: a brand-new items array, not an in-place mutation
    // that vue-query's deep-readonly data would silently drop (FAR-630/FAR-645).
    expect(after).not.toBe(before)
    expect(after.find((x) => x.id === 'rec-1')?.feedback_status).toBe('resolved')
    // The published item object itself must be replaced, not mutated in place.
    expect(after.find((x) => x.id === 'rec-1')).not.toBe(before.find((x) => x.id === 'rec-1'))
  })

  it('dismissProposal replaces the whole response through the writable computed (FAR-645)', async () => {
    const wrapper = mount(EvalProposalsQueueView, {
      global: {
        stubs: { FeatureGate: { template: '<div><slot /></div>' } },
        mocks: { $t: (key: string) => key },
      },
    })
    await flushPromises()
    await nextTick()

    const before = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(before.find((x) => x.id === 'rec-2')?.feedback_status).toBe('pending')

    await (wrapper.vm as unknown as { dismissProposal: (id: string) => Promise<void> }).dismissProposal('rec-2')
    await flushPromises()
    await nextTick()

    expect(mockPatch).toHaveBeenCalledWith('/api/v1/feedback/{record_id}/status', expect.objectContaining({ body: { status: 'dismissed' }, params: { path: { record_id: 'rec-2' } } }))

    const after = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(after).not.toBe(before)
    expect(after.find((x) => x.id === 'rec-2')?.feedback_status).toBe('dismissed')
    expect(after.find((x) => x.id === 'rec-2')).not.toBe(before.find((x) => x.id === 'rec-2'))
  })
})
