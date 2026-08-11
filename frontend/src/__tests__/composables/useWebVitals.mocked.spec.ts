import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'

const h = vi.hoisted(() => {
  const metricHandlers: Record<string, (metric: unknown) => void> = {}
  const featureEnabled = vi.fn()
  const fetchPlan = vi.fn()
  const planStore = { currentTier: 'community', fetchPlan, featureEnabled }
  const usePlanStore = vi.fn(() => planStore)
  return { metricHandlers, featureEnabled, fetchPlan, planStore, usePlanStore }
})

vi.mock('web-vitals', () => ({
  onLCP: vi.fn((cb: (metric: unknown) => void) => { h.metricHandlers.LCP = cb }),
  onFCP: vi.fn((cb: (metric: unknown) => void) => { h.metricHandlers.FCP = cb }),
  onCLS: vi.fn((cb: (metric: unknown) => void) => { h.metricHandlers.CLS = cb }),
  onINP: vi.fn((cb: (metric: unknown) => void) => { h.metricHandlers.INP = cb }),
  onTTFB: vi.fn((cb: (metric: unknown) => void) => { h.metricHandlers.TTFB = cb }),
}))

vi.mock('../../stores/planStore', () => ({
  usePlanStore: h.usePlanStore,
}))

function makeMetric(name: string, value = 100): { name: string; value: number; rating: string; navigationType: string } {
  return { name, value, rating: 'good', navigationType: 'navigate' }
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.resetModules()
  vi.useRealTimers()
  for (const key of Object.keys(h.metricHandlers)) delete h.metricHandlers[key]
  h.featureEnabled.mockReset()
  h.fetchPlan.mockReset()
  h.fetchPlan.mockResolvedValue(undefined)
  h.planStore.currentTier = 'community'
  fetchMock = vi.fn(async () => ({ ok: true } as unknown as Response))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

async function mountHarness() {
  const { useWebVitals } = await import('../../composables/useWebVitals')
  const Harness = defineComponent({
    setup() {
      useWebVitals()
      return () => null
    },
  })
  return mount(Harness)
}

describe('useWebVitals', () => {
  it('does not register handlers or fetch when the feature flag is disabled', async () => {
    h.featureEnabled.mockReturnValue(false)
    const wrapper = await mountHarness()

    await vi.waitFor(() => expect(h.usePlanStore).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 20))

    expect(Object.keys(h.metricHandlers)).toHaveLength(0)
    wrapper.unmount()
    await new Promise(r => setTimeout(r, 20))
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('registers all five web-vitals handlers when enabled', async () => {
    h.featureEnabled.mockReturnValue(true)
    const wrapper = await mountHarness()

    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))
    expect(Object.keys(h.metricHandlers).sort()).toEqual(['CLS', 'FCP', 'INP', 'LCP', 'TTFB'])
    wrapper.unmount()
  })

  it('buffers metrics and flushes after the 5s scheduled flush', async () => {
    h.featureEnabled.mockReturnValue(true)
    vi.useFakeTimers()
    const wrapper = await mountHarness()
    await vi.advanceTimersByTimeAsync(0)
    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))

    h.metricHandlers.LCP(makeMetric('LCP', 120))

    expect(fetchMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(5000)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/metrics/web-vitals',
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
      }),
    )
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.events).toHaveLength(1)
    expect(body.events[0]).toEqual(
      expect.objectContaining({
        metric_name: 'LCP',
        metric_value: 120,
        metric_rating: 'good',
        navigation_type: 'navigate',
      }),
    )
    expect(body.events[0].route_path).toBe(window.location.pathname)
    expect(body.events[0].page_url).toBe(window.location.href)
    wrapper.unmount()
  })

  it('flushes immediately once ten metrics accumulate', async () => {
    h.featureEnabled.mockReturnValue(true)
    const wrapper = await mountHarness()
    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))

    for (let i = 0; i < 9; i++) h.metricHandlers.INP(makeMetric('INP', i))
    expect(fetchMock).not.toHaveBeenCalled()

    h.metricHandlers.INP(makeMetric('INP', 99))

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.events).toHaveLength(10)
    wrapper.unmount()
  })

  it('flushes buffered metrics on unmount', async () => {
    h.featureEnabled.mockReturnValue(true)
    const wrapper = await mountHarness()
    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))

    h.metricHandlers.FCP(makeMetric('FCP', 55))
    h.metricHandlers.TTFB(makeMetric('TTFB', 30))

    expect(fetchMock).not.toHaveBeenCalled()
    wrapper.unmount()

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const body = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)
    expect(body.events).toHaveLength(2)
  })

  it('silently swallows fetch failures and keeps the app alive', async () => {
    h.featureEnabled.mockReturnValue(true)
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const wrapper = await mountHarness()
    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))

    for (let i = 0; i < 10; i++) h.metricHandlers.CLS(makeMetric('CLS', i))
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())

    expect(errorSpy).not.toHaveBeenCalled()
    errorSpy.mockRestore()
    wrapper.unmount()
  })

  it('fetches the plan first when the current tier is unknown', async () => {
    h.planStore.currentTier = ''
    h.featureEnabled.mockReturnValue(true)
    const wrapper = await mountHarness()

    await vi.waitFor(() => expect(h.fetchPlan).toHaveBeenCalled())
    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))
    wrapper.unmount()
  })

  it('skips registration when the initial plan fetch fails', async () => {
    h.planStore.currentTier = ''
    h.fetchPlan.mockRejectedValue(new Error('plan fetch failed'))
    h.featureEnabled.mockReturnValue(true)
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const wrapper = await mountHarness()

    await vi.waitFor(() => expect(h.fetchPlan).toHaveBeenCalled())
    await new Promise(r => setTimeout(r, 20))
    expect(Object.keys(h.metricHandlers)).toHaveLength(0)
    errorSpy.mockRestore()
    wrapper.unmount()
  })

  it('does not refetch the plan when the tier is already known', async () => {
    h.planStore.currentTier = 'team'
    h.featureEnabled.mockReturnValue(true)
    const wrapper = await mountHarness()

    await vi.waitFor(() => expect(Object.keys(h.metricHandlers)).toHaveLength(5))
    expect(h.fetchPlan).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
