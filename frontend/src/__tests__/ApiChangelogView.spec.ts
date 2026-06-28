import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: [], error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import ApiChangelogView from '../views/ApiChangelogView.vue'

describe('ApiChangelogView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(ApiChangelogView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('API Changelog')
  })
})
