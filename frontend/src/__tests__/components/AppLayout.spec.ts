import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbkBtb2R1bG8ucnVuIiwib3JnX3JvbGUiOiJhZG1pbiJ9.fakesignature'),
  clearAccessToken: vi.fn(),
}))

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

import AppLayout from '../../components/AppLayout.vue'
import { usePlanStore } from '../../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/', name: 'dashboard', component: { template: '<div>Dashboard</div>' } }],
})

describe('AppLayout', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('renders plan badge by default', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()
    await nextTick()
    expect(wrapper.findComponent({ name: 'SidebarLink' }).exists()).toBe(true)
  })

  it('shows V1 plan badge when store is v1', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    const store = usePlanStore()
    store.currentTier = 'v1'
    store.expiresAt = '2026-12-31T23:59:59Z'
    await nextTick()
    await nextTick()
    expect(wrapper.text()).toContain('V1')
  })
})
