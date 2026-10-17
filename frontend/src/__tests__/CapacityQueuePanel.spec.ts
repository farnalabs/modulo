import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

import CapacityQueuePanel from '../components/analytics/CapacityQueuePanel.vue'
import enUS from '../locales/en-US.js'
import { useAnalyticsStore } from '../stores/analytics'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: { 'en-US': enUS },
})

function capacityResponse(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: '2026-10-17T12:00:00+00:00',
    window_start: '2026-10-17T11:00:00+00:00',
    window_seconds: 3600,
    interval_seconds: 300,
    limits: {
      org_run_concurrency_limit: 5,
      sandbox_concurrency_limit: 4,
      sandbox_limit_is_default: false,
    },
    pending: {
      total: 3,
      oldest_pending_age_seconds: 1800,
      by_cause: { pipeline: 1, org_run: 1, sandbox: 1, unmarked: 0 },
      by_pipeline: [
        {
          pipeline_id: 'aaaaaaaa-1111-4111-8111-111111111111',
          pipeline_name: 'alpha',
          pending_count: 3,
          oldest_pending_age_seconds: 1800,
          max_concurrent_runs: 2,
        },
      ],
    },
    drain: {
      created: 10,
      started: 10,
      completed: 9,
      ratio: 1,
      baseline_depth: 4,
      baseline_oldest_pending_age_seconds: 200,
      final_depth: 3,
      depth_change: -1,
      verdict: 'draining',
    },
    binding: {
      scope: 'pipeline',
      limit: 2,
      in_use: 2,
      headroom: 0,
      pending_blocked: 1,
      admissible_if_raised: 1,
    },
    queue_if_raised: { pending_now: 3, admissible_if_binding_raised: 1, pending_after_raise: 2 },
    active_health: {
      org_run_limit: 5,
      gate_total: 5,
      executing_total: 5,
      running_total: 5,
      parked_total: 0,
      cancelling_total: 0,
      utilisation: 1,
      cap_held: true,
      completions_in_window: 9,
      zero_completion_intervals: 0,
      stall_intervals: 3,
      stall_suspected: false,
      stale_heartbeat_threshold_seconds: 1800,
      stale_heartbeat_count: 1,
      attribution: [
        {
          pipeline_id: 'aaaaaaaa-1111-4111-8111-111111111111',
          pipeline_name: 'alpha',
          executing: 5,
          running: 5,
          parked: 0,
          oldest_executing_age_seconds: 300,
          stale_heartbeat_count: 1,
        },
      ],
    },
    series: [
      {
        interval_start: '2026-10-17T11:00:00+00:00',
        interval_end: '2026-10-17T11:05:00+00:00',
        created: 2,
        started: 2,
        completed: 1,
        pending_depth: 3,
        oldest_pending_age_seconds: 300,
        drain_ratio: 1,
        complete: true,
        by_pipeline: [],
      },
    ],
    ...overrides,
  }
}

function installResponse(payload: unknown) {
  mockGet.mockResolvedValue({ data: payload, error: undefined })
}

function mountPanel() {
  setActivePinia(createPinia())
  return mount(CapacityQueuePanel, { global: { plugins: [i18n] } })
}

beforeEach(() => {
  mockGet.mockReset()
})

describe('CapacityQueuePanel', () => {
  it('fetches the capacity endpoint and renders the queue summary', async () => {
    installResponse(capacityResponse())
    const wrapper = mountPanel()
    await flushPromises()

    expect(mockGet).toHaveBeenCalled()
    const [url] = mockGet.mock.calls[0]
    expect(url).toBe('/api/v1/analytics/capacity')
    expect(wrapper.find('[data-testid="capacity-pending-total"]').text()).toBe('3')
    expect(wrapper.find('[data-testid="capacity-verdict"]').text()).toContain('Draining')
    expect(wrapper.find('[data-testid="capacity-cause-pipeline"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="capacity-cause-sandbox"]').text()).toBe('1')
    expect(wrapper.find('[data-testid="capacity-executing-total"]').text()).toBe('5')
  })

  it('renders the stall alert when the leak signature is present', async () => {
    const payload = capacityResponse()
    payload.active_health = {
      ...payload.active_health,
      stall_suspected: true,
      zero_completion_intervals: 4,
    }
    payload.drain = { ...payload.drain, verdict: 'stalled' }
    installResponse(payload)
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="capacity-stall-alert"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="capacity-verdict"]').text()).toContain('Stalled')
  })

  it('scopes the request to the selected pipeline', async () => {
    installResponse(capacityResponse())
    const wrapper = mountPanel()
    const store = useAnalyticsStore()
    store.filters.pipelineId = 'aaaaaaaa-1111-4111-8111-111111111111'
    await flushPromises()

    const lastCall = mockGet.mock.calls[mockGet.mock.calls.length - 1]
    expect(lastCall[1].params.query.pipeline_id).toBe('aaaaaaaa-1111-4111-8111-111111111111')
    wrapper.unmount()
  })

  it('surfaces a fetch failure without crashing', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: { detail: 'Capacity unavailable' } })
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="capacity-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="capacity-error"]').text()).toContain('Capacity unavailable')
  })
})
