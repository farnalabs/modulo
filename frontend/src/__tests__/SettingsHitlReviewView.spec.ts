import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({ data: { gates: [] }, error: undefined })

    const wrapper = mount(SettingsHitlReviewView)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('HITL Review')
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsHitlReviewView)
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
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

    const wrapper = mount(SettingsHitlReviewView)
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('approval-gate-1')
    expect(wrapper.text()).toContain('pending')
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

    const wrapper = mount(SettingsHitlReviewView)
    await nextTick()
    await nextTick()
    await nextTick()

    const toggle = wrapper.find('[data-testid="hitl-review-toggle-expand"]')
    expect(toggle.exists()).toBe(true)
    await toggle.trigger('click')
    await nextTick()

    expect(wrapper.text()).toContain('Claim Gate')
    expect(wrapper.text()).toContain('Claim Metadata')
  })
})
