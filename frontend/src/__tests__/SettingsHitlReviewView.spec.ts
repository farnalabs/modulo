import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/schema', () => ({}))

import SettingsHitlReviewView from '../views/SettingsHitlReviewView.vue'

const PENDING_URL = '/api/v1/hitl/pending'

function mockGetWithGates(gates: unknown[]) {
  return (url: string) => {
    if (url === PENDING_URL) {
      return Promise.resolve({ data: { gates }, error: undefined })
    }
    if (url === '/api/v1/pipelines') {
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }
    return Promise.resolve({ data: { items: [] }, error: undefined })
  }
}

function pendingGateRow() {
  return {
    run_id: '550e8400-e29b-41d4-a716-446655440000',
    gate_id: 'approval-gate-1',
    pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
    claimed_by: null,
    claimed_at: null,
    expires_at: null,
    decision: null,
    decision_at: null,
    created_at: '2025-06-30T10:00:00Z',
  }
}

function problemDetail(detail: string) {
  // Shape produced by the api client wrapper: FastAPI's ProblemDetail body
  // (type/title/status/detail) or a raw {detail} normalized by toProblemDetail.
  return {
    type: 'urn:problem:modulo:conflict',
    title: 'Conflict',
    status: 409,
    detail,
  }
}

async function expandFirstGate(wrapper: VueWrapper) {
  const toggle = wrapper.find('[data-testid="hitl-review-toggle-expand"]')
  expect(toggle.exists()).toBe(true)
  await toggle.trigger('click')
  await flushPromises()
  await nextTick()
}

