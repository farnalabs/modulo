import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

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

  it('renders License link in sidebar', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()
    await nextTick()

    const sidebarLinks = wrapper.findAllComponents({ name: 'SidebarLink' })
    expect(sidebarLinks.length).toBeGreaterThan(0)
    const licenseLink = sidebarLinks.find((s) => s.props('label') === 'License')
    expect(licenseLink).toBeTruthy()
    expect(licenseLink!.props('to')).toBe('/settings/license')
  })

  it('shows Free plan badge by default', async () => {
    const pinia = createPinia()
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [pinia, router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Free')
    expect(wrapper.text()).not.toContain('Enterprise')
  })

  it('shows Enterprise plan badge when store is enterprise', async () => {
    const pinia = createPinia()
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [pinia, router],
        stubs: { LogoMark: true },
      },
    })
    const store = usePlanStore()
    store.currentTier = 'enterprise'
    store.expiresAt = '2026-12-31T23:59:59Z'
    await nextTick()
    await nextTick()

    expect(wrapper.text()).toContain('Enterprise')
  })

  it('renders MCP link in sidebar', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()
    await nextTick()

    const sidebarLinks = wrapper.findAllComponents({ name: 'SidebarLink' })
    const mcpLink = sidebarLinks.find((s) => s.props('label') === 'MCP')
    expect(mcpLink).toBeTruthy()
    expect(mcpLink!.props('to')).toBe('/settings/mcp')
  })

  it('renders License badge link that points to settings/license', async () => {
    const pinia = createPinia()
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [pinia, router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()

    const licenseBadgeLinks = wrapper.findAll('a[href="/settings/license"]')
    expect(licenseBadgeLinks.length).toBeGreaterThan(0)
  })
})
