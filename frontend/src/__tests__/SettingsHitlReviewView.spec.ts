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
})
