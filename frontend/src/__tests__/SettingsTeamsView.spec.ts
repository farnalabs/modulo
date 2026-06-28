import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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

describe('SettingsTeamsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsTeamsView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Teams')
  })
})
