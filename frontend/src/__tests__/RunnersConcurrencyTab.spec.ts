import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const { getMock, putMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  putMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: getMock, PUT: putMock },
}))

import RunnersConcurrencyTab from '../views/runners/RunnersConcurrencyTab.vue'

const status = (state: string | null, detail: string | null = 'preflight detail') => ({
  aggregate_state: 'healthy' as const,
  probe_interval_seconds: 60,
  staleness_threshold_seconds: 300,
  concurrency: {
    sandbox_concurrency_limit: null,
    is_default: true,
    preflight: state === null ? {
      state: 'unknown' as const,
      detail: null,
      engine_cpu_count: null,
      engine_mem_total_mb: null,
      needed_cpu: null,
      needed_mem_mb: null,
    } : {
      state: state as 'ok' | 'exceeds_cpu' | 'exceeds_mem' | 'exceeds_cpu_and_mem' | 'uncapped' | 'unknown',
      detail,
      engine_cpu_count: state === 'ok' ? 4 : null,
      engine_mem_total_mb: state === 'ok' ? 8192 : null,
      needed_cpu: 2,
      needed_mem_mb: 1024,
    },
  },
})

async function mountView(limitData: { sandbox_concurrency_limit: number | null; is_default: boolean } = { sandbox_concurrency_limit: 8, is_default: false }, runnerStatus = status('ok')) {
  getMock.mockResolvedValue({ data: limitData, error: undefined })
  const reloadStatus = vi.fn().mockResolvedValue(undefined)
  const wrapper = mount(RunnersConcurrencyTab, { props: { status: runnerStatus, reloadStatus } })
  await flushPromises()
  return { wrapper, reloadStatus }
}

describe('RunnersConcurrencyTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ data: { sandbox_concurrency_limit: 8, is_default: false }, error: undefined })
    putMock.mockResolvedValue({ data: null, error: undefined })
  })

  it('renders effective explicit caps and preflight resource details', async () => {
    const { wrapper } = await mountView()
    expect(wrapper.text()).toContain('8')
    expect(wrapper.text()).toContain('Engine resources')
    expect(wrapper.find('[data-testid="runner-concurrency-preflight"]').classes()).toContain('border-success/40')
  })

  it.each([
    ['default', { sandbox_concurrency_limit: 4, is_default: true }, null, 'Default (unset)'],
    ['no gate', { sandbox_concurrency_limit: null, is_default: false }, null, 'No gate'],
    ['deny all', { sandbox_concurrency_limit: 0, is_default: false }, null, 'deny-all'],
    ['uncapped preflight', { sandbox_concurrency_limit: 2, is_default: false }, status('uncapped'), 'Uncapped'],
    ['unknown preflight', { sandbox_concurrency_limit: 2, is_default: false }, status('unknown', 'unknown detail'), 'unknown detail'],
  ])('renders %s state', async (_name, limit, runnerStatus, expected) => {
    const { wrapper } = await mountView(limit, runnerStatus ?? status(null))
    expect(wrapper.text()).toContain(expected)
  })

  it('renders warning preflight branches and fallback detail', async () => {
    for (const state of ['exceeds_cpu', 'exceeds_mem', 'exceeds_cpu_and_mem']) {
      const { wrapper } = await mountView(undefined, status(state, 'fallback detail'))
      expect(wrapper.text()).toContain(state === 'exceeds_cpu' ? 'CPU' : state === 'exceeds_mem' ? 'memory' : 'CPU and memory')
      expect(wrapper.text()).toContain('fallback detail')
    }
  })

  it('clamps and saves the limit, refetching both sources', async () => {
    const { wrapper, reloadStatus } = await mountView()
    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]')
    await input.setValue('150.8')
    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await flushPromises()
    expect(putMock).toHaveBeenCalledWith('/api/v1/admin/org/sandbox-concurrency', { body: { sandbox_concurrency_limit: 100 } })
    expect(getMock).toHaveBeenCalledTimes(2)
    expect(reloadStatus).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('Runner concurrency limit updated')
  })

  it('handles API and thrown save failures, including a null limit', async () => {
    const { wrapper } = await mountView()
    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]')
    await input.setValue('')
    putMock.mockResolvedValueOnce({ data: null, error: { detail: 'invalid limit' } })
    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('invalid limit')

    putMock.mockRejectedValueOnce(new Error('connection lost'))
    await input.setValue('5')
    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('connection lost')
  })

  it('shows load errors and invokes retry', async () => {
    getMock.mockRejectedValueOnce(new Error('status unavailable'))
    const wrapper = mount(RunnersConcurrencyTab, { props: { status: status(null), reloadStatus: vi.fn() } })
    await flushPromises()
    expect(wrapper.text()).toContain('status unavailable')
    getMock.mockResolvedValueOnce({ data: { sandbox_concurrency_limit: 3, is_default: false }, error: undefined })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('3')
  })
})
