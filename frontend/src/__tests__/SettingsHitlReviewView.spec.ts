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
import FilterBar from '../components/shared/FilterBar.vue'

const PENDING_GATE = {
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

function claimedGate(overrides: Record<string, unknown> = {}) {
  return {
    ...PENDING_GATE,
    claimed_by: 'reviewer@team',
    claimed_at: '2025-06-30T10:05:00Z',
    ...overrides,
  }
}

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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await nextTick()
    expect(wrapper!.find('.animate-spin').exists()).toBe(true)
  })

  it('renders gates list', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({
      data: { gates: [PENDING_GATE] },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper!.text()).toContain('#approval')
    expect(wrapper!.text()).toContain('pending')
  })

  it('expands gate detail panel on click', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({
      data: { gates: [PENDING_GATE] },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()

    const toggle = wrapper!.find('[data-testid="hitl-review-toggle-expand"]')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')
    await nextTick()

    expect(wrapper!.text()).toContain('Claim Gate')
    expect(wrapper!.text()).toContain('Run ID')
  })

  it('does not re-fetch gates when typing in the search box (client-side filtering)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/hitl/pending') {
        return Promise.resolve({
          data: {
            gates: [
              PENDING_GATE,
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
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

  it('drives the full claim -> approve lifecycle through the shared gate card (FAR-686)', async () => {
    // The shared card owns the claim token: claiming immediately reveals
    // approve/reject (no page reload needed), and a decision re-fetches the
    // pending list, so the decided gate leaves the PENDING review page.
    const { api } = await import('../lib/api/client')
    let serverGates: Record<string, unknown>[] = [{ ...PENDING_GATE }]
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/hitl/pending') {
        return Promise.resolve({ data: { gates: serverGates }, error: undefined })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url.endsWith('/claim')) {
        serverGates = serverGates.map((g) => ({ ...g, claimed_by: 'reviewer@team', claimed_at: '2025-06-30T10:05:00Z' }))
        return Promise.resolve({ data: { claim_token: 'tok-1', expires_at: '2025-06-30T10:20:00Z' }, error: undefined })
      }
      if (url.endsWith('/approve')) {
        serverGates = []
        return Promise.resolve({ data: { ok: true }, error: undefined })
      }
      return Promise.resolve({ data: { ok: true }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper!.text()).toContain('pending')

    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()
    await wrapper!.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    // The CARD's own badge flips to claimed immediately (the row badge
    // converges on the next auto-refresh — a refetch here would unmount the
    // list branch and destroy the card's in-session token).
    const badges = wrapper!.findAll('span.badge').map((s) => s.text())
    expect(badges).toContain('claimed')
    expect(wrapper!.find('[data-testid="hitl-gate-approve"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-gate-reject"]').exists()).toBe(true)

    await wrapper!.find('[data-testid="hitl-gate-approve"]').trigger('click')
    await flushPromises()
    await nextTick()

    // A decided gate is no longer pending work: the re-fetch drops the row and
    // the empty state takes over.
    expect(wrapper!.text()).toContain('No pending HITL gates')
    expect(wrapper!.text()).not.toContain('#approval')
  })

  it('keeps a claimed gate listed after a refresh and recovers via re-claim (FAR-686 regression)', async () => {
    // THE reported bug: claimed gates used to vanish from the review page
    // (the backend pending list excluded them), leaving no approve/reject
    // path after a reload. The claimed gate must stay listed and the card
    // must offer the token-recovery (re-claim) path.
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({
      data: { gates: [claimedGate()] },
      error: undefined,
    })
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url.endsWith('/claim')) {
        return Promise.resolve({ data: { claim_token: 'tok-2', expires_at: '2025-06-30T10:20:00Z' }, error: undefined })
      }
      return Promise.resolve({ data: { ok: true }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper!.findAll('span').find((s) => s.classes().includes('badge'))
    expect(badge?.text()).toBe('claimed')
    expect(wrapper!.text()).toContain('Assigned: reviewer@team')

    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()

    // No token in this browser session (page was reloaded): re-claim, not claim.
    expect(wrapper!.find('[data-testid="hitl-gate-claim"]').exists()).toBe(false)
    const reclaim = wrapper!.find('[data-testid="hitl-gate-reclaim"]')
    expect(reclaim.exists()).toBe(true)

    await reclaim.trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper!.find('[data-testid="hitl-gate-approve"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-gate-reject"]').exists()).toBe(true)
  })

  it('status filter "claimed" matches claimed gates served by the API (FAR-686)', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((url: string) => {
      if (url === '/api/v1/hitl/pending') {
        return Promise.resolve({
          data: {
            gates: [
              claimedGate(),
              {
                ...PENDING_GATE,
                run_id: '550e8400-e29b-41d4-a716-446655440002',
                gate_id: 'deploy-gate-1',
                pipeline_id: '660e8400-e29b-41d4-a716-446655440003',
              },
            ],
          },
          error: undefined,
        })
      }
      return Promise.resolve({ data: { items: [] }, error: undefined })
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    expect(wrapper!.text()).toContain('#approval')
    expect(wrapper!.text()).toContain('#deploy-g')

    const pendingCallsBefore = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/hitl/pending').length
    wrapper!.findComponent(FilterBar).vm.$emit('update:filter', 'status', 'claimed')
    await flushPromises()
    await nextTick()

    // The filter change re-queries the server, which now serves claimed gates.
    const pendingCallsAfter = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === '/api/v1/hitl/pending').length
    expect(pendingCallsAfter).toBe(pendingCallsBefore + 1)
    expect(wrapper!.text()).toContain('#approval')
    expect(wrapper!.text()).not.toContain('#deploy-g')
  })

  it('renders the run link inside the expanded gate card', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({
      data: { gates: [PENDING_GATE] },
      error: undefined,
    })

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()

    await wrapper!.find('[data-testid="hitl-review-toggle-expand"]').trigger('click')
    await nextTick()

    const runLink = wrapper!.find('[data-testid="hitl-gate-run-link"]')
    expect(runLink.exists()).toBe(true)
    expect(runLink.attributes('href')).toBe('/runs/550e8400-e29b-41d4-a716-446655440000')
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    const pendingCallsBefore = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    // FAR-686: the claim button lives in the shared gate card.
    const claimButton = wrapper!.find('[data-testid="hitl-gate-claim"]')
    expect(claimButton.exists()).toBe(true)
    await claimButton.trigger('click')
    await flushPromises()
    await nextTick()

    // The failure message lives in the VIEW-LEVEL banner, not in the gate
    // row: the immediate refresh drops terminal-run gates from the list, so
    // a row-level message would be erased before it renders (FAR-612,
    // composed with the shared card via the claim-failed emit).
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    await wrapper!.find('[data-testid="hitl-gate-claim"]').trigger('click')
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    const pendingCallsBefore = (api.GET as any).mock.calls.filter((c: unknown[]) => c[0] === PENDING_URL).length
    await wrapper!.find('[data-testid="hitl-gate-claim"]').trigger('click')
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

  it('renders a claimed-by-another gate with a re-claim path and no approve/reject buttons', async () => {
    const { api } = await import('../lib/api/client')
    const claimedByOther = {
      ...pendingGateRow(),
      claimed_by: '999e8400-e29b-41d4-a716-446655440009',
      claimed_at: '2025-06-30T11:00:00Z',
      expires_at: '2025-06-30T11:15:00Z',
    }
    ;(api.GET as any).mockImplementation(mockGetWithGates([claimedByOther]))

    wrapper = mount(SettingsHitlReviewView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    expect(wrapper!.text()).toContain('Claimed by')
    expect(wrapper!.text()).toContain('999e8400-e29b-41d4-a716-446655440009')
    expect(wrapper!.find('[data-testid="hitl-gate-approve"]').exists()).toBe(false)
    expect(wrapper!.find('[data-testid="hitl-gate-reject"]').exists()).toBe(false)
    // FAR-686: the same-account re-claim path replaces the old read-only
    // notice — attempting re-claim is allowed and fails loudly (409 banner)
    // when another reviewer holds the gate.
    expect(wrapper!.find('[data-testid="hitl-gate-reclaim"]').exists()).toBe(true)
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
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' }, RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })
    await flushPromises()
    await nextTick()
    await expandFirstGate(wrapper!)

    await wrapper!.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(wrapper!.find('[data-testid="hitl-gate-approve"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-gate-reject"]').exists()).toBe(true)
    expect(wrapper!.find('[data-testid="hitl-gate-claim"]').exists()).toBe(false)
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
