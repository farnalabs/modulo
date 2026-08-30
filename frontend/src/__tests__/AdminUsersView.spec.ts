import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../lib/api/client', () => ({
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbkBleGFtcGxlLmNvbSJ9.AAA'),
}))

import AdminUsersView from '../views/AdminUsersView.vue'
import { usePlanStore } from '../stores/planStore'

const usersPayload = {
  items: [
    {
      id: 'u1',
      email: 'ada@example.com',
      display_name: 'Ada',
      org_role: 'admin',
      is_active: true,
      auth_provider: 'password',
      created_at: '2026-01-01T00:00:00Z',
      last_login: null,
    },
  ],
  total: 1,
  page: 1,
  page_size: 50,
}

function stubUsersApi() {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => usersPayload,
    }),
  )
}

async function mountView(planPatch: Record<string, unknown>) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.$patch(planPatch)
  const wrapper = mount(AdminUsersView, {
    global: { plugins: [pinia] },
  })
  await flushPromises()
  for (let i = 0; i < 3; i++) {
    await nextTick()
  }
  return wrapper
}

describe('AdminUsersView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without crashing', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = usePlanStore()
    store.$patch({ features: { user_management: true }, currentTier: 'community' })
    const wrapper = mount(AdminUsersView, {
      global: {
        plugins: [pinia],
        stubs: {
          FeatureGate: { template: '<div><slot /></div>' },
        },
      },
    })
    await nextTick()
    expect(wrapper.exists()).toBe(true)
    expect(wrapper.text()).toContain('Users')
  })

  it('renders the users table with no lock overlay on community tier (FAR-462)', async () => {
    stubUsersApi()
    const wrapper = await mountView({ features: { user_management: true }, currentTier: 'community' })

    expect(wrapper.find('[data-testid="feature-gate"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(true)
    expect(wrapper.text()).toContain('ada@example.com')
  })

  it('stays unlocked via the community required-tier fallback when flags have not loaded', async () => {
    stubUsersApi()
    const wrapper = await mountView({ features: {}, currentTier: 'community' })

    expect(wrapper.find('[data-testid="feature-gate-disabled"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="feature-gate-lock"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Users')
  })
})