describe('SettingsHitlReviewView', () => {
  let wrapper: VueWrapper | null = null

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({ data: { gates: [] }, error: undefined })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper!.exists()).toBe(true)
    expect(wrapper!.text()).toContain('HITL Review')
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    expect(wrapper!.find('.animate-spin').exists()).toBe(true)
  })

  it('renders gates list', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({
      data: {
        gates: [
          {
            run_id: '550e8400-e29b-41d4-a716-446655440000',
            gate_id: 'approval-gate-1',
            pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
            claimed_by: null,
            claimed_at: null,
            expires_at: null,
            decision: null,
            decision_at: null,
            created_at: '2025-06-30T10:00:00Z',
          },
        ],
      },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper!.text()).toContain('#approval')
    expect(wrapper!.text()).toContain('pending')
  })

  it('expands gate detail panel on click', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({
      data: {
        gates: [
          {
            run_id: '550e8400-e29b-41d4-a716-446655440000',
            gate_id: 'approval-gate-1',
            pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
            claimed_by: null,
            claimed_at: null,
            expires_at: null,
            decision: null,
            decision_at: null,
            created_at: '2025-06-30T10:00:00Z',
          },
        ],
      },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    const toggle = wrapper!.find('[data-testid="hitl-review-toggle-expand"]')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')
    await nextTick()

    expect(wrapper!.text()).toContain('Claim Gate')
    expect(wrapper!.text()).toContain('Claim Metadata')
  })

  it('does not re-fetch gates when typing in the search box (client-side filtering)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/hitl/pending') {
        return Promise.resolve({
          data: {
            gates: [
              {
                run_id: '550e8400-e29b-41d4-a716-446655440000',
                gate_id: 'approval-gate-1',
                pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
                claimed_by: null,
                claimed_at: null,
                expires_at: null,
                decision: null,
                decision_at: null,
                created_at: '2025-06-30T10:00:00Z',
              },
              {
                run_id: '550e8400-e29b-41d4-a716-446655440002',
                gate_id: 'deploy-gate-1',
                pipeline_id: '660e8400-e29b-41d4-a716-446655440003',
                claimed_by: null,
                claimed_at: null,
                expires_at: null,
                decision: null,
                decision_at: null,
                created_at: '2025-06-30T10:00:00Z',
              },
            ],
          },
          error: undefined,
        })
      }
      if (url === '/api/v1/pipelines') {
        return Promise.resolve({
          data: {
            items: [
              { id: '660e8400-e29b-41d4-a716-446655440001', name: 'Alpha' },
              { id: '660e8400-e29b-41d4-a716-446655440003', name: 'Beta' },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()

    const callsAfterMount = (api.GET as any).mock.calls.length
    expect(callsAfterMount).toBeGreaterThan(0)
    const pendingCallsAfterMount = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/hitl/pending').length

    const searchInput = wrapper!.find('[data-testid="filter-bar-search"]')
    expect(searchInput.exists()).toBe(true)

    await searchInput.setValue('alpha')
    await flushPromises()
    await nextTick()

    expect((api.GET as any).mock.calls.length).toBe(callsAfterMount)
    expect((api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/hitl/pending').length).toBe(pendingCallsAfterMount)

    expect(wrapper!.text()).toContain('Alpha')
    expect(wrapper!.text()).not.toContain('Beta')
  })

  it('updates the gate row after claim and approve succeed (readonly vue-query data fix, FAR-630)', async () => {
    // claimGate()/approveGate() patch gates by replacing the whole array
    // through the writable computed (vue-query data is deep-readonly, so
    // `gates.value[idx] = ...` would be silently dropped), so the row badge
    // reflects the new decision immediately.
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({
      data: {
        gates: [
          {
            run_id: '550e8400-e29b-41d4-a716-446655440000',
            gate_id: 'approval-gate-1',
            pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
            claimed_by: null,
            claimed_at: null,
            expires_at: null,
            decision: null,
            decision_at: null,
            created_at: '2025-06-30T10:00:00Z',
          },
        ],
      },
      error: undefined,
    })
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url.endsWith('/claim')) {
        return Promise.resolve({ data: { claim_token: 'tok-1', expires_at: '2025-06-30T10:15:00Z' }, error: undefined })
      }
      return Promise.resolve({ data: { ok: true }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper!.text()).toContain('pending')

    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()
    await wrapper!.find('[data-testid="hitl-review-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    const claimedBadge = wrapper!.findAll('span').find((s) => s.classes().includes('badge'))
    expect(claimedBadge?.text()).toBe('claimed')

    await wrapper!.find('[data-testid="hitl-review-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    const approvedBadge = wrapper!.findAll('span').find((s) => s.classes().includes('badge'))
    expect(approvedBadge?.text()).toBe('approved')
  })

  it('maps a run-not-awaiting 409 to a specific view-level banner and re-fetches the list', async () => {
    const { api } = await import('../lib/api/client')
    const gates = [pendingGateRow()]
    ;(api.GET as any).mockImplementation(mockGetWithGates(gates))
    ;(api.POST as any).mockResolvedValue({
      data: null,
      error: problemDetail('Run 550e8400-e29b-41d4-a716-446655440000 is not awaiting a human decision (status: complete)'),
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    const pendingCallsBefore = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    const claimButton = wrapper!.find('[data-testid="hitl-review-claim"]')
    expect(claimButton.exists()).toBe(true)
    await claimButton.trigger('click')
    await flushPromises()
    await nextTick()

    // The failure message lives in the VIEW-LEVEL banner, not in the gate
    // row: the immediate refresh drops terminal-run gates from the list, so
    // a row-level message would be erased before it renders (FAR-612).
    const banner = wrapper!.find('[data-testid="hitl-review-claim-failure-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('no longer waiting for a human decision')
    expect(banner.text()).toContain('status: complete')
    const pendingCallsAfter = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    expect(pendingCallsAfter).toBe(pendingCallsBefore + 1)
  })

  it('maps an already-claimed 409 to a specific view-level banner', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation(mockGetWithGates([pendingGateRow()]))
    ;(api.POST as any).mockResolvedValue({
      data: null,
      error: problemDetail("Gate 'approval-gate-1' on run 550e8400-e29b-41d4-a716-446655440000 is already claimed"),
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    await wrapper!.find('[data-testid="hitl-review-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    const banner = wrapper!.find('[data-testid="hitl-review-claim-failure-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('already claimed by another reviewer')
  })

  it('re-fetches the list even when the claim throws a network error', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation(mockGetWithGates([pendingGateRow()]))
    ;(api.POST as any).mockRejectedValue(new Error('network down'))

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    const pendingCallsBefore = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    await wrapper!.find('[data-testid="hitl-review-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    // Network errors land in the catch path: the row on screen may be stale
    // (the claim may have landed before the connection dropped), so the
    // refresh must fire here too ÔÇö not only for API-error failures (FAR-612).
    const pendingCallsAfter = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    expect(pendingCallsAfter).toBe(pendingCallsBefore + 1)
    const banner = wrapper!.find('[data-testid="hitl-review-claim-failure-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('network down')
  })

  it('renders a claimed-by-another gate as read-only with no approve/reject buttons', async () => {
    const { api } = await import('../lib/api/client')
    const claimedByOther = {
      ...pendingGateRow(),
      claimed_by: '999e8400-e29b-41d4-a716-446655440009',
      claimed_at: '2025-06-30T11:00:00Z',
      expires_at: '2025-06-30T11:15:00Z',
    }
    ;(api.GET as any).mockImplementation(mockGetWithGates([claimedByOther]))

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    expect(wrapper!.find('[data-testid="hitl-review-claimed-other"]').exists()).toBe(true)
    expect(wrapper!.text()).toContain('Claimed by')
    expect(wrapper!.text()).toContain('999e8400-e29b-41d4-a716-446655440009')
    expect(wrapper!.find('[data-testid="hitl-review-approve"]').exists()).toBe(false)
    expect(wrapper!.find('[data-testid="hitl-review-reject"]').exists()).toBe(false)
  })

  it('renders approve/reject buttons after this session claims the gate', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation(mockGetWithGates([pendingGateRow()]))
    ;(api.POST as any).mockResolvedValue({
      data: {
        run_id: '550e8400-e29b-41d4-a716-446655440000',
        gate_id: 'approval-gate-1',
        claim_token: 'tok-123',
        expires_at: '2025-06-30T10:15:00Z',
      },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    await wrapper!.find('[data-testid="hitl-review-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper!.find('[data-testid="hitl-review-approve"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-review-reject"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-review-claim"]').exists()).toBe(false)
  })
  it('shows the decision briefing in the expanded panel (FAR-613)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/hitl/pending') {
        return Promise.resolve({
          data: {
            gates: [
              {
                run_id: '550e8400-e29b-41d4-a716-446655440000',
                gate_id: 'approval-gate-1',
                pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
                claimed_by: null,
                claimed_at: null,
                expires_at: null,
                decision: null,
                decision_at: null,
                created_at: '2025-06-30T10:00:00Z',
                description: 'Approve only when the generated comments are accurate and safe to post.',
                context: {
                  trigger: 'condition',
                  condition: "node_id=='550e8400-e29b-41d4-a716-446655440000'",
                  source_node_id: '550e8400-e29b-41d4-a716-446655440000',
                  source_node_label: 'Comment Generator',
                  artifacts: [{ node_id: '550e8400-e29b-41d4-a716-446655440000', summary: '{"ok":true}' }],
                  pipeline_name: 'PR Reviewer',
                },
              },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()

    const briefing = wrapper!.find('[data-testid="hitl-briefing"]')
    expect(briefing.exists()).toBe(true)
    expect(briefing.text()).toContain('Approve only when the generated comments are accurate and safe to post.')
    // The briefing sits above the approve/reject controls in the actions column.
    const actionsColumn = briefing.element.parentElement
    expect(actionsColumn?.textContent).toContain('Approve')
    // Details collapsed until toggled.
    expect(wrapper!.find('[data-testid="hitl-briefing-details"]').exists()).toBe(false)
    await wrapper!.find('[data-testid="hitl-briefing-toggle"]').trigger('click')
    expect(wrapper!.find('[data-testid="hitl-briefing-details"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-briefing-details"]').text()).toContain('Comment Generator')
  })

  it('renders the muted no-description fallback for a legacy gate (FAR-613)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({
      data: {
        gates: [
          {
            run_id: '550e8400-e29b-41d4-a716-446655440000',
            gate_id: 'approval-gate-1',
            pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
            claimed_by: null,
            claimed_at: null,
            expires_at: null,
            decision: null,
            decision_at: null,
            created_at: '2025-06-30T10:00:00Z',
            description: null,
            context: null,
          },
        ],
      },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await flushPromises()
    await nextTick()
    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()

    const fallback = wrapper!.find('[data-testid="hitl-briefing-description-fallback"]')
    expect(fallback.exists()).toBe(true)
    expect(fallback.text()).toContain('No description provided for this gate')
    // The gate stays claimable ÔÇö the legacy briefing never breaks the flow.
    expect(wrapper!.find('[data-testid="hitl-review-claim"]').exists()).toBe(true)
  })
})
