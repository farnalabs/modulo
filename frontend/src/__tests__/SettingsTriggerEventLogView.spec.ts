import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({
      data: { items: [], total: 0, next_cursor: null },
      error: undefined,
    }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))
vi.mock('../lib/api/schema', () => ({}))

import SettingsTriggerEventLogView from '../views/SettingsTriggerEventLogView.vue'

describe('SettingsTriggerEventLogView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(SettingsTriggerEventLogView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Trigger Event Log')
  })
})
