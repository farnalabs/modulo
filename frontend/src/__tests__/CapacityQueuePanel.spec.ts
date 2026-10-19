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
import { formatCapacityDuration, useAnalyticsStore } from '../stores/analytics'

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
    expect(wrapper.find('[data-testid="capacity-stall-alert"]').classes()).toContain('text-destructive-text')
    const verdict = wrapper.find('[data-testid="capacity-verdict"]')
    expect(verdict.text()).toContain('Stalled')
    expect(verdict.classes()).toContain('text-destructive-text')
  })

  it('scopes the request to the selected pipeline', async () => {
    installResponse(capacityResponse())
    const wrapper = mountPanel()
    const store = useAnalyticsStore()
    store.filters.pipelineId = 'aaaaaaaa-1111-4111-8111-111111111111'
    await flushPromises()

    const lastCall = mockGet.mock.calls[mockGet.mock.calls.length - 1]
    expect(lastCall[1].params.query.pipeline_id).toEqual(['aaaaaaaa-1111-4111-8111-111111111111'])
    wrapper.unmount()
  })

  it('surfaces a fetch failure without crashing', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: { detail: 'Capacity unavailable' } })
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="capacity-error"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="capacity-error"]').text()).toContain('Capacity unavailable')
    expect(wrapper.find('[data-testid="capacity-error"]').classes()).toContain('text-destructive-text')
  })

  it('renders neutral labels when optional fields are absent', async () => {
    const payload = capacityResponse({
      generated_at: 'not-a-date',
      limits: { org_run_concurrency_limit: null, sandbox_concurrency_limit: null, sandbox_limit_is_default: false },
      drain: {
        created: 0,
        started: 0,
        completed: 0,
        ratio: null,
        baseline_depth: 0,
        baseline_oldest_pending_age_seconds: null,
        final_depth: 0,
        depth_change: 0,
        verdict: 'stable',
      },
      binding: { scope: 'none', limit: null, in_use: null, headroom: null, pending_blocked: 0, admissible_if_raised: 0 },
      pending: {
        total: 1,
        oldest_pending_age_seconds: null,
        by_cause: { pipeline: 1 },
        by_pipeline: [
          {
            pipeline_id: 'bbbbbbbb-2222-4222-8222-222222222222',
            pipeline_name: null,
            pending_count: 1,
            oldest_pending_age_seconds: null,
            max_concurrent_runs: null,
          },
        ],
      },
      active_health: { ...capacityResponse().active_health, utilisation: null, attribution: [] },
    })
    installResponse(payload)
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="capacity-verdict"]').text()).toContain('Stable')
    expect(wrapper.find('[data-testid="capacity-oldest-pending"]').text()).toBe('—')
    expect(wrapper.find('[data-testid="capacity-drain-ratio"]').text()).toBe('—')
    expect(wrapper.find('[data-testid="capacity-org-cap-use"]').text()).toBe('—')
    expect(wrapper.find('[data-testid="capacity-limits"]').text()).toContain('None')
    expect(wrapper.find('[data-testid="capacity-binding"]').text()).toContain('None')
    // No pipeline_name -> the short id (first 8 chars) is shown.
    expect(wrapper.text()).toContain('bbbbbbbb')
    // Missing by_cause keys default to zero rather than undefined.
    expect(wrapper.find('[data-testid="capacity-cause-unmarked"]').text()).toBe('0')
  })

  it('falls back to the stable label for an unknown verdict', async () => {
    const base = capacityResponse()
    installResponse(capacityResponse({ drain: { ...base.drain, verdict: 'mystery' } }))
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="capacity-verdict"]').text()).toContain('Stable')
  })

  it('falls back to the stable label for a missing verdict', async () => {
    const base = capacityResponse()
    installResponse(
      capacityResponse({ drain: { ...base.drain, verdict: undefined as unknown as string } }),
    )
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="capacity-verdict"]').text()).toContain('Stable')
  })

  it('renders the default and unknown-binding labels', async () => {
    const base = capacityResponse()
    installResponse(
      capacityResponse({
        limits: { org_run_concurrency_limit: 5, sandbox_concurrency_limit: 4, sandbox_limit_is_default: true },
        binding: { ...base.binding, scope: 'mystery' },
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.find('[data-testid="capacity-limits"]').text()).toContain('4 (default)')
    expect(wrapper.find('[data-testid="capacity-binding"]').text()).toContain('None')
  })

  it('refetches the capacity report when refresh is clicked', async () => {
    installResponse(capacityResponse())
    const wrapper = mountPanel()
    await flushPromises()
    const callsBefore = mockGet.mock.calls.length

    await wrapper.find('[data-testid="capacity-refresh"]').trigger('click')
    await flushPromises()

    expect(mockGet.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('ignores a stale capacity failure superseded by a newer request', async () => {
    setActivePinia(createPinia())
    const store = useAnalyticsStore()
    let rejectFirst: (reason: unknown) => void = () => {}
    const first = new Promise((_resolve, reject) => {
      rejectFirst = reject
    })
    mockGet
      .mockImplementationOnce(() => first)
      .mockImplementationOnce(() => Promise.resolve({ data: capacityResponse(), error: undefined }))

    const stale = store.fetchCapacity()
    const fresh = store.fetchCapacity()
    rejectFirst(new Error('stale request failed'))

    await Promise.allSettled([stale, fresh])
    expect(store.capacityError).toBeNull()
    expect(store.capacity?.pending.total).toBe(3)
  })

  it('formats capacity durations across every magnitude', () => {
    expect(formatCapacityDuration(null)).toBe('—')
    expect(formatCapacityDuration(undefined)).toBe('—')
    expect(formatCapacityDuration(7200)).toBe('2.0h')
    expect(formatCapacityDuration(120)).toBe('2m')
    expect(formatCapacityDuration(30)).toBe('30s')
  })

  it('uses empty capacity defaults before the store has loaded', () => {
    setActivePinia(createPinia())
    mockGet.mockImplementation(() => new Promise(() => {}))
    const wrapper = mountPanel()
    // The panel keeps an empty stand-in until the first fetch resolves. The
    // setup-state computed may be exposed either wrapped or unwrapped.
    const setupState = (wrapper.vm as unknown as {
      $: { setupState: { capacity: { drain: { verdict: string }; value?: { drain: { verdict: string } } } } }
    }).$.setupState
    const capacity = setupState.capacity.value ?? setupState.capacity
    expect(capacity.drain.verdict).toBe('stable')
    wrapper.unmount()
  })
})
