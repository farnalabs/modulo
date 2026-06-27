import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsRateLimitsView from '../views/SettingsRateLimitsView.vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
  },
}))

import { api } from '../lib/api/client'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SettingsRateLimitsView', () => {
  it('renders the heading', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: { mode: 'in_memory', rules: [] },
      error: undefined,
    } as never)
    const wrapper = mount(SettingsRateLimitsView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Rate Limits')
  })

  it('displays rules in a table', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: {
        mode: 'redis',
        rules: [
          { path_prefix: '/api/v1/chat', max_requests: 100, window_s: 60 },
          { path_prefix: '/api/v1/admin', max_requests: 500, window_s: 300 },
        ],
      },
      error: undefined,
    } as never)
    const wrapper = mount(SettingsRateLimitsView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('/api/v1/chat')
    expect(wrapper.text()).toContain('100')
    expect(wrapper.text()).toContain('60')
    expect(wrapper.text()).toContain('/api/v1/admin')
    expect(wrapper.text()).toContain('500')
    expect(wrapper.text()).toContain('300')
    expect(wrapper.text()).toContain('Redis')
  })

  it('shows empty state when no rules', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: { mode: 'in_memory', rules: [] },
      error: undefined,
    } as never)
    const wrapper = mount(SettingsRateLimitsView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('No rate limit rules configured')
  })

  it('shows error state on API failure', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: undefined,
      error: 'Network error',
    } as never)
    const wrapper = mount(SettingsRateLimitsView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Failed to load rate limits')
  })

  it('shows Retry button on error', async () => {
    vi.mocked(api.GET).mockResolvedValue({
      data: undefined,
      error: 'Network error',
    } as never)
    const wrapper = mount(SettingsRateLimitsView)
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('Retry')
  })
})
