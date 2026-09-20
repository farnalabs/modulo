import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/formatDate', () => ({
  formatDateShortWithTime: (d: Date) => {
    if (Number.isNaN(d.getTime())) return '—'
    return `formatted:${d.toISOString()}`
  },
}))

const mockGet = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => mockGet(...args),
  },
}))

import AdminProductAnalyticsView from '../views/AdminProductAnalyticsView.vue'

describe('AdminProductAnalyticsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: null, error: undefined })
  })

  it('renders the page header with title and subtitle', async () => {
    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: {
            template: '<div><h1>{{ title }}</h1><p>{{ subtitle }}</p></div>',
            props: ['title', 'subtitle'],
          },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Product Analytics')
    expect(wrapper.text()).toContain('Transparency into how product analytics')
  })

  it('shows loading spinner while fetching', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))
    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading-spinner">loading</div>' },
          ErrorAlert: true,
          SectionCard: { template: '<div><slot /></div>', props: ['title'] },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="loading-spinner"]').exists()).toBe(true)
  })

  it('shows error alert when store has an error', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: { status: 500, detail: 'Server Error' },
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: {
            template: '<div data-testid="error-alert">{{ message }}</div>',
            props: ['message', 'onRetry'],
          },
          SectionCard: { template: '<div><slot /></div>', props: ['title'] },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(true)
    })
  })

  it('shows warning banner when warning is not_reaching_farnalabs', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: 'not_reaching_farnalabs',
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('Analytics data is not reaching Farnalabs')
  })

  it('hides warning banner when warning is null', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).not.toContain('Analytics data is not reaching')
  })

  it('renders last dump date and dump count', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: '2026-01-15T10:30:00Z',
        dump_count_total: 42,
        consent_level: 'all',
        instance_enabled: true,
        enforcement_enabled: true,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="last-dump"]').text()).toContain('2026-01-15')
    expect(wrapper.find('[data-testid="dump-count-total"]').text()).toBe('42')
  })

  it('renders dash placeholder when last_successful_dump_at is null', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="last-dump"]').text()).toBe('—')
  })

  it('renders consent level badge as success when consent_level is all', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'all',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="consent-level"]')
    expect(badge.classes()).toContain('badge-status-success')
    expect(badge.text()).toContain('All')
  })

  it('renders consent level badge as muted when consent_level is off', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="consent-level"]')
    expect(badge.classes()).toContain('badge-status-muted')
    expect(badge.text()).toContain('Off')
  })

  it('shows unknown consent level label when consent_level is empty', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: '',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="consent-level"]')
    expect(badge.text()).toContain('Unknown')
  })

  it('renders instance enabled badge', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: true,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="instance-enabled"]')
    expect(badge.classes()).toContain('badge-status-success')
    expect(badge.text()).toContain('Enabled')
  })

  it('renders instance disabled badge', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="instance-enabled"]')
    expect(badge.classes()).toContain('badge-status-muted')
    expect(badge.text()).toContain('Disabled')
  })

  it('renders enforcement active badge', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: true,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="enforcement-enabled"]')
    expect(badge.classes()).toContain('badge-context-purple')
    expect(badge.text()).toContain('Active')
  })

  it('renders enforcement inactive badge', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const badge = wrapper.find('[data-testid="enforcement-enabled"]')
    expect(badge.classes()).toContain('badge-status-muted')
    expect(badge.text()).toContain('Inactive')
  })

  it('calls fetchTransparency on mount', async () => {
    mockGet.mockResolvedValue({ data: null, error: undefined })

    mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: { template: '<div><slot /></div>', props: ['title'] },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/product-analytics/transparency')
  })

  it('displays transparency section cards', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div class="section-card"><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    const sections = wrapper.findAll('.section-card')
    expect(sections.length).toBe(2)
    expect(sections[0].text()).toContain('Delivery Status')
    expect(sections[1].text()).toContain('Consent & Enforcement')
  })

  it('formatDate returns dash for invalid date', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: 'invalid-date-string',
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          SectionCard: {
            template: '<div><h2>{{ title }}</h2><slot /></div>',
            props: ['title'],
          },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(wrapper.find('[data-testid="last-dump"]').text()).toBe('—')
  })

  it('hides transparency data and shows loading/error states exclusively', async () => {
    mockGet.mockReturnValue(new Promise(() => {}))

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: { template: '<div data-testid="loading">loading</div>' },
          ErrorAlert: true,
          SectionCard: { template: '<div><slot /></div>', props: ['title'] },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await nextTick()
    expect(wrapper.find('[data-testid="loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="last-dump"]').exists()).toBe(false)
  })

  it('ErrorAlert retry callback is store.fetchTransparency', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: { status: 500, detail: 'Server Error' },
    })

    const wrapper = mount(AdminProductAnalyticsView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: {
            template: '<div data-testid="error-alert"><button data-testid="retry-btn" @click="onRetry">Retry</button></div>',
            props: ['message', 'onRetry'],
          },
          SectionCard: { template: '<div><slot /></div>', props: ['title'] },
          PageHeader: { template: '<div />', props: ['title', 'subtitle'] },
        },
      },
    })
    await vi.waitFor(() => {
      expect(wrapper.find('[data-testid="error-alert"]').exists()).toBe(true)
    })

    mockGet.mockClear()
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        enforcement_enabled: false,
        warning: null,
      },
      error: undefined,
    })

    await wrapper.find('[data-testid="retry-btn"]').trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/product-analytics/transparency')
  })
})
