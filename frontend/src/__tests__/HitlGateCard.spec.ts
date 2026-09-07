import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import HitlGateCard from '../components/hitl/HitlGateCard.vue'
import type { HitlGate } from '../components/hitl/HitlGateCard.vue'
import { resetHitlGateState } from '../composables/useHitlGateState'

function gate(overrides: Partial<HitlGate> = {}): HitlGate {
  return {
    run_id: '550e8400-e29b-41d4-a716-446655440000',
    gate_id: 'approval-gate-1',
    pipeline_id: '660e8400-e29b-41d4-a716-446655440001',
    pipeline_name: 'Reviewer Pipeline',
    label: 'Review the deploy plan',
    claimed_by: null,
    claimed_at: null,
    expires_at: null,
    decision: null,
    decision_at: null,
    ...overrides,
  }
}

describe('HitlGateCard', () => {
  let wrapper: VueWrapper | null = null

  beforeEach(() => {
    vi.clearAllMocks()
    // Module-scoped gate state outlives component instances by design — each
    // test must start from a fresh browser session.
    resetHitlGateState()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
  })

  it('shows the claim button for a pending gate', () => {
    wrapper = mount(HitlGateCard, { props: { gate: gate() }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })
    expect(wrapper.find('[data-testid="hitl-gate-claim"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="hitl-gate-reclaim"]').exists()).toBe(false)
  })

  it('stores the claim token after a successful claim and reveals approve/reject', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({
      data: { claim_token: 'tok-1', expires_at: '2025-06-30T10:20:00Z' },
      error: undefined,
    })
    wrapper = mount(HitlGateCard, { props: { gate: gate() }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })

    await wrapper.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/claim')
    expect(post).toBeTruthy()
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: gate().run_id, gate_id: 'approval-gate-1' } },
      body: { expiry_minutes: 15 },
    })

    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-reject"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-notes"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-claim-token"]').text()).toContain('tok-1')

    const claimedEvents = wrapper.emitted('claimed')
    expect(claimedEvents).toHaveLength(1)
    expect(claimedEvents![0][0]).toMatchObject({ type: 'success' })
  })

  it('offers re-claim for a claimed gate without a token and surfaces a 409 conflict', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({
      data: null,
      error: { detail: 'gate_already_claimed' },
    })
    wrapper = mount(HitlGateCard, { props: { gate: gate({ claimed_by: 'other@team' }) }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })

    // Another reviewer holds the claim: no fresh claim, only the recovery path.
    expect(wrapper.find('[data-testid="hitl-gate-claim"]').exists()).toBe(false)
    const reclaim = wrapper.find('[data-testid="hitl-gate-reclaim"]')
    expect(reclaim.exists()).toBe(true)

    await reclaim.trigger('click')
    await flushPromises()

    const message = wrapper.find('[data-testid="hitl-gate-message"]')
    expect(message.exists()).toBe(true)
    expect(message.text()).toContain('Claim failed:')
    expect(message.text()).toContain('gate_already_claimed')
    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(false)
  })

  it('approves with the stored token and notes, emitting a success decided payload', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url.endsWith('/claim')) {
        return Promise.resolve({ data: { claim_token: 'tok-1', expires_at: '2025-06-30T10:20:00Z' }, error: undefined })
      }
      return Promise.resolve({ data: { ok: true }, error: undefined })
    })
    wrapper = mount(HitlGateCard, { props: { gate: gate() }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })

    await wrapper.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="hitl-gate-notes"]').setValue('looks good')
    await wrapper.find('[data-testid="hitl-gate-approve"]').trigger('click')
    await flushPromises()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/approve')
    expect(post).toBeTruthy()
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: gate().run_id, gate_id: 'approval-gate-1' } },
      body: { claim_token: 'tok-1', notes: 'looks good' },
    })

    const decidedEvents = wrapper.emitted('decided')
    expect(decidedEvents).toHaveLength(1)
    expect(decidedEvents![0][0]).toMatchObject({ type: 'success', text: 'Gate approved. Pipeline resuming.' })
  })

  it('rejects with the stored token and a default reason, emitting a decided payload', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockImplementation((url: string) => {
      if (url.endsWith('/claim')) {
        return Promise.resolve({ data: { claim_token: 'tok-1', expires_at: '2025-06-30T10:20:00Z' }, error: undefined })
      }
      return Promise.resolve({ data: { ok: true }, error: undefined })
    })
    wrapper = mount(HitlGateCard, { props: { gate: gate() }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })

    await wrapper.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="hitl-gate-reject"]').trigger('click')
    await flushPromises()

    const post = (api.POST as any).mock.calls.find((c: unknown[]) => c[0] === '/api/v1/runs/{run_id}/hitl/{gate_id}/reject')
    expect(post).toBeTruthy()
    expect((post as unknown[])[1]).toEqual({
      params: { path: { run_id: gate().run_id, gate_id: 'approval-gate-1' } },
      body: { claim_token: 'tok-1', reason: 'Rejected by reviewer' },
    })

    const decidedEvents = wrapper.emitted('decided')
    expect(decidedEvents).toHaveLength(1)
    expect(decidedEvents![0][0]).toMatchObject({ type: 'success' })
  })

  it('renders the approved decision banner for an approved gate', () => {
    wrapper = mount(HitlGateCard, { props: { gate: gate({ decision: 'approved', decision_at: '2025-06-30T11:00:00Z' }) }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })
    expect(wrapper.text()).toContain('Gate was approved. The pipeline has resumed.')
    expect(wrapper.find('[data-testid="hitl-gate-claim"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(false)
  })

  it('renders the rejected decision banner for a rejected gate', () => {
    wrapper = mount(HitlGateCard, { props: { gate: gate({ decision: 'rejected', decision_at: '2025-06-30T11:00:00Z' }) }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })
    expect(wrapper.text()).toContain('Gate was rejected. The pipeline was routed to the reject target.')
    expect(wrapper.find('[data-testid="hitl-gate-reject"]').exists()).toBe(false)
  })

  it('omits the run link by default and renders it with showRunLink', async () => {
    wrapper = mount(HitlGateCard, { props: { gate: gate() }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })
    expect(wrapper.find('[data-testid="hitl-gate-run-link"]').exists()).toBe(false)
    wrapper.unmount()

    wrapper = mount(HitlGateCard, { props: { gate: gate(), showRunLink: true }, global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } } })
    const runLink = wrapper.find('[data-testid="hitl-gate-run-link"]')
    expect(runLink.exists()).toBe(true)
    expect(runLink.attributes('href')).toBe('/runs/550e8400-e29b-41d4-a716-446655440000')
  })

  it('restores the claim token and notes when the card remounts (FAR-686 regression)', async () => {
    // The review page's 30s auto-refresh unmounts the list branch: the
    // remounted card must come back with approve/reject (not re-claim) and
    // the reviewer's typed notes intact.
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({
      data: { claim_token: 'tok-persist', expires_at: '2025-06-30T10:20:00Z' },
      error: undefined,
    })
    const mountOptions = (gateProps: HitlGate) => ({
      props: { gate: gateProps },
      global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    })

    wrapper = mount(HitlGateCard, mountOptions(gate()))
    await wrapper.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    await wrapper.find('[data-testid="hitl-gate-notes"]').setValue('notes typed before unmount')

    const firstInstance = wrapper
    wrapper = null
    firstInstance.unmount()

    // Remount as the auto-refresh would: same gate, now claimed_by on the server.
    wrapper = mount(HitlGateCard, mountOptions(gate({ claimed_by: 'reviewer@team' })))
    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-reject"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-reclaim"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="hitl-gate-claim-token"]').text()).toContain('tok-persist')

    const restoredNotes = wrapper.find('[data-testid="hitl-gate-notes"]')
    expect((restoredNotes.element as HTMLTextAreaElement).value).toBe('notes typed before unmount')
  })

  it('drops a stale persisted token when the server reports the gate as pending again', async () => {
    // A pending gate has no live claim server-side: any persisted token is
    // stale and must not resurrect approve/reject on remount.
    const { api } = await import('../lib/api/client')
    ;(api.POST as any).mockResolvedValue({
      data: { claim_token: 'tok-stale', expires_at: '2025-06-30T10:20:00Z' },
      error: undefined,
    })
    const mountOptions = {
      props: { gate: gate() },
      global: { stubs: { RouterLink: { template: '<a :href="to"><slot /></a>', props: ['to'] } } },
    }

    wrapper = mount(HitlGateCard, mountOptions)
    await wrapper.find('[data-testid="hitl-gate-claim"]').trigger('click')
    await flushPromises()
    wrapper.unmount()
    wrapper = null

    wrapper = mount(HitlGateCard, mountOptions)
    expect(wrapper.find('[data-testid="hitl-gate-claim"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="hitl-gate-approve"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="hitl-gate-reclaim"]').exists()).toBe(false)
  })
})
