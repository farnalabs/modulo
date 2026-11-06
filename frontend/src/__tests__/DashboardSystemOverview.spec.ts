import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'

const mockGet = vi.hoisted(() => vi.fn())
const mockPush = vi.hoisted(() => vi.fn())
const mockPlanStore = vi.hoisted(() => ({
  loaded: true,
  currentTier: 'community',
  devMode: true,
  userPermissions: ['system_maps_access', 'system.config.manage'],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))
const mockIsOperator = vi.hoisted(() => ({ value: true }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: () => 'test-token',
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mockPush }) }))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
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
    mockPush.mockReset()
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
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

  it('shows discovery only after confirmed internal authoring access loads', async () => {
    const { wrapper, store } = mountOverview()

    expect(wrapper.find('[data-testid="dashboard-system-discover"]').exists()).toBe(false)
    store.accessLoaded = true
    store.systemMapsEnabled = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get('[data-testid="dashboard-system-discover"]').text()).toContain('Discover My System')
  })

  it('does not present discovery for published-read viewers', async () => {
    const { wrapper, store } = mountOverview()
    mockPlanStore.userPermissions = ['system_maps.read']
    mockIsOperator.value = false
    store.accessLoaded = true
    store.systemMapsEnabled = true
    await wrapper.vm.$nextTick()

    expect(wrapper.find('[data-testid="dashboard-system-discover"]').exists()).toBe(false)
  })

  it('does not present discovery while access is loading or failed', async () => {
    const { wrapper, store } = mountOverview()
    store.accessLoaded = false
    store.systemMapsEnabled = true
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="dashboard-system-discover"]').exists()).toBe(false)

    store.accessLoaded = true
    store.error = 'access failed'
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="dashboard-system-discover"]').exists()).toBe(false)
  })

  it('discovers and routes to the created draft for internal operators', async () => {
    const { wrapper, store } = mountOverview()
    store.accessLoaded = true
    store.systemMapsEnabled = true
    const discover = vi.spyOn(store, 'discover').mockResolvedValue({ draft_map_id: 'map-9' } as never)
    mockPush.mockResolvedValue(undefined)
    await wrapper.vm.$nextTick()

    await wrapper.get('[data-testid="dashboard-system-discover"]').trigger('click')

    expect(discover).toHaveBeenCalledTimes(1)
    expect(mockPush).toHaveBeenCalledWith('/system-maps/map-9?discovery=1')
  })

  it('surfaces a discovery failure inline and re-enables the button', async () => {
    const { wrapper, store } = mountOverview()
    store.accessLoaded = true
    store.systemMapsEnabled = true
    const discover = vi.spyOn(store, 'discover').mockRejectedValue(new Error('discovery failed'))
    await wrapper.vm.$nextTick()

    await wrapper.get('[data-testid="dashboard-system-discover"]').trigger('click')

    await vi.waitFor(() =>
      expect(wrapper.find('[data-testid="dashboard-system-discover-error"]').exists()).toBe(true),
    )
    expect(wrapper.get('[data-testid="dashboard-system-discover-error"]').text()).toContain(
      'Could not discover your system',
    )
    expect(discover).toHaveBeenCalledTimes(1)
    // The button is usable again so the operator can retry.
    expect(wrapper.get('[data-testid="dashboard-system-discover"]').attributes('disabled')).toBeUndefined()
    // A failed discovery must not navigate away from the dashboard.
    expect(mockPush).not.toHaveBeenCalled()
  })

  it('dismisses the inline discovery error on request', async () => {
    const { wrapper, store } = mountOverview()
    store.accessLoaded = true
    store.systemMapsEnabled = true
    vi.spyOn(store, 'discover').mockRejectedValue(new Error('discovery failed'))
    await wrapper.vm.$nextTick()

    await wrapper.get('[data-testid="dashboard-system-discover"]').trigger('click')
    await vi.waitFor(() =>
      expect(wrapper.find('[data-testid="dashboard-system-discover-error"]').exists()).toBe(true),
    )

    await wrapper.get('[data-testid="error-alert-dismiss"]').trigger('click')

    expect(wrapper.find('[data-testid="dashboard-system-discover-error"]').exists()).toBe(false)
  })

  it('ignores a second discovery click while the first is still in flight', async () => {
    const { wrapper, store } = mountOverview()
    store.accessLoaded = true
    store.systemMapsEnabled = true
    let resolveDiscover: (value: unknown) => void = () => {}
    const discover = vi.spyOn(store, 'discover').mockReturnValue(
      new Promise((resolve) => {
        resolveDiscover = resolve
      }) as never,
    )
    mockPush.mockResolvedValue(undefined)
    await wrapper.vm.$nextTick()

    const button = wrapper.get('[data-testid="dashboard-system-discover"]')
    // Both clicks dispatch synchronously, before the first click's load state
    // has re-rendered — the guard must reject the duplicate.
    await Promise.all([button.trigger('click'), button.trigger('click')])

    expect(discover).toHaveBeenCalledTimes(1)

    resolveDiscover({ draft_map_id: 'map-1' })
    await vi.waitFor(() => expect(mockPush).toHaveBeenCalledWith('/system-maps/map-1?discovery=1'))
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

  it('renders n/a for an observed node whose individual metrics are absent', async () => {
    const { wrapper, store } = mountOverview()
    store.overview = {
      ...overview,
      nodes: [
        {
          ...overview.nodes[0],
          node_key: 'node-unmeasured',
          display_name: 'Healthy but unmeasured',
          health: 'healthy',
          queue_depth: null,
          throughput_per_min: null,
          failure_rate: null,
          telemetry_coverage: null,
        },
      ],
    } as never
    store.overviewLoading = false
    store.overviewError = null
    store.overviewEmpty = false
    await wrapper.vm.$nextTick()

    const node = wrapper.get('[data-testid="system-map-node"]')
    expect(node.text()).toContain('Healthy but unmeasured')
    // Each absent metric renders the shared n/a fallback rather than a blank grid.
    expect(node.text().match(/n\/a/g)).toHaveLength(4)
  })

  it('shows the no-observed empty state when every node has no measured activity', async () => {
    const { wrapper, store } = mountOverview()
    store.overview = { ...overview, nodes: [overview.nodes[1]] } as never
    store.overviewLoading = false
    store.overviewError = null
    store.overviewEmpty = false
    await wrapper.vm.$nextTick()

    const empty = wrapper.get('[data-testid="system-map-overview-empty"]')
    expect(empty.text()).toContain('No observed activity yet')
    expect(wrapper.find('[data-testid="system-map-overview-metrics"]').exists()).toBe(false)
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
