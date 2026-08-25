import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import DbCapacityBanner from '../components/DbCapacityBanner.vue'
import { fetchDbCapacity, type DbCapacityInfo } from '../lib/api/dbCapacity'

vi.mock('../lib/api/dbCapacity', () => ({
  fetchDbCapacity: vi.fn(),
}))

const mockedFetch = vi.mocked(fetchDbCapacity)

function base(overrides: Partial<DbCapacityInfo> = {}): DbCapacityInfo {
  return {
    capacity_percent: null,
    mode: 'fixed',
    alert_level: 'ok',
    used_bytes: 0,
    capacity_bytes: 0,
    ...overrides,
  }
}

describe('DbCapacityBanner', () => {
  beforeEach(() => {
    mockedFetch.mockReset()
  })

  async function mountBanner(data: DbCapacityInfo | null) {
    mockedFetch.mockResolvedValue(data)
    const wrapper = mount(DbCapacityBanner)
    await nextTick()
    await nextTick()
    return wrapper
  }

  const bannerTestId = '[data-testid="db-capacity-banner"]'

  it('renders nothing when alert_level is ok', async () => {
    const wrapper = await mountBanner(base({ alert_level: 'ok', capacity_percent: 50, mode: 'fixed' }))
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders nothing when mode is elastic even with a high percent', async () => {
    const wrapper = await mountBanner(base({ mode: 'elastic', alert_level: 'critical', capacity_percent: 99 }))
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders nothing when mode is disabled', async () => {
    const wrapper = await mountBanner(base({ mode: 'disabled', alert_level: 'full', capacity_percent: 99 }))
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders nothing when the endpoint returns null (unavailable / error)', async () => {
    const wrapper = await mountBanner(null)
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders nothing when the percent is null even if the alert level is high', async () => {
    const wrapper = await mountBanner(base({ alert_level: 'critical', capacity_percent: null }))
    expect(wrapper.find(bannerTestId).exists()).toBe(false)
  })

  it('renders the warn banner with role=status', async () => {
    const wrapper = await mountBanner(base({ alert_level: 'warn', capacity_percent: 85, used_bytes: 8_589_934_592, capacity_bytes: 10_737_418_240 }))
    const banner = wrapper.find(bannerTestId)
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('status')
    expect(banner.attributes('aria-live')).toBe('polite')
    expect(banner.attributes('data-alert-level')).toBe('warn')
    expect(wrapper.text()).toContain('85%')
    expect(wrapper.text()).toContain('clear down old runs')
    expect(wrapper.text()).toContain('8 GB of 10 GB used')
    expect(wrapper.text()).toContain('Mode: fixed')
    expect(wrapper.find('[data-testid="db-capacity-run-retention-link"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="db-capacity-housekeeping-link"]').exists()).toBe(true)
  })

  it('renders the critical banner with role=alert', async () => {
    const wrapper = await mountBanner(base({ alert_level: 'critical', capacity_percent: 92 }))
    const banner = wrapper.find(bannerTestId)
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('alert')
    expect(banner.attributes('aria-live')).toBe('assertive')
    expect(banner.attributes('data-alert-level')).toBe('critical')
    expect(wrapper.text()).toContain('92%')
    expect(wrapper.text()).toContain('almost full')
  })

  it('renders the full banner with role=alert and the operator bypass note', async () => {
    const wrapper = await mountBanner(base({ alert_level: 'full', capacity_percent: 99 }))
    const banner = wrapper.find(bannerTestId)
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('role')).toBe('alert')
    expect(banner.attributes('aria-live')).toBe('assertive')
    expect(banner.attributes('data-alert-level')).toBe('full')
    expect(wrapper.text()).toContain('New runs are disabled')
    expect(wrapper.text()).toContain('Operators can bypass this limit')
  })
})
