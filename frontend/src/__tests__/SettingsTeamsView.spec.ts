import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/admin/teams') return Promise.resolve({ data: { items: [] }, error: undefined })
      if (url === '/api/v1/admin/users') return Promise.resolve({ data: { items: [] }, error: undefined })
      if (url.startsWith('/api/v1/teams/') && url.endsWith('/members')) return Promise.resolve({ data: { items: [] }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import SettingsTeamsView from '../views/SettingsTeamsView.vue'
import { api } from '../lib/api/client'

describe('SettingsTeamsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsTeamsView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Teams')
  })

  it('shows owned resource count for each team', async () => {
    const teams = [
      { id: 't1', name: 'Engineering', description: null, account_id: 'a1', member_count: 2, owned_resource_count: 4, created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
      { id: 't2', name: 'Design', description: null, account_id: 'a2', member_count: 0, owned_resource_count: 1, created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
    ]
    ;(api.GET as unknown as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/v1/admin/teams') return Promise.resolve({ data: { items: teams }, error: undefined })
      if (url === '/api/v1/admin/users') return Promise.resolve({ data: { items: [] }, error: undefined })
      if (url.startsWith('/api/v1/teams/') && url.endsWith('/members')) return Promise.resolve({ data: { items: [] }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(SettingsTeamsView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await vi.waitFor(() => {
      const counts = wrapper.findAll('[data-testid="settings-teams-owned-resource-count"]')
      expect(counts.length).toBe(2)
    })

    const counts = wrapper.findAll('[data-testid="settings-teams-owned-resource-count"]')
    expect(counts[0].text()).toContain('4')
    expect(counts[1].text()).toContain('1')
  })

  it('renders the team disclosure toggle as a native button with full disclosure semantics', async () => {
    const teams = [
      { id: 't1', name: 'Engineering', description: null, account_id: 'a1', member_count: 2, owned_resource_count: 4, created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z' },
    ]
    ;(api.GET as unknown as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/api/v1/admin/teams') return Promise.resolve({ data: { items: teams }, error: undefined })
      if (url === '/api/v1/admin/users') return Promise.resolve({ data: { items: [] }, error: undefined })
      if (url.startsWith('/api/v1/teams/') && url.endsWith('/members')) return Promise.resolve({ data: { items: [] }, error: undefined })
      return Promise.resolve({ data: null, error: undefined })
    })

    const wrapper = mount(SettingsTeamsView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })

    await vi.waitFor(() => expect(wrapper.find('[data-testid="settings-teams-toggle-t1"]').exists()).toBe(true))
    const toggle = wrapper.find('[data-testid="settings-teams-toggle-t1"]')

    expect(toggle.element.tagName).toBe('BUTTON')
    expect(toggle.attributes('type')).toBe('button')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe('settings-teams-panel-t1')

    await toggle.trigger('click')
    await nextTick()

    const panel = wrapper.find('#settings-teams-panel-t1')
    expect(panel.exists()).toBe(true)
    expect(panel.element.tagName).toBe('SECTION')
    expect(panel.attributes('aria-label')).toBe('Engineering details')
  })
})
