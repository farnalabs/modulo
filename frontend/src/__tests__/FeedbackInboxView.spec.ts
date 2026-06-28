import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((url: string) => {
      if (url === '/api/v1/pipelines') return Promise.resolve({ data: { items: [] }, error: undefined })
      return Promise.resolve({ data: { items: [] }, error: undefined })
    }),
    POST: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import FeedbackInboxView from '../views/FeedbackInboxView.vue'

describe('FeedbackInboxView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(FeedbackInboxView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Feedback Inbox')
  })
})
