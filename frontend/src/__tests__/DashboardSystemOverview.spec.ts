import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: () => 'test-token',
}))

import enUS from '../locales/en-US.js'
import DashboardSystemOverview from '../components/dashboard/DashboardSystemOverview.vue'
import { useSystemMapsStore } from '../stores/systemMaps'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

const overview = {
  system_map_id: 'map',
  revision_id: 'revision',
  revision_number: 1,
  generated_at: '2026-01-08T00:00:00Z',
  window_days: 7,
  window_start: '2026-01-01T00:00:00Z',
  window_end: '2026-01-08T00:00:00Z',
  nodes: [
    {
      node_key: 'node-1',
      node_type: 'pipeline',
      display_name: 'Build',
      health: 'degraded',
      queue_depth: 3,
      throughput_per_min: 0.5,
      capacity_pressure: 0.25,
      failure_rate: 0.34,
      telemetry_coverage: 0.5,
      last_run_id: null,
      last_run_status: null,
      last_run_completed_at: null,
    },
    {
      node_key: 'node-2',
      node_type: 'pipeline',
      display_name: 'Deploy',
      health: 'unknown',
      queue_depth: null,
      throughput_per_min: null,
      capacity_pressure: null,
      failure_rate: null,
      telemetry_coverage: null,
      last_run_id: null,
      last_run_status: null,
      last_run_completed_at: null,
    },
  ],
  loop_configs: [],
  bottlenecks: [],
}

/**
 * Mount with the store's own network actions stubbed: this spec covers the
 * RENDERING contract (loading / error / empty / metrics states), while the real
 * fetch functions are exercised end-to-end in systemMapsStore.spec.ts. The
 * retry case below deliberately leaves the real fetch in place (with `api.GET`
 * mocked) so the retry path is proven through the store rather than a stub.
 */
function mountOverview() {
  setActivePinia(createPinia())
  const store = useSystemMapsStore()
  const fetchDiscovery = vi.spyOn(store, 'fetchDiscovery').mockResolvedValue()
  const fetchDefaultOverview = vi.spyOn(store, 'fetchDefaultOverview').mockResolvedValue()
  const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })
  return { wrapper, store, fetchDiscovery, fetchDefaultOverview }
}

