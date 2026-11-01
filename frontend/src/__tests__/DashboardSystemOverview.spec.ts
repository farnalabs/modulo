import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
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
 * fetch functions are exercised end-to-end in systemMapsStore.spec.ts.
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
  })

  it('loads discovery and then the default overview on mount', async () => {
    const { fetchDiscovery, fetchDefaultOverview } = mountOverview()
    await vi.waitFor(() => expect(fetchDefaultOverview).toHaveBeenCalledTimes(1))

    expect(fetchDiscovery).toHaveBeenCalledTimes(1)
    expect(fetchDiscovery.mock.invocationCallOrder[0]).toBeLessThan(
      fetchDefaultOverview.mock.invocationCallOrder[0],
    )
  })

  it('keeps discovery disabled until the discovery API exists', () => {
    const { wrapper } = mountOverview()

    expect(wrapper.get('[data-testid="dashboard-system-discover"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="system-map-draft-review"]').text()).toContain(
      'No discovery request was made',
    )
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
    expect(nodes).toHaveLength(2)
    expect(nodes[0].text()).toContain('Degraded')
    expect(nodes[0].text()).toContain('34%')
    expect(nodes[0].text()).toContain('50%')
    expect(nodes[0].text()).toContain('Build')
    // Unobserved values stay n/a rather than being rendered as zero.
    expect(nodes[1].text()).toContain('Unknown')
    expect(nodes[1].text()).toContain('n/a')
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
    expect(wrapper.find('[data-testid="system-map-overview-empty"]').exists()).toBe(false)
  })

  it('renders a distinct empty state, not an error', async () => {
    const { wrapper, store } = mountOverview()
    store.overviewLoading = false
    store.overviewError = null
    store.overviewEmpty = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="system-map-overview-empty"]').text()).toContain('No published system map')
    expect(wrapper.find('[data-testid="system-map-overview-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="system-map-overview-metrics"]').exists()).toBe(false)
  })

  it('renders through the shared SectionCard with a live region', () => {
    const { wrapper } = mountOverview()

    const card = wrapper.get('[data-testid="dashboard-system-overview"]')
    expect(card.attributes('aria-live')).toBe('polite')
    expect(card.get('h2').text()).toContain('System Overview')
  })
})
