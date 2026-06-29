import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: {}, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import EvalProposalsQueueView from '../views/EvalProposalsQueueView.vue'

describe('EvalProposalsQueueView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(EvalProposalsQueueView)
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Eval Proposals Queue')
  })
})