describe('DashboardSystemOverview', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    mockGet.mockReset()
  })

  it('loads discovery and the selected default overview window on mount', async () => {
    const { fetchDiscovery, fetchDefaultOverview } = mountOverview()
    await vi.waitFor(() => expect(fetchDefaultOverview).toHaveBeenCalledTimes(1))

    expect(fetchDiscovery).toHaveBeenCalledTimes(1)
    expect(fetchDefaultOverview).toHaveBeenCalledWith(7)
    expect(fetchDiscovery.mock.invocationCallOrder[0]).toBeLessThan(
      fetchDefaultOverview.mock.invocationCallOrder[0],
    )
  })

  it('passes a non-default selected window to the overview store', async () => {
    setActivePinia(createPinia())
    const store = useSystemMapsStore()
    vi.spyOn(store, 'fetchDiscovery').mockResolvedValue()
    const fetchDefaultOverview = vi.spyOn(store, 'fetchDefaultOverview').mockResolvedValue()

    mount(DashboardSystemOverview, { props: { windowDays: 30 }, global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(fetchDefaultOverview).toHaveBeenCalledTimes(1))

    expect(fetchDefaultOverview).toHaveBeenCalledWith(30)
  })

  it('does not present a discovery action on the dashboard', () => {
    const { wrapper } = mountOverview()

    expect(wrapper.find('[data-testid="dashboard-system-discover"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-maps-discover"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Discover My System')
  })

  it('renders observed metrics with interpolated window, rates and localized health', async () => {
    const { wrapper, store } = mountOverview()
    store.overview = overview as never
    store.overviewLoading = false
    store.overviewError = null
    store.overviewEmpty = false
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="system-map-overview-metrics"]').text()).toContain(
      'Observed over the last 7 days',
    )
    const nodes = wrapper.findAll('[data-testid="system-map-node"]')
    expect(nodes).toHaveLength(1)
    expect(nodes[0].text()).toContain('Degraded')
    expect(nodes[0].text()).toContain('34%')
    expect(nodes[0].text()).toContain('50%')
    expect(nodes[0].text()).toContain('Build')
    // Nodes with no observed metrics are omitted instead of presenting a grid of
    // unknown values; the card shows an explicit empty state when all are absent.
    expect(wrapper.find('[data-testid="system-map-draft-review"]').exists()).toBe(false)
  })

  it('renders the loading state first', async () => {
    const { wrapper, store } = mountOverview()
    store.overviewLoading = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="system-map-overview-loading"]').text()).toContain('Loading')
    expect(wrapper.find('[data-testid="system-map-overview-metrics"]').exists()).toBe(false)
  })

  it('renders a distinct error state', async () => {
    const { wrapper, store } = mountOverview()
    store.overviewLoading = false
    store.overviewError = 'failed'
    await wrapper.vm.$nextTick()

    const error = wrapper.get('[data-testid="system-map-overview-error"]')
    expect(error.text()).toContain('unavailable')
    expect(error.attributes('aria-live')).toBe('assertive')
    // The shared ErrorAlert carries the retry affordance into this view.
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="system-map-overview-empty"]').exists()).toBe(false)
  })

  it('renders the unavailable state without a retry on a 402 feature_required', async () => {
    const { wrapper, store } = mountOverview()
    store.overviewLoading = false
    store.overviewUnavailable = true
    await wrapper.vm.$nextTick()

    const unavailable = wrapper.get('[data-testid="system-map-overview-error"]')
    expect(unavailable.text()).toContain('System Maps is not available on your plan')
    expect(wrapper.find('[data-testid="error-alert-retry"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-overview-empty"]').exists()).toBe(false)
  })

  it('retries the overview fetch from the shared error alert', async () => {
    const { wrapper, store, fetchDefaultOverview } = mountOverview()
    await vi.waitFor(() => expect(fetchDefaultOverview).toHaveBeenCalledTimes(1))
    store.overviewLoading = false
    store.overviewError = 'failed'
    await wrapper.vm.$nextTick()

    await wrapper.get('[data-testid="error-alert-retry"]').trigger('click')

    await vi.waitFor(() => expect(fetchDefaultOverview).toHaveBeenCalledTimes(2))
  })

  it('retries the overview from the error state and replaces it with the loaded metrics', async () => {
    setActivePinia(createPinia())
    const store = useSystemMapsStore()
    const fetchDiscovery = vi.spyOn(store, 'fetchDiscovery').mockResolvedValue()
    mockGet
      // First list request fails: the view settles into its error state.
      .mockResolvedValueOnce({ error: { detail: 'System map list request failed' } })
      // The retry re-runs the failing list request, which now succeeds...
      .mockResolvedValueOnce({
        data: {
          items: [{ id: 'map', published_revision_id: 'revision', default_map: true }],
          total: 1,
          page: 1,
          page_size: 20,
        },
      })
      // ...and the overview it resolves to.
      .mockResolvedValueOnce({ data: overview })

    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })
    await vi.waitFor(() =>
      expect(wrapper.find('[data-testid="system-map-overview-error"]').exists()).toBe(true),
    )

    await wrapper.get('[data-testid="error-alert-retry"]').trigger('click')

    await vi.waitFor(() =>
      expect(wrapper.find('[data-testid="system-map-overview-metrics"]').exists()).toBe(true),
    )
    expect(wrapper.find('[data-testid="system-map-overview-error"]').exists()).toBe(false)
    // The retry re-runs every source the view reads, not only the leaf overview.
    expect(fetchDiscovery).toHaveBeenCalledTimes(2)
    expect(mockGet).toHaveBeenCalledTimes(3)
  })

  it('renders a distinct empty state, not an error', async () => {
    const { wrapper, store } = mountOverview()
    store.overviewLoading = false
    store.overviewError = null
    store.overviewEmpty = true
    await wrapper.vm.$nextTick()

    const empty = wrapper.get('[data-testid="system-map-overview-empty"]')
    expect(empty.text()).toContain('No published system map')
    // Rendered through the shared EmptyState rather than a bespoke box.
    expect(empty.classes()).toContain('card')
    expect(wrapper.find('[data-testid="system-map-overview-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-overview-metrics"]').exists()).toBe(false)
  })

  it('persists the collapse state and exposes the button state', async () => {
    localStorage.removeItem('dashboard-system-overview-collapsed')
    const { wrapper } = mountOverview()
    const toggle = wrapper.get('[data-testid="system-map-overview-toggle"]')
    expect(toggle.attributes('aria-expanded')).toBe('true')

    await toggle.trigger('click')

    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(localStorage.getItem('dashboard-system-overview-collapsed')).toBe('true')
  })

  it('renders through the shared SectionCard without a live region', () => {
    const { wrapper } = mountOverview()

    const card = wrapper.get('[data-testid="dashboard-system-overview"]')
    expect(card.attributes('aria-live')).toBeUndefined()
    expect(card.get('h2').text()).toContain('System Overview')
  })
})
