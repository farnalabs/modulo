import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PUT: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

vi.mock('../lib/api/schema', () => ({}))

import SettingsTriggersView from '../views/SettingsTriggersView.vue'

const dialogStubs = ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle']

describe('SettingsTriggersView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 100 }, error: undefined })

    const wrapper = mount(SettingsTriggersView, {
      global: { stubs: dialogStubs },
    })
    await nextTick()
    await nextTick()
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Triggers')
  })

  it('shows loading spinner initially', async () => {
    const { api } = await import('../lib/api/client');
    (api.GET as any).mockReturnValue(new Promise(() => {}))

    const wrapper = mount(SettingsTriggersView, {
      global: { stubs: dialogStubs },
    })
    await nextTick()
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })
})
