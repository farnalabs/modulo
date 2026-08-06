import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [], total: 0, next_cursor: null, prev_cursor: null }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminAuditView from '../views/AdminAuditView.vue'
import { api } from '../lib/api/client'
import type { Mock } from 'vitest'

describe('AdminAuditView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminAuditView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Audit Log')
  })

  it('surfaces the chain-break detail from the verify response', async () => {
    ;(api.GET as Mock).mockImplementation(async (url: string) => {
      if (url === '/api/v1/admin/audit/verify') {
        return {
          data: {
            valid: false,
            event_count: 1,
            detail: 'Audit chain break at event 1 (id evt-2): stored previous_hash (bad-hash) does not match',
          },
          error: undefined,
        }
      }
      return { data: { items: [], total: 0, next_cursor: null, prev_cursor: null }, error: undefined }
    })
    const wrapper = mount(AdminAuditView, {
      global: { stubs: { FeatureGate: { template: '<div><slot /></div>' } } },
    })
    await nextTick()
    const verifyBtn = wrapper.find('[data-testid="admin-audit-verify-chain"]')
    if (verifyBtn.exists()) {
      await verifyBtn.trigger('click')
      await nextTick()
    }
    expect(wrapper.text()).toContain('bad-hash')
  })
})
