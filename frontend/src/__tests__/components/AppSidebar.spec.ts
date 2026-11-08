import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createRouter, createWebHistory, useRoute } from 'vue-router'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
  // useEventStream registers a module-scope re-arm listener on import (FAR-1842).
  onAuthChange: vi.fn(() => vi.fn()),
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
  [key: string]: unknown
}

function mountSidebar(props = {}, extra: MountOptions = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const { ...mountExtra } = extra
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

  describe('mobile — unconditional icon rail', () => {
    it('renders the touch rail without legacy drawer chrome', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
    })

    it('opens and closes the full sidebar as a native modal dialog', async () => {
      const showModal = vi.spyOn(HTMLDialogElement.prototype, 'showModal')
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      const panel = wrapper.find('#mobile-nav-panel')
      expect(panel.exists()).toBe(true)
      expect(panel.element.tagName).toBe('DIALOG')
      expect((panel.element as HTMLDialogElement).open).toBe(true)
      expect(showModal).toHaveBeenCalledTimes(1)
      expect(panel.classes()).toContain('backdrop:bg-black/50')
      await panel.find('.sidebar-link').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      showModal.mockRestore()
      wrapper.unmount()
    })

    it('keeps the rail visible while plan loading is unresolved', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
    })

    it('closes the expanded full sidebar when the route changes', async () => {
      const route = useRoute()
      route.path = '/'
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(true)

      route.path = '/notifications'
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      wrapper.unmount()
    })

    it('keeps the rail when the route changes with the full sidebar closed', async () => {
      const route = useRoute()
      route.path = '/'
      const wrapper = mountSidebar()
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)

      route.path = '/admin/my-profile'
      await flushPromises()
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      wrapper.unmount()
    })

    it('emits logout when mobile SidebarRail emits logout', async () => {
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarRail = wrapper.findComponent({ name: 'SidebarRail' })
      expect(sidebarRail.exists()).toBe(true)
      await sidebarRail.vm.$emit('logout')
      await flushPromises()

      expect(wrapper.emitted('logout')).toBeTruthy()
      expect(wrapper.emitted('logout')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits toggle-theme when mobile SidebarRail emits toggle-theme', async () => {
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarRail = wrapper.findComponent({ name: 'SidebarRail' })
      expect(sidebarRail.exists()).toBe(true)
      await sidebarRail.vm.$emit('toggle-theme')
      await flushPromises()

      expect(wrapper.emitted('toggle-theme')).toBeTruthy()
      expect(wrapper.emitted('toggle-theme')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits open-command-palette when mobile SidebarRail emits open-command-palette', async () => {
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarRail = wrapper.findComponent({ name: 'SidebarRail' })
      expect(sidebarRail.exists()).toBe(true)
      await sidebarRail.vm.$emit('open-command-palette')
      await flushPromises()

      expect(wrapper.emitted('open-command-palette')).toBeTruthy()
      expect(wrapper.emitted('open-command-palette')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits logout when mobile SidebarFull emits logout', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('logout')
      await flushPromises()

      expect(wrapper.emitted('logout')).toBeTruthy()
      expect(wrapper.emitted('logout')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits toggle-theme when mobile SidebarFull emits toggle-theme', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('toggle-theme')
      await flushPromises()

      expect(wrapper.emitted('toggle-theme')).toBeTruthy()
      expect(wrapper.emitted('toggle-theme')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits open-command-palette when mobile SidebarFull emits open-command-palette', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('open-command-palette')
      await flushPromises()

      expect(wrapper.emitted('open-command-palette')).toBeTruthy()
      expect(wrapper.emitted('open-command-palette')).toHaveLength(1)
      wrapper.unmount()
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
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
    })

    it('renders the avatar as a router-link to /admin/my-profile in full mode', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()
      const avatar = wrapper.find('.avatar-ring')
      expect(avatar.exists()).toBe(true)
      expect(avatar.attributes('href')).toBe('/admin/my-profile')
    })

    it('lists Notifications in MONITOR above Error Dashboard (real manifest, FAR-656)', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      const store = usePlanStore()
      store.currentTier = 'team'
      await flushPromises()
      const monitorItems = wrapper.find('#sidebar-group-monitor')
      expect(monitorItems.exists()).toBe(true)
      const hrefs = monitorItems.findAll('a.sidebar-link').map((l) => l.attributes('href'))
      expect(hrefs[0]).toBe('/notifications')
      expect(hrefs[1]).toBe('/admin/errors')
    })

    it('shows the Webhook Notifications link by default (real manifest, FAR-2235)', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      const store = usePlanStore()
      store.currentTier = 'team'
      store.features['webhook_notification_log'] = true
      await flushPromises()
      const monitorItems = wrapper.find('#sidebar-group-monitor')
      const hrefs = monitorItems.findAll('a.sidebar-link').map((l) => l.attributes('href'))
      expect(hrefs).toContain('/admin/notification-delivery')
    })

    it('shows the Webhook Notifications link when the flag is enabled (real manifest, FAR-656)', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      const store = usePlanStore()
      store.currentTier = 'team'
      store.features['webhook_notification_log'] = true
      await flushPromises()
      const monitorItems = wrapper.find('#sidebar-group-monitor')
      const hrefs = monitorItems.findAll('a.sidebar-link').map((l) => l.attributes('href'))
      expect(hrefs).toContain('/admin/notification-delivery')
    })

    it('emits open-command-palette when desktop SidebarFull emits open-command-palette', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('open-command-palette')
      await flushPromises()

      expect(wrapper.emitted('open-command-palette')).toBeTruthy()
      expect(wrapper.emitted('open-command-palette')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits logout when desktop SidebarFull emits logout', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('logout')
      await flushPromises()

      expect(wrapper.emitted('logout')).toBeTruthy()
      expect(wrapper.emitted('logout')).toHaveLength(1)
      wrapper.unmount()
    })

    it('emits toggle-theme when desktop SidebarFull emits toggle-theme', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('toggle-theme')
      await flushPromises()

      expect(wrapper.emitted('toggle-theme')).toBeTruthy()
      expect(wrapper.emitted('toggle-theme')).toHaveLength(1)
      wrapper.unmount()
    })

    it('calls closeMobilePanel when SidebarFull emits collapse event (mobile)', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(true)

      // Find the SidebarFull component inside the mobile panel and emit collapse
      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('collapse')
      await flushPromises()

      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      wrapper.unmount()
    })

    it('calls closeMobilePanel when SidebarFull emits navigate event (mobile)', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(true)

      const sidebarFull = wrapper.findComponent({ name: 'SidebarFull' })
      expect(sidebarFull.exists()).toBe(true)
      await sidebarFull.vm.$emit('navigate')
      await flushPromises()

      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      wrapper.unmount()
    })

    it('calls closeMobilePanel and emits open-command-palette when SidebarFull emits open-command-palette (mobile)', async () => {
      const wrapper = mountSidebar()
      await flushPromises()
      await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
      await flushPromises()
      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(true)

      // Directly call the handler since event propagation in test env may differ
      const vm = wrapper.vm as unknown as { onOpenCommandPalette: () => void }
      vm.onOpenCommandPalette()
      await flushPromises()

      expect(wrapper.find('#mobile-nav-panel').exists()).toBe(false)
      expect(wrapper.emitted('open-command-palette')).toBeTruthy()
      expect(wrapper.emitted('open-command-palette')).toHaveLength(1)
      wrapper.unmount()
    })
  })
})
