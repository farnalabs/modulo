import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbkBtb2R1bG8ucnVuIiwib3JnX3JvbGUiOiJhZG1pbiJ9.fakesignature'),
  clearAccessToken: vi.fn(),
}))

function mockMatchMedia(matches: boolean) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  // jsdom has no matchMedia; default the layout to the desktop breakpoint so
  // the expanded sidebar (with the plan badge) renders.
  mockMatchMedia(true)
})

afterEach(() => {
  vi.restoreAllMocks()
  delete (window as unknown as { matchMedia?: unknown }).matchMedia
})

import AppLayout from '../../components/AppLayout.vue'
import { usePlanStore } from '../../stores/planStore'
import { useOnboardingStore } from '../../composables/useOnboarding'

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/', name: 'dashboard', component: { template: '<div>Dashboard</div>' } }],
})

describe('AppLayout', () => {
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

  describe('mobile — hamburger drawer (flag OFF, default)', () => {
    it('applies pt-14 to main and shows the hamburger header on mobile when the flag is off', async () => {
      mockMatchMedia(false)
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await nextTick()
      await nextTick()
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-14')
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(true)
    })
  })

  describe('mobile — icon rail (flag ON)', () => {
    it('omits pt-14 from main and shows the rail on mobile when the flag is on', async () => {
      mockMatchMedia(false)
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      const store = usePlanStore()
      store.features['mobile_sidebar_rail'] = true
      await nextTick()
      await nextTick()
      const main = wrapper.find('main')
      expect(main.classes()).not.toContain('pt-14')
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
    })
  })

  it('keeps the onboarding banner wrapper in normal flow (not an absolute overlay)', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    await nextTick()
    const bannerWrapper = wrapper.find('main > div.relative.z-10')
    expect(bannerWrapper.exists()).toBe(true)
    expect(bannerWrapper.classes()).toContain('relative')
    expect(bannerWrapper.classes()).not.toContain('absolute')
  })

  it('does not render onboarding banner content when the store is inactive', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    const store = useOnboardingStore()
    store.dismissed = true
    await nextTick()
    await nextTick()
    expect(wrapper.find('.onboarding-banner').exists()).toBe(false)
    expect(wrapper.find('[data-testid="onboarding-banner-trigger"]').exists()).toBe(false)
  })

  it('renders onboarding banner in normal flow when the store is active', async () => {
    const wrapper = mount(AppLayout, {
      global: {
        plugins: [createPinia(), router],
        stubs: { LogoMark: true },
      },
    })
    const store = useOnboardingStore()
    store.ready = true
    store.isFirstRun = true
    store.dismissed = false
    await nextTick()
    await nextTick()
    expect(wrapper.find('[data-testid="onboarding-banner-trigger"]').exists()).toBe(true)
    const bannerWrapper = wrapper.find('main > div.relative.z-10')
    expect(bannerWrapper.classes()).toContain('relative')
    expect(bannerWrapper.classes()).not.toContain('absolute')
  })
})
