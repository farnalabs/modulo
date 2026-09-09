import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { usePlanStore } from '../../stores/planStore'

const reloadStatus = vi.fn().mockResolvedValue(undefined)

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        // ABSENT key: the Docker-tier default 4 (D3b reader contract).
        return Promise.resolve({ data: { sandbox_concurrency_limit: 4, is_default: true }, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: null }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import RunnersConcurrencyTab from '../../views/runners/RunnersConcurrencyTab.vue'
import type { RunnersStatus } from '../../lib/runnersStatus'

const baseStatus: RunnersStatus = {
  aggregate_state: 'healthy',
  probe_interval_seconds: 60,
  staleness_threshold_seconds: 120,
  machines: [],
  profiles: [],
  concurrency: {
    sandbox_concurrency_limit: 4,
    is_default: true,
    preflight: {
      state: 'ok',
      detail: null,
      engine_cpu_count: 8,
      engine_mem_total_mb: 16384,
      needed_cpu: 4,
      needed_mem_mb: 4096,
    },
  },
}

describe('RunnersConcurrencyTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    reloadStatus.mockClear()
  })

  function mountTab(status: RunnersStatus | null = baseStatus) {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ currentTier: 'team', features: { environment_profiles: true } })
    return mount(RunnersConcurrencyTab, {
      props: { status, reloadStatus },
    })
  }

  it('round-trips the ABSENT key as "4 (default)" (D3b is_default)', async () => {
    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    const effective = wrapper.find('[data-testid="runner-concurrency-effective"]')
    expect(effective.text()).toContain('4 (default)')
  })

  it('round-trips an explicit 0 as deny-all and SAVES 0 unchanged', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        return Promise.resolve({ data: { sandbox_concurrency_limit: 0, is_default: false }, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="runner-concurrency-effective"]').text()).toContain('deny-all')

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: 0 },
    })
  })

  it('shows an empty input for an explicit null (no gate) and saves null', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/org/sandbox-concurrency') {
        return Promise.resolve({ data: { sandbox_concurrency_limit: null, is_default: false }, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    const input = wrapper.find('[data-testid="admin-sandbox-concurrency-limit"]') as any
    expect(input.element.value).toBe('')
    expect(wrapper.find('[data-testid="runner-concurrency-effective"]').text()).toContain('No gate')

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/org/sandbox-concurrency', {
      body: { sandbox_concurrency_limit: null },
    })
  })

  it('warns when the cap exceeds the engine resources (preflight)', async () => {
    const status: RunnersStatus = {
      ...baseStatus,
      concurrency: {
        sandbox_concurrency_limit: 16,
        is_default: false,
        preflight: {
          state: 'exceeds_cpu_and_mem',
          detail: null,
          engine_cpu_count: 2,
          engine_mem_total_mb: 2048,
          needed_cpu: 16,
          needed_mem_mb: 16384,
        },
      },
    }
    const wrapper = mountTab(status)
    await flushPromises()
    await nextTick()
    const preflight = wrapper.find('[data-testid="runner-concurrency-preflight"]')
    expect(preflight.text()).toContain("exceeds both the engine's CPU and memory")
  })

  it('flags the uncapped state when no cap is set', async () => {
    const status: RunnersStatus = {
      ...baseStatus,
      concurrency: {
        sandbox_concurrency_limit: null,
        is_default: false,
        preflight: {
          state: 'uncapped',
          detail: 'No concurrency cap is set — host headroom cannot be assessed.',
          engine_cpu_count: null,
          engine_mem_total_mb: null,
          needed_cpu: null,
          needed_mem_mb: null,
        },
      },
    }
    const wrapper = mountTab(status)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('[data-testid="runner-concurrency-preflight"]').text()).toContain('Uncapped')
  })

  it('reloads the status after a successful save', async () => {
    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(reloadStatus).toHaveBeenCalled()
  })

  it('shows the updated message after a successful save', async () => {
    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(wrapper.text()).toContain('Runner concurrency limit updated.')
  })

  it('shows an error when the save API call fails', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.PUT as unknown as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: null,
      error: { detail: 'boom' },
    })

    const wrapper = mountTab()
    await nextTick()
    await flushPromises()
    await nextTick()

    await wrapper.find('[data-testid="admin-sandbox-concurrency-save"]').trigger('click')
    await nextTick()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed to save')
    expect(wrapper.text()).toContain('boom')
  })
})
