import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

const { onLCP, onFCP, onCLS, onINP, onTTFB } = vi.hoisted(() => ({
  onLCP: vi.fn(),
  onFCP: vi.fn(),
  onCLS: vi.fn(),
  onINP: vi.fn(),
  onTTFB: vi.fn(),
}))

vi.mock('web-vitals', () => ({
  onLCP,
  onFCP,
  onCLS,
  onINP,
  onTTFB,
}))

let useWebVitals: () => void

async function mountWebVitals() {
  ;({ useWebVitals } = await import('../../composables/useWebVitals'))
  return mount(defineComponent({
    setup() {
      useWebVitals()
      return () => h('div')
    },
  }))
}

function fakeMetric(name: string) {
  return {
    name,
    value: 100,
    rating: 'good',
    id: `id-${name}`,
    delta: 100,
    entries: [],
    navigationType: 'navigate',
  } as unknown as Parameters<typeof onLCP>[0]
}

const VITALS_API = '/api/v1/metrics/web-vitals'

beforeEach(() => {
  vi.resetModules()
  setActivePinia(createPinia())
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 200 }))
})

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

async function flushAll() {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}

describe('useWebVitals', () => {
  it('does not register listeners or post when the feature flag is off', async () => {
    const wrapper = await mountWebVitals()
    await flushAll()

    expect(onLCP).not.toHaveBeenCalled()
    expect(onFCP).not.toHaveBeenCalled()
    expect(onCLS).not.toHaveBeenCalled()
    expect(onINP).not.toHaveBeenCalled()
    expect(onTTFB).not.toHaveBeenCalled()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('registers all five metric listeners when the feature flag is on', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    usePlanStore().features['web_vitals_analytics'] = true

    const wrapper = await mountWebVitals()
    await flushAll()

    expect(onLCP).toHaveBeenCalledTimes(1)
    expect(onFCP).toHaveBeenCalledTimes(1)
    expect(onCLS).toHaveBeenCalledTimes(1)
    expect(onINP).toHaveBeenCalledTimes(1)
    expect(onTTFB).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('buffers metrics and posts them once ten have accumulated', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    usePlanStore().features['web_vitals_analytics'] = true

    const wrapper = await mountWebVitals()
    await flushAll()

    const report = onLCP.mock.calls[0][0]
    for (let i = 0; i < 9; i++) {
      report(fakeMetric(`LCP-${i}`))
    }
    expect(globalThis.fetch).not.toHaveBeenCalled()

    report(fakeMetric('LCP-final'))
    await flushAll()

    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    expect(globalThis.fetch).toHaveBeenCalledWith(
      VITALS_API,
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        body: expect.stringContaining('LCP-final'),
      }),
    )
    wrapper.unmount()
  })

  it('flushes buffered metrics on unmount', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    usePlanStore().features['web_vitals_analytics'] = true

    const wrapper = await mountWebVitals()
    await flushAll()

    const report = onLCP.mock.calls[0][0]
    report(fakeMetric('LCP-unmount'))
    expect(globalThis.fetch).not.toHaveBeenCalled()

    wrapper.unmount()
    await flushAll()

    expect(globalThis.fetch).toHaveBeenCalledTimes(1)
    const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    const body = JSON.parse((init as RequestInit).body as string)
    expect(body.events).toHaveLength(1)
    expect(body.events[0].metric_name).toBe('LCP-unmount')
  })

  it('records route path and page url on each metric', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    usePlanStore().features['web_vitals_analytics'] = true

    const wrapper = await mountWebVitals()
    await flushAll()

    const report = onINP.mock.calls[0][0]
    report(fakeMetric('INP'))
    wrapper.unmount()
    await flushAll()

    const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    const body = JSON.parse((init as RequestInit).body as string)
    expect(body.events[0].route_path).toBe(window.location.pathname)
    expect(body.events[0].page_url).toBe(window.location.href)
  })

  it('fetches the plan first when currentTier is unset', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    const planStore = usePlanStore()
    planStore.currentTier = null as unknown as string
    const fetchPlanSpy = vi.spyOn(planStore, 'fetchPlan').mockResolvedValue(undefined)
    planStore.features['web_vitals_analytics'] = true

    const wrapper = await mountWebVitals()
    await flushAll()

    expect(fetchPlanSpy).toHaveBeenCalled()
    expect(onLCP).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('stays disabled when the plan fetch fails', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    const planStore = usePlanStore()
    planStore.currentTier = null as unknown as string
    vi.spyOn(planStore, 'fetchPlan').mockRejectedValue(new Error('plan unavailable'))

    const wrapper = await mountWebVitals()
    await flushAll()

    expect(onLCP).not.toHaveBeenCalled()
    expect(globalThis.fetch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not call fetchPlan when the tier is already known', async () => {
    const { usePlanStore } = await import('../../stores/planStore')
    const planStore = usePlanStore()
    planStore.currentTier = 'community'
    const fetchPlanSpy = vi.spyOn(planStore, 'fetchPlan').mockResolvedValue(undefined)
    planStore.features['web_vitals_analytics'] = false

    const wrapper = await mountWebVitals()
    await flushAll()

    expect(fetchPlanSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
