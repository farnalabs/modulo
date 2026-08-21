import { describe, it, expect, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick as vueNextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'

async function nextTick() { await vueNextTick(); await flushPromises() }

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockResolvedValue({ data: { items: [] }, error: undefined }),
    POST: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    PATCH: vi.fn().mockResolvedValue({ data: null, error: undefined }),
    DELETE: vi.fn().mockResolvedValue({ response: { status: 204, ok: true }, error: undefined }),
  },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

import AdminEnvironmentProfilesView from '../views/AdminEnvironmentProfilesView.vue'

describe('AdminEnvironmentProfilesView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    const store = usePlanStore()
    store.$patch({ features: { environment_profiles: true }, currentTier: 'team' })
  })

  it('renders without crashing', async () => {
    const wrapper = mount(AdminEnvironmentProfilesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          LockIcon: true,
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Environment Profiles')
  })

  it('shows create profile button', async () => {
    const wrapper = mount(AdminEnvironmentProfilesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          LockIcon: true,
        },
      },
    })
    await nextTick()
    const btn = wrapper.find('[data-testid="admin-envprofiles-add"]')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toBe('Create Profile')
  })

  it('shows empty state when no profiles exist', async () => {
    const wrapper = mount(AdminEnvironmentProfilesView, {
      global: {
        stubs: {
          LoadingSpinner: true,
          ErrorAlert: true,
          LockIcon: true,
        },
      },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('No environment profiles configured')
  })
})
