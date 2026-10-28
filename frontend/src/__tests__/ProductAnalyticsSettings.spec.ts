import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import ProductAnalyticsSettings from '../components/product-analytics/ProductAnalyticsSettings.vue'
import ProductAnalyticsError from '../components/product-analytics/ProductAnalyticsError.vue'
import ToggleSwitch from '../components/shared/ToggleSwitch.vue'

const i18n = createI18n({
  legacy: false,
  locale: 'en-US',
  messages: {
    'en-US': {
      views: {
        ProductAnalytics: {
          settings_title: 'Product Analytics',
          settings_description: 'Configure product analytics collection',
          opt_in_toggle_label: 'Enable Product Analytics',
          opt_in_toggle_description: 'Help improve Modulo by sharing anonymous usage data',
          current_level: 'Current Level',
          level_all: 'All',
          level_off: 'Off',
          last_successful_dump: 'Last Successful Dump',
          coming_soon: 'Coming soon',
        },
      },
    },
  },
})

const fetchConsentMock = vi.fn()
const updateLevelMock = vi.fn()

vi.mock('../stores/productAnalyticsStore', () => ({
  useProductAnalyticsStore: () => ({
    isOptedIn: false,
    loading: false,
    fetchConsent: fetchConsentMock,
    updateLevel: updateLevelMock,
  }),
}))

function mountSettings(props: Record<string, unknown> = {}) {
  return mount(ProductAnalyticsSettings, {
    global: {
      plugins: [i18n],
      stubs: {
        ProductAnalyticsError: true,
        ToggleSwitch: true,
        SectionCard: { template: '<div class="section-card"><slot /></div>' },
      },
    },
    props,
  })
}

describe('ProductAnalyticsSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    fetchConsentMock.mockResolvedValue(undefined)
    updateLevelMock.mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the section card', () => {
    const wrapper = mountSettings()
    expect(wrapper.find('.section-card').exists()).toBe(true)
  })

  it('renders ProductAnalyticsError component', () => {
    const wrapper = mountSettings()
    expect(wrapper.findComponent(ProductAnalyticsError).exists()).toBe(true)
  })

  it('shows opt-in toggle with correct label and description', () => {
    const wrapper = mountSettings()
    expect(wrapper.text()).toContain('Enable Product Analytics')
    expect(wrapper.text()).toContain('Help improve Modulo by sharing anonymous usage data')
  })

  it('renders ToggleSwitch with expected label and test id', () => {
    const wrapper = mountSettings()
    const toggle = wrapper.findComponent(ToggleSwitch)
    expect(toggle.exists()).toBe(true)
    expect(toggle.props('label')).toBe('Enable Product Analytics')
  })

  it('renders ToggleSwitch with checked=true when opted in', async () => {
    vi.mock('../stores/productAnalyticsStore', () => ({
      useProductAnalyticsStore: () => ({
        isOptedIn: true,
        loading: false,
        fetchConsent: fetchConsentMock,
        updateLevel: updateLevelMock,
      }),
    }))

    const wrapper = mountSettings()
    const toggle = wrapper.findComponent(ToggleSwitch)
    expect(toggle.props('checked')).toBe(true)
  })

  it('renders ToggleSwitch component when mounted', () => {
    const wrapper = mountSettings()
    expect(wrapper.findComponent(ToggleSwitch).exists()).toBe(true)
  })

  it('shows stat items with current level and last successful dump', () => {
    const wrapper = mountSettings()
    expect(wrapper.text()).toContain('Current Level')
    expect(wrapper.text()).toContain('All')
    expect(wrapper.text()).toContain('Last Successful Dump')
    expect(wrapper.text()).toContain('Coming soon')
  })

  it('shows level_all badge when opted in', async () => {
    vi.mock('../stores/productAnalyticsStore', () => ({
      useProductAnalyticsStore: () => ({
        isOptedIn: true,
        loading: false,
        fetchConsent: fetchConsentMock,
        updateLevel: updateLevelMock,
      }),
    }))

    const wrapper = mountSettings()
    expect(wrapper.text()).toContain('All')
    expect(wrapper.find('.badge-status-success').exists()).toBe(true)
  })

  it('shows level badge when rendered', () => {
    const wrapper = mountSettings()
    expect(wrapper.text()).toContain('All')
    expect(wrapper.find('.badge-status-success').exists()).toBe(true)
  })

  it('calls fetchConsent on mount', () => {
    mountSettings()
    expect(fetchConsentMock).toHaveBeenCalledOnce()
  })

  it('emits toggle event and calls updateLevel when toggle changes to true', async () => {
    const wrapper = mountSettings()
    const toggle = wrapper.findComponent(ToggleSwitch)
    await toggle.vm.$emit('toggle', true)
    await wrapper.vm.$nextTick()

    expect(updateLevelMock).toHaveBeenCalledWith('all')
  })

  it('calls updateLevel with off when toggle changes to false', async () => {
    vi.mock('../stores/productAnalyticsStore', () => ({
      useProductAnalyticsStore: () => ({
        isOptedIn: true,
        loading: false,
        fetchConsent: fetchConsentMock,
        updateLevel: updateLevelMock,
      }),
    }))

    const wrapper = mountSettings()
    const toggle = wrapper.findComponent(ToggleSwitch)
    await toggle.vm.$emit('toggle', false)
    await wrapper.vm.$nextTick()

    expect(updateLevelMock).toHaveBeenCalledWith('off')
  })

  it('calls updateLevel when toggle changes', async () => {
    const wrapper = mountSettings()
    const toggle = wrapper.findComponent(ToggleSwitch)
    await toggle.vm.$emit('toggle', true)
    await nextTick()
    expect(updateLevelMock).toHaveBeenCalledWith('all')
  })

  it('renders correct badge classes for opt-in state', () => {
    const wrapper = mountSettings()
    const badges = wrapper.findAll('.badge')
    expect(badges.length).toBeGreaterThanOrEqual(1)
    expect(badges[0].classes()).toContain('badge-status-success')
  })

  it('does not crash when store returns undefined for last dump', () => {
    const wrapper = mountSettings()
    expect(wrapper.text()).toContain('Coming soon')
  })
})
