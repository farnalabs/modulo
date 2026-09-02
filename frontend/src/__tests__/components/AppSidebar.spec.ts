import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory, useRoute } from 'vue-router'
import type { RouteLocationNormalizedLoaded } from 'vue-router'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
}))

vi.mock('vue-router', async () => {
  const { reactive } = await import('vue')
  const route = reactive({
    path: '/',
    fullPath: '/',
    params: {},
    query: {},
    hash: '',
    matched: [],
    name: 'dashboard',
    redirectedFrom: undefined,
    meta: {},
  })
  const router = {
    install: vi.fn(),
    push: vi.fn(),
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    currentRoute: { value: route },
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    isReady: vi.fn(() => Promise.resolve(true)),
  }
  return {
    useRoute: () => route,
    useRouter: () => router,
    createRouter: () => router,
    createWebHistory: () => ({}),
  }
})

import AppSidebar from '../../components/AppSidebar.vue'
import { usePlanStore } from '../../stores/planStore'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: { template: '<div>Dashboard</div>' } },
    { path: '/admin/my-profile', name: 'my-profile', component: { template: '<div>Profile</div>' } },
    { path: '/notifications', name: 'notifications', component: { template: '<div>Notifications</div>' } },
  ],
})

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

interface MountOptions {
  mobileRail?: boolean
  [key: string]: unknown
}

function mountSidebar(props = {}, extra: MountOptions = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  if (extra.mobileRail) {
    store.features['mobile_sidebar_rail'] = true
  }
  const { mobileRail: _mobileRail, ...mountExtra } = extra
  return mount(AppSidebar, {
    props: {
      isSystemAdmin: true,
      userRole: 'admin',
      userPermissions: [],
      userEmail: 'admin@modulo.run',
      userInitial: 'A',
      isLight: true,
      ...props,
    },
    global: {
      plugins: [pinia, router],
      stubs: {
        SvgIcon: { template: '<span class="svg-stub" />' },
      },
    },
    ...mountExtra,
  })
}

