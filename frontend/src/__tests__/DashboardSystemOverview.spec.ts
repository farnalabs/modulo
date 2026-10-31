import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createI18n } from 'vue-i18n'
import enUS from '../locales/en-US.js'
import DashboardSystemOverview from '../components/dashboard/DashboardSystemOverview.vue'

const i18n = createI18n({ legacy: false, locale: 'en-US', messages: { 'en-US': enUS } })

describe('DashboardSystemOverview', () => {
  it('keeps discovery disabled until the discovery API exists', () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })

    expect(wrapper.get('[data-testid="dashboard-system-discover"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="system-map-draft-review"]').text()).toContain('No discovery request was made')
  })

  it('renders through the shared SectionCard with a live region', () => {
    setActivePinia(createPinia())
    const wrapper = mount(DashboardSystemOverview, { global: { plugins: [i18n] } })

    const card = wrapper.get('[data-testid="dashboard-system-overview"]')
    expect(card.attributes('aria-live')).toBe('polite')
    expect(card.get('h2').text()).toContain('System Overview')
  })
})
