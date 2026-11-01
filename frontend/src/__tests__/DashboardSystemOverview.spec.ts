import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import DashboardSystemOverview from '../components/dashboard/DashboardSystemOverview.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

const overview = {
  system_map_id: 'map',
  revision_id: 'revision',
  revision_number: 1,
  generated_at: '2026-01-01T00:00:00Z',
  window_days: 7,
  window_start: '2025-12-25T00:00:00Z',
  window_end: '2026-01-01T00:00:00Z',
  nodes: [{ node_key: 'node-1', node_type: 'pipeline', display_name: 'Build', health: 'degraded', queue_depth: null, throughput_per_min: null, capacity_pressure: null, failure_rate: null, telemetry_coverage: null, last_run_id: null, last_run_status: null, last_run_completed_at: null }],
  loop_configs: [],
  bottlenecks: [],
}

describe('DashboardSystemOverview', () => {
  it('keeps discovery disabled until the discovery API exists', () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })

    expect(wrapper.get('[data-testid="dashboard-system-discover"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="system-map-draft-review"]').text()).toContain('No discovery request was made')
  })

  it('renders observed metrics with interpolated window and localized health', async () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })
    const store = (await import('../stores/systemMaps')).useSystemMapsStore()
    store.overview = overview as never
    store.overviewLoading = false
    store.overviewError = null
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('Observed over the last 7 days')
    expect(wrapper.text()).toContain('Degraded')
    expect(wrapper.text()).toContain('n/a')
  })

  it('renders loading and error states', async () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })
    const store = (await import('../stores/systemMaps')).useSystemMapsStore()
    store.overviewLoading = true
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="system-map-overview-loading"]').text()).toContain('Loading')
    store.overviewLoading = false
    store.overviewError = 'failed'
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[data-testid="system-map-overview-error"]').text()).toContain('unavailable')
  })

  it('renders through the shared SectionCard with a live region', () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })

    const card = wrapper.get('[data-testid="dashboard-system-overview"]')
    expect(card.attributes('aria-live')).toBe('polite')
    expect(card.get('h2').text()).toContain('System Overview')
  })
})