describe('AppSidebar', () => {
  beforeEach(() => {
    localStorage.clear()
    delete (window as unknown as { matchMedia?: unknown }).matchMedia
  })

  describe('mobile — hamburger drawer (flag OFF, default)', () => {
    it('renders the hamburger drawer by default on mobile when the flag is off', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(false)
    })

    it('opens the mobile drawer when the hamburger button is clicked', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-sidebar').classes()).toContain('translate-x-0')
      expect(wrapper.find('div[aria-hidden="true"].fixed.inset-0').exists()).toBe(true)
    })

    it('closes the mobile drawer when the backdrop is clicked', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()
      await wrapper.find('div[aria-hidden="true"].fixed.inset-0').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-sidebar').classes()).toContain('-translate-x-full')
      expect(wrapper.find('div[aria-hidden="true"].fixed.inset-0').exists()).toBe(false)
    })

    it('closes the mobile drawer on Escape and returns focus to the hamburger button', async () => {
      const wrapper = mountSidebar({}, { attachTo: document.body })
      await flushPromises()
      const button = wrapper.find('[aria-controls="mobile-sidebar"]')
      await button.trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-sidebar').classes()).toContain('translate-x-0')

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await flushPromises()

      expect(wrapper.find('#mobile-sidebar').classes()).toContain('-translate-x-full')
      expect(document.activeElement).toBe(button.element)

      wrapper.unmount()
    })

    it('closes the mobile drawer when a nav item is activated', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()
      const drawer = wrapper.find('#mobile-sidebar')
      await drawer.find('.sidebar-link').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-sidebar').classes()).toContain('-translate-x-full')
    })

    it('renders the profile link as a router-link to /admin/my-profile in the drawer', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      const link = wrapper.find('#mobile-sidebar').find('a')
      expect(link.attributes('href')).toBe('/admin/my-profile')
      const avatar = wrapper.find('#mobile-sidebar').find('.avatar-ring')
      expect(avatar.exists()).toBe(true)
    })

    it('traps focus within the mobile drawer while it is open', async () => {
      const wrapper = mountSidebar({}, { attachTo: document.body })
      await flushPromises()
      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()

      const drawer = wrapper.find('#mobile-sidebar')
      const focusables = drawer.findAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      expect(focusables.length).toBeGreaterThan(0)
      const first = focusables[0].element as HTMLElement
      const last = focusables[focusables.length - 1].element as HTMLElement

      last.focus()
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }))
      await flushPromises()
      expect(document.activeElement).toBe(first)

      first.focus()
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true }))
      await flushPromises()
      expect(document.activeElement).toBe(last)

      wrapper.unmount()
    })

    it('marks the closed drawer inert and hidden from the a11y tree', async () => {
      // jsdom reflects the inert IDL property inconsistently (reading `.inert`
      // returns undefined, and an open drawer serialises `inert="false"`), so
      // assert the effective inert state from the attribute instead. In a real
      // browser `:inert="false"` removes the attribute entirely.
      const isInert = (el: Element) => {
        const attr = el.getAttribute('inert')
        return attr !== null && attr !== 'false'
      }

      const wrapper = mountSidebar()
      await flushPromises()
      const closedDrawer = wrapper.find('#mobile-sidebar')
      expect(isInert(closedDrawer.element)).toBe(true)
      expect(closedDrawer.attributes('aria-hidden')).toBe('true')
      expect(closedDrawer.classes()).toContain('invisible')

      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()
      const openDrawer = wrapper.find('#mobile-sidebar')
      expect(isInert(openDrawer.element)).toBe(false)
      expect(openDrawer.attributes('aria-hidden')).toBeUndefined()
      expect(openDrawer.classes()).not.toContain('invisible')
    })

    it('makes the non-hamburger header controls inert while the drawer is open', async () => {
      const isInert = (el: Element) => {
        const attr = el.getAttribute('inert')
        return attr !== null && attr !== 'false'
      }

      const wrapper = mountSidebar()
      await flushPromises()
      const controls = wrapper.find('header > div.ml-auto')
      expect(controls.exists()).toBe(true)
      expect(isInert(controls.element)).toBe(false)

      await wrapper.find('[aria-controls="mobile-sidebar"]').trigger('click')
      await flushPromises()
      expect(isInert(controls.element)).toBe(true)

      // The hamburger button stays OUTSIDE the inert wrapper so the drawer can
      // be closed even while the other header controls are inert.
      const hamburger = wrapper.find('[aria-controls="mobile-sidebar"]')
      expect(hamburger.element.closest('[inert]')).toBeNull()

      wrapper.unmount()
    })

    it('localises the search button title via i18n', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      const searchBtn = wrapper.find('button[aria-label="Search pages"]')
      expect(searchBtn.exists()).toBe(true)
      const modifier = navigator.platform.includes('Mac') ? 'Cmd' : 'Ctrl'
      expect(searchBtn.attributes('title')).toBe(`Search pages... (${modifier}+K)`)
    })
  })

  describe('mobile — icon rail + overlay panel (flag ON, experimental)', () => {
    it('shows the collapsed icon rail by default when the flag is on', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[aria-label="Collapse sidebar"]').exists()).toBe(false)
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
    })

    it('opens the mobile overlay panel when the rail expand button is clicked', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
      expect(wrapper.find('div[aria-hidden="true"].fixed.inset-0').exists()).toBe(true)
    })

    it('closes the mobile panel when the backdrop is clicked', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      await wrapper.find('div[aria-hidden="true"].fixed.inset-0').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    })

    it('closes the mobile panel on Escape', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    })

    it('closes the mobile panel when a nav item is activated', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      const dialog = wrapper.find('[role="dialog"]')
      await dialog.find('.sidebar-link').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    })

    it('renders the avatar as a router-link to /admin/my-profile in rail mode', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      const avatar = wrapper.find('.avatar-ring')
      expect(avatar.exists()).toBe(true)
      expect(avatar.attributes('href')).toBe('/admin/my-profile')
    })

    it('returns focus to the rail expand trigger when the mobile panel closes', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true, attachTo: document.body })
      await flushPromises()

      const expandButton = wrapper.find('[aria-label="Expand sidebar"]')
      await expandButton.trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(true)

      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      await flushPromises()

      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
      expect(document.activeElement).toBe(expandButton.element)

      wrapper.unmount()
    })

    it('traps focus within the mobile dialog while it is open', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true, attachTo: document.body })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()

      const dialog = wrapper.find('[role="dialog"]')
      const focusables = dialog.findAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )
      expect(focusables.length).toBeGreaterThan(0)
      const first = focusables[0].element as HTMLElement
      const last = focusables[focusables.length - 1].element as HTMLElement

      last.focus()
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }))
      await flushPromises()
      expect(document.activeElement).toBe(first)

      first.focus()
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true }))
      await flushPromises()
      expect(document.activeElement).toBe(last)

      wrapper.unmount()
    })

    it('closes the mobile panel when the route changes while it is open', async () => {
      const route = vi.mocked(useRoute)() as RouteLocationNormalizedLoaded
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="dialog"]').exists()).toBe(true)

      route.path = '/notifications'
      await flushPromises()

      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

      route.path = '/'
    })
  })

  describe('desktop — in-flow collapsible sidebar', () => {
    it('shows the full sidebar on desktop and collapses to the rail', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('[aria-label="Collapse sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(false)

      await wrapper.find('[aria-label="Collapse sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(localStorage.getItem('sidebar-collapsed')).toBe('true')

      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('[aria-label="Collapse sidebar"]').exists()).toBe(true)
      expect(localStorage.getItem('sidebar-collapsed')).toBe('false')
    })

    it('does not render mobile chrome on desktop', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
      expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    })

    it('renders the avatar as a router-link to /admin/my-profile in full mode', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()
      const avatar = wrapper.find('.avatar-ring')
      expect(avatar.exists()).toBe(true)
      expect(avatar.attributes('href')).toBe('/admin/my-profile')
    })
  })
})
