import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import RunAttemptHistory from '../components/runs/RunAttemptHistory.vue'

const apiMocks = vi.hoisted(() => ({
  attempts: vi.fn(),
}))

vi.mock('@/lib/api/client', () => ({
  api: {
    GET: vi.fn((url: string) => {
      if (url === '/api/v1/runs/{run_id}/attempts') return apiMocks.attempts()
      return Promise.resolve({ data: null, error: undefined })
    }),
  },
}))

function attempt(overrides: Record<string, unknown> = {}) {
  return {
    attempt_index: 0,
    started_at: '2026-10-24T10:00:00Z',
    ended_at: '2026-10-24T10:02:30Z',
    terminal_reason: 'completed',
    model_used: 'openai/gpt-4o',
    error_code: null,
    ...overrides,
  }
}

async function mountWith(items: Array<Record<string, unknown>>, total = items.length) {
  apiMocks.attempts.mockResolvedValue({ data: { items, total }, error: undefined })
  const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  apiMocks.attempts.mockReset()
})

describe('RunAttemptHistory (FAR-2078)', () => {
  it('renders every attempt in order with the terminal reason translated', async () => {
    const wrapper = await mountWith([
      attempt({ attempt_index: 0, terminal_reason: 'guard_cap', ended_at: '2026-10-24T10:01:00Z' }),
      attempt({ attempt_index: 1, terminal_reason: 'completed', ended_at: '2026-10-24T10:05:00Z' }),
    ])

    const rows = wrapper.findAll('[data-testid="run-attempt-history-row"]')
    expect(rows).toHaveLength(2)
    expect(rows[0].find('th').text()).toBe('1')
    expect(rows[1].find('th').text()).toBe('2')
    expect(rows[0].text()).toContain('Retry limit reached')
    expect(rows[1].text()).toContain('Completed')
    // The raw wire enum must never be the rendered copy.
    expect(rows[0].text()).not.toContain('guard_cap')
  })

  it('renders the start time as accessible text with a machine-readable datetime', async () => {
    const wrapper = await mountWith([attempt()])

    const time = wrapper.get('time')
    expect(time.attributes('datetime')).toBe('2026-10-24T10:00:00Z')
    // Human-readable, not a bare ISO string.
    expect(time.text()).not.toContain('T10:00:00Z')
    expect(time.text().length).toBeGreaterThan(0)
  })

  it('shows an open attempt as running instead of inventing an end state', async () => {
    const wrapper = await mountWith([
      attempt({ ended_at: null, terminal_reason: null, error_code: null }),
    ])

    const row = wrapper.get('[data-testid="run-attempt-history-row"]')
    expect(row.text()).toContain('Running')
    // No terminal reason has been recorded because the attempt has not ended.
    expect(row.text()).toContain('—')
  })

  it('shows the model and error code when the attempt carries them', async () => {
    const wrapper = await mountWith([
      attempt({ model_used: 'anthropic/claude-sonnet', error_code: 'provider.rate_limited' }),
    ])

    const row = wrapper.get('[data-testid="run-attempt-history-row"]')
    expect(row.text()).toContain('anthropic/claude-sonnet')
    expect(row.text()).toContain('provider.rate_limited')
  })

  it('renders an unrecognised terminal reason as translated "Unknown", never the raw value', async () => {
    const wrapper = await mountWith([attempt({ terminal_reason: 'mystery_reason' })])

    const row = wrapper.get('[data-testid="run-attempt-history-row"]')
    expect(row.text()).toContain('Unknown')
    expect(row.text()).not.toContain('mystery_reason')
  })

  it('states plainly that an older run has no recorded attempts', async () => {
    const wrapper = await mountWith([], 0)

    expect(wrapper.get('[data-testid="run-attempt-history-empty"]').text()).toContain('No attempt history')
    expect(wrapper.find('[data-testid="run-attempt-history-table"]').exists()).toBe(false)
  })

  it('renders an inline unavailable state when the fetch fails, without breaking the section', async () => {
    apiMocks.attempts.mockResolvedValue({ data: null, error: { detail: 'not found' } })
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="run-attempt-history-unavailable"]').text()).toContain('unavailable')
    expect(wrapper.find('[data-testid="run-attempt-history-table"]').exists()).toBe(false)
  })

  it('renders the unavailable state when the request throws', async () => {
    apiMocks.attempts.mockRejectedValue(new Error('network down'))
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history-unavailable"]').exists()).toBe(true)
  })

  it('recovers from a failed fetch when the retry affordance is used', async () => {
    apiMocks.attempts
      .mockResolvedValueOnce({ data: null, error: { detail: 'not found' } })
      .mockResolvedValueOnce({ data: { items: [attempt()], total: 1 }, error: undefined })
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history-unavailable"]').exists()).toBe(true)
    await wrapper.get('[data-testid="run-attempt-history-retry"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history-unavailable"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="run-attempt-history-row"]')).toHaveLength(1)
  })

  it('discloses when the attempt list is truncated rather than presenting it as complete', async () => {
    const wrapper = await mountWith([attempt(), attempt({ attempt_index: 1 })], 5)

    expect(wrapper.text()).toContain('Showing the first 2 of 5 attempts')
  })

  it('tolerates a payload that omits items and total', async () => {
    apiMocks.attempts.mockResolvedValue({ data: {}, error: undefined })
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="run-attempt-history-table"]').exists()).toBe(false)
  })

  it('renders a dash for a duration it cannot derive, never a fabricated one', async () => {
    const wrapper = await mountWith([
      attempt({ started_at: null }),
      attempt({ attempt_index: 1, started_at: 'not-a-date' }),
      attempt({ attempt_index: 2, ended_at: '2026-10-24T09:00:00Z' }),
    ])

    const rows = wrapper.findAll('[data-testid="run-attempt-history-row"]')
    expect(rows).toHaveLength(3)
    for (const row of rows) {
      expect(row.findAll('td')[1].text()).toBe('—')
    }
  })

  it('renders a dash when an attempt carries no model', async () => {
    const wrapper = await mountWith([attempt({ model_used: null })])

    const row = wrapper.get('[data-testid="run-attempt-history-row"]')
    expect(row.findAll('td')[3].text()).toBe('—')
  })

  it('ignores a stale success response when the run changes mid-flight', async () => {
    let resolveFirst: (value: unknown) => void = () => {}
    apiMocks.attempts
      .mockImplementationOnce(() => new Promise((resolve) => { resolveFirst = resolve }))
      .mockResolvedValueOnce({
        data: { items: [attempt({ attempt_index: 3 })], total: 1 },
        error: undefined,
      })
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await wrapper.setProps({ runId: 'run-2' })
    await flushPromises()

    resolveFirst({ data: { items: [attempt({ attempt_index: 9 })], total: 9 }, error: undefined })
    await flushPromises()

    const rows = wrapper.findAll('[data-testid="run-attempt-history-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].find('th').text()).toBe('4')
  })

  it('ignores a stale rejection when the run changes mid-flight', async () => {
    let rejectFirst: (reason: unknown) => void = () => {}
    apiMocks.attempts
      .mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
      .mockResolvedValueOnce({ data: { items: [attempt()], total: 1 }, error: undefined })
    const wrapper = mount(RunAttemptHistory, { props: { runId: 'run-1' } })
    await wrapper.setProps({ runId: 'run-2' })
    await flushPromises()

    rejectFirst(new Error('stale request failed'))
    await flushPromises()

    expect(wrapper.find('[data-testid="run-attempt-history-unavailable"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="run-attempt-history-loading"]').exists()).toBe(false)
    expect(wrapper.findAll('[data-testid="run-attempt-history-row"]')).toHaveLength(1)
  })
})
