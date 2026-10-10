import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

const { sampleResponse, mockGet, mockPatch, mockPost } = vi.hoisted(() => {
  const makeItem = (id: string, feedback_status: string, producing_node_id: string) => ({
    id,
    run_id: 'run-1',
    review_id: 'review',
    rejected_by: null,
    rejection_reason: '',
    rejected_output: {},
    producing_node_id,
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
      items: [
        makeItem('rec-1', 'pending', 'node-1'),
        makeItem('rec-2', 'pending', 'node-2'),
        makeItem('rec-3', 'pending', '11111111-1111-1111-1111-111111111111'),
      ],
      total: 3,
      page: 1,
      page_size: 10,
    },
    mockGet: vi.fn(),
    mockPatch: vi.fn(),
    mockPost: vi.fn(),
  }
})

vi.mock('../lib/api/client', () => ({
  api: {
    GET: mockGet,
    PATCH: mockPatch,
    POST: mockPost,
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
import PageTabs from '../components/PageTabs.vue'

// The PrimeVue Dialog portals to <body>; this stub renders the default and
// footer slots inline so the publish form and its buttons are DOM-reachable.
const dialogStub = { template: '<div><slot /><slot name="footer" /></div>' }

function mountView() {
  return mount(EvalProposalsQueueView, {
    global: {
      stubs: {
        FeatureGate: { template: '<div><slot /></div>' },
        Dialog: dialogStub,
      },
      mocks: { $t: (key: string) => key },
    },
  })
}

async function flush() {
  await flushPromises()
  await nextTick()
}

describe('EvalProposalsQueueView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: sampleResponse, error: undefined })
    mockPatch.mockResolvedValue({ data: {}, error: undefined })
    mockPost.mockResolvedValue({ data: {}, error: undefined })
  })

  it('renders without crashing', async () => {
    const wrapper = mountView()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.title')
  })

  it('does not render the redundant page-level tab strip (FAR-1236)', async () => {
    const wrapper = mountView()
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="proposal-card-rec-1"]').exists()).toBe(true)
    })
    expect(wrapper.findComponent(PageTabs).exists()).toBe(false)
  })

  it('POSTs the publish endpoint with snake_case body on submit', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(true)

    await wrapper.find('[data-testid="publish-name"]').setValue('Response quality check')
    await wrapper.find('[data-testid="publish-config"]').setValue('{"criteria":["accurate"]}')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    // Body fields are snake_case (PublishEvalProposalRequest). node_id is
    // omitted because 'node-1' is not a UUID — the backend resolves it.
    expect(mockPost).toHaveBeenCalledWith('/api/v1/feedback/proposals/{record_id}/publish', {
      params: { path: { record_id: 'rec-1' } },
      body: {
        name: 'Response quality check',
        eval_type: 'llm_judge',
        config: { criteria: ['accurate'] },
      },
    })
  })

  it('includes node_id when the producing node id is a UUID', async () => {
    const wrapper = mountView()
    await flush()

    const publishButtons = wrapper.findAll('[data-testid="proposal-publish"]')
    await publishButtons[2].trigger('click')
    await nextTick()

    await wrapper.find('[data-testid="publish-name"]').setValue('Schema check')
    await wrapper.find('[data-testid="publish-config"]').setValue('{}')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(mockPost).toHaveBeenCalledWith('/api/v1/feedback/proposals/{record_id}/publish', {
      params: { path: { record_id: 'rec-3' } },
      body: {
        name: 'Schema check',
        eval_type: 'llm_judge',
        config: {},
        node_id: '11111111-1111-1111-1111-111111111111',
      },
    })
  })

  it('refreshes the list, shows success, and closes the dialog on publish', async () => {
    const wrapper = mountView()
    await flush()
    const getCallsBefore = mockGet.mock.calls.length

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(mockGet.mock.calls.length).toBeGreaterThan(getCallsBefore)
    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.publish_success')
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(false)
  })

  it('blocks submit and shows an error for invalid config JSON', async () => {
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-config"]').setValue('{ not json')
    await nextTick()

    expect(wrapper.text()).toContain('views.EvalProposalsQueueView.config_invalid')
    const confirm = wrapper.find('[data-testid="publish-confirm"]')
    expect(confirm.attributes('disabled')).toBeDefined()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('surfaces a publish failure in the dialog and keeps it open', async () => {
    mockPost.mockResolvedValueOnce({ data: undefined, error: { detail: 'publish exploded' } })
    const wrapper = mountView()
    await flush()

    await wrapper.find('[data-testid="proposal-publish"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-testid="publish-name"]').setValue('X')
    await wrapper.find('[data-testid="publish-confirm"]').trigger('click')
    await flush()

    expect(wrapper.find('[data-testid="publish-dialog-error"]').text()).toContain('publish exploded')
    // The dialog stays open so the reviewer can correct and retry.
    expect(wrapper.find('[data-testid="publish-name"]').exists()).toBe(true)
  })

  it('dismissProposal replaces the whole response through the writable computed (FAR-645)', async () => {
    const wrapper = mountView()
    await flush()

    const before = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(before.find((x) => x.id === 'rec-2')?.feedback_status).toBe('pending')

    await (wrapper.vm as unknown as { dismissProposal: (id: string) => Promise<void> }).dismissProposal('rec-2')
    await flush()

    expect(mockPatch).toHaveBeenCalledWith('/api/v1/feedback/{record_id}/status', expect.objectContaining({ body: { status: 'dismissed' }, params: { path: { record_id: 'rec-2' } } }))

    const after = (wrapper.vm as unknown as { proposalsResp: typeof sampleResponse }).proposalsResp.items
    expect(after).not.toBe(before)
    expect(after.find((x) => x.id === 'rec-2')?.feedback_status).toBe('dismissed')
    expect(after.find((x) => x.id === 'rec-2')).not.toBe(before.find((x) => x.id === 'rec-2'))
  })
})
