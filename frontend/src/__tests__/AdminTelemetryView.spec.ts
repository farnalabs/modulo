import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { Mock } from 'vitest'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    PUT: vi.fn(),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminTelemetryView from '../views/AdminTelemetryView.vue'
import { api } from '../lib/api/client'

function mockGet(enabled: boolean) {
  ;(api.GET as Mock).mockResolvedValue({ data: { enabled }, error: undefined })
}

function mockPut(enabled: boolean) {
  ;(api.PUT as Mock).mockResolvedValue({ data: { enabled }, error: undefined })
}

function mockGetError() {
  ;(api.GET as Mock).mockResolvedValue({ data: undefined, error: { detail: 'forbidden' } })
}

function mockPutError() {
  ;(api.PUT as Mock).mockResolvedValue({ data: undefined, error: { detail: 'failed' } })
}

describe('AdminTelemetryView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the telemetry heading', async () => {
    mockGet(false)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    expect(wrapper.text()).toContain('Telemetry')
  })

  it('loads and displays telemetry status as disabled', async () => {
    mockGet(false)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    const toggle = wrapper.find('[data-testid="admin-telemetry-toggle"]')
    expect(toggle.exists()).toBe(true)
    // Toggle should not be checked when disabled
    expect(toggle.attributes('aria-checked')).toBe('false')
  })

  it('loads and displays telemetry status as enabled', async () => {
    mockGet(true)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    const toggle = wrapper.find('[data-testid="admin-telemetry-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
  })

  it('toggles telemetry on when clicking the toggle while disabled', async () => {
    mockGet(false)
    mockPut(true)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    await wrapper.find('[data-testid="admin-telemetry-toggle"]').trigger('click')
    await flushPromises()
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/telemetry', { body: { enabled: true } })
    const toggle = wrapper.find('[data-testid="admin-telemetry-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('true')
  })

  it('toggles telemetry off when clicking the toggle while enabled', async () => {
    mockGet(true)
    mockPut(false)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    await wrapper.find('[data-testid="admin-telemetry-toggle"]').trigger('click')
    await flushPromises()
    expect(api.PUT).toHaveBeenCalledWith('/api/v1/admin/telemetry', { body: { enabled: false } })
    const toggle = wrapper.find('[data-testid="admin-telemetry-toggle"]')
    expect(toggle.attributes('aria-checked')).toBe('false')
  })

  it('shows error when GET fails', async () => {
    mockGetError()
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    expect(wrapper.text()).toContain('forbidden')
  })

  it('shows error when PUT fails', async () => {
    mockGet(false)
    mockPutError()
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    await wrapper.find('[data-testid="admin-telemetry-toggle"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('failed')
  })

  it('shows what-is-collected information', async () => {
    mockGet(false)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    expect(wrapper.text()).toContain('pipeline run counts')
    expect(wrapper.text()).toContain('Error category')
    expect(wrapper.text()).toContain('features are used')
  })

  it('shows default-off notice', async () => {
    mockGet(false)
    const wrapper = mount(AdminTelemetryView)
    await flushPromises()
    expect(wrapper.text()).toContain('off by default')
  })
})
