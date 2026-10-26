import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

// The view renders the date-only dump watermark via `formatDateShort`. Return
// the local calendar date (no time component) so the spec proves the watermark
// renders without a time and is deterministic across the runner's timezone.
vi.mock('../lib/formatDate', () => ({
  formatDateShort: (d: Date) => {
    if (Number.isNaN(d.getTime())) return '—'
    const month = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${d.getFullYear()}-${month}-${day}`
  },
}))

const mockGet = vi.fn()
const mockPut = vi.fn()

vi.mock('../lib/api/client', () => ({
  api: {
    GET: (...args: unknown[]) => mockGet(...args),
    PUT: (...args: unknown[]) => mockPut(...args),
  },
}))

import AdminProductAnalyticsView from '../views/AdminProductAnalyticsView.vue'

describe('AdminProductAnalyticsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockGet.mockResolvedValue({ data: null, error: undefined })
    mockPut.mockResolvedValue({ data: null, error: undefined })
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
        last_successful_dump_at: '2026-01-15',
        dump_count_total: 42,
        consent_level: 'all',
        instance_enabled: true,
        egress_allowed: true,
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

    const lastDump = wrapper.find('[data-testid="last-dump"]').text()
    // Date-only watermark: the calendar day renders with NO time component.
    expect(lastDump).toBe('2026-01-15')
    expect(wrapper.find('[data-testid="dump-count-total"]').text()).toBe('42')
  })

  it('renders dash placeholder when last_successful_dump_at is null', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
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

  it('renders licence required badge when the licence requires analytics', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: true,
        instance_switch_source: 'licence',
        licence_requires_analytics: true,
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

    const badge = wrapper.find('[data-testid="licence-requires-analytics"]')
    expect(badge.classes()).toContain('badge-context-purple')
    expect(badge.text()).toContain('Required')
  })

  it('renders licence not-required badge and the off-by-default copy', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        instance_switch_source: 'default',
        licence_requires_analytics: false,
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

    const badge = wrapper.find('[data-testid="licence-requires-analytics"]')
    expect(badge.classes()).toContain('badge-status-muted')
    expect(badge.text()).toContain('Not required')
    expect(wrapper.find('[data-testid="instance-switch-source"]').text()).toContain('Off by default')
  })

  it('shows the licence-required explanatory copy and lets the operator turn it off', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: true,
        instance_switch_source: 'licence',
        licence_requires_analytics: true,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    expect(wrapper.find('[data-testid="instance-switch-source"]').text()).toContain(
      'On because your licence requires it',
    )
    // A licence-derived value is not an operator override, so there is nothing
    // to "reset" - the toggle is the way to veto it.
    expect(wrapper.find('[data-testid="instance-switch-reset"]').exists()).toBe(false)

    const toggle = wrapper.find('[data-testid="instance-switch-toggle"]')
    expect((toggle.element as HTMLInputElement).checked).toBe(true)
    // jsdom does not flip a checkbox on ``trigger('change')``; set the DOM
    // state the way a real click would, then fire the handler.
    ;(toggle.element as HTMLInputElement).checked = false
    await toggle.trigger('change')

    expect(mockPut).toHaveBeenCalledWith('/api/v1/product-analytics/instance-switch', {
      body: { enabled: false },
    })
  })

  it('clears an explicit override back to the licence default', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        instance_switch_source: 'operator',
        licence_requires_analytics: true,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    expect(wrapper.find('[data-testid="instance-switch-reset"]').exists()).toBe(true)
    await wrapper.find('[data-testid="instance-switch-reset"]').trigger('click')

    expect(mockPut).toHaveBeenCalledWith('/api/v1/product-analytics/instance-switch', {
      body: { enabled: null },
    })
  })

  it('offers no reset for an environment-sourced switch (the env var owns it)', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: true,
        instance_switch_source: 'environment',
        licence_requires_analytics: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    expect(wrapper.find('[data-testid="instance-switch-source"]').text()).toContain(
      'MODULO_PRODUCT_ANALYTICS_ENABLED',
    )
    // An env-sourced switch has no stored system_config row, so a reset would
    // be a silent backend no-op while the copy claims an effect. Never render it.
    expect(wrapper.find('[data-testid="instance-switch-reset"]').exists()).toBe(false)
    expect((wrapper.find('[data-testid="instance-switch-toggle"]').element as HTMLInputElement).checked).toBe(true)
  })

  it('shows the save error and does not refetch when the switch write fails', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        instance_switch_source: 'operator',
        licence_requires_analytics: false,
        warning: null,
      },
      error: undefined,
    })
    mockPut.mockResolvedValue({ data: undefined, error: { status: 503, detail: 'Unavailable' } })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    mockGet.mockClear()
    const toggle = wrapper.find('[data-testid="instance-switch-toggle"]')
    // A real click flips the DOM before the request fires. The failed save must
    // re-assert the committed value, or the control keeps showing the user's
    // rejected flip.
    ;(toggle.element as HTMLInputElement).checked = true
    await toggle.trigger('change')
    await flushPromises()

    expect(wrapper.find('[data-testid="instance-switch-error"]').exists()).toBe(true)
    expect((toggle.element as HTMLInputElement).checked).toBe(false)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('shows the save error when the switch write throws', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        instance_switch_source: 'default',
        licence_requires_analytics: false,
        warning: null,
      },
      error: undefined,
    })
    // A thrown request (network failure / abort) must surface the same error
    // state as a resolved error envelope, and must not refetch.
    mockPut.mockRejectedValue(new Error('network down'))

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    mockGet.mockClear()
    await wrapper.find('[data-testid="instance-switch-toggle"]').trigger('change')
    await flushPromises()

    expect(wrapper.find('[data-testid="instance-switch-error"]').exists()).toBe(true)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('skips the retired toggle when the view unmounts mid-save', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        instance_switch_source: 'operator',
        licence_requires_analytics: false,
        warning: null,
      },
      error: undefined,
    })
    // Hold the write open so the component can be torn down while it is in
    // flight. Vue nulls template refs on unmount, so the `finally` must skip
    // the detached element rather than throw on it.
    let resolvePut!: (value: unknown) => void
    mockPut.mockReturnValue(
      new Promise((resolve) => {
        resolvePut = resolve
      }),
    )

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    const toggle = wrapper.find('[data-testid="instance-switch-toggle"]')
    ;(toggle.element as HTMLInputElement).checked = true
    await toggle.trigger('change')

    wrapper.unmount()
    resolvePut({ data: null, error: undefined })
    await flushPromises()

    // The save ran to completion (initial fetch + the post-write refetch) and
    // the unmounted toggle was left untouched — no detached-element write.
    expect(mockGet).toHaveBeenCalledTimes(2)
  })

  it('exposes a null view and the unknown level when transparency is absent', async () => {
    // The store leaves ``transparency`` null when the API returns no body. The
    // view's computed projections must tolerate that rather than assume data.
    mockGet.mockResolvedValue({ data: null, error: undefined })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    const vm = wrapper.vm as unknown as { view: unknown; consentLevelLabel: string }
    expect(vm.view).toBeNull()
    expect(vm.consentLevelLabel).toContain('Unknown')
    expect(wrapper.find('[data-testid="consent-level"]').exists()).toBe(false)
  })

  it('renders egress allowed badge when egress_allowed is true', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'all',
        instance_enabled: true,
        egress_allowed: true,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    const badge = wrapper.find('[data-testid="egress-allowed"]')
    expect(badge.classes()).toContain('badge-status-success')
    expect(badge.text()).toBe('Allowed')
  })

  it('renders egress blocked badge when egress_allowed is false', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: null,
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
        egress_allowed: false,
        warning: null,
      },
      error: undefined,
    })

    const wrapper = mount(AdminProductAnalyticsView, {
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
    await nextTick()

    const badge = wrapper.find('[data-testid="egress-allowed"]')
    expect(badge.classes()).toContain('badge-status-muted')
    expect(badge.text()).toBe('Blocked')
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
    expect(sections.length).toBe(3)
    expect(sections[0].text()).toContain('Delivery Status')
    expect(sections[1].text()).toContain('Consent & Egress')
    expect(sections[2].text()).toContain('Instance Switch')
  })

  it('formatDate returns dash for invalid date', async () => {
    mockGet.mockResolvedValue({
      data: {
        last_successful_dump_at: 'invalid-date-string',
        dump_count_total: 0,
        consent_level: 'off',
        instance_enabled: false,
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
        warning: null,
      },
      error: undefined,
    })

    await wrapper.find('[data-testid="retry-btn"]').trigger('click')
    await flushPromises()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/product-analytics/transparency')
  })
})
