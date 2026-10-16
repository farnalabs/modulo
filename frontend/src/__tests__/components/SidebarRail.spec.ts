import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

// Same module mocks as AppSidebar.spec.ts: this file mounts SidebarRail directly
// (whose SidebarNav/SidebarFooter children and the AppSidebar wiring mounts
// below import the API client), while NotificationBell itself is stubbed so the
// size prop the rail hands it is observable at the seam.
vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
  // useEventStream registers a module-scope re-arm listener on import (FAR-1842).
  onAuthChange: vi.fn(() => vi.fn()),
}))

import SidebarRail from '../../components/SidebarRail.vue'
import AppSidebar from '../../components/AppSidebar.vue'
import { usePlanStore } from '../../stores/planStore'

/**
 * NotificationBell is stubbed so this spec can observe WHICH size the rail
 * hands it — the rail's half of the contract. NotificationBell's own rendering
 * of `size="touch"` (h-11 w-11) is asserted by NotificationBell's own spec;
 * the stub mirrors that contract as `data-bell-size` so a touch/default
 * difference is visible here without re-testing the bell's internals.
 */
const NotificationBellStub = {
  name: 'NotificationBell',
  props: { size: { type: String, default: undefined } },
  template: `<div data-testid="rail-bell" :data-bell-size="size ?? 'default'" />`,
}

/**
 * The four rail controls that carry an explicit h-10 w-10 box class at 40x40
 * (named so this spec never claims "all controls" beyond what it checks), plus
 * the avatar link (a third shape: its size comes from .avatar-ring + inner
 * h-7 w-7, ~34px — it has no box class at all at default size).
 */
const SIZED_CONTROLS = [
  { name: 'expand chevron button', selector: '[aria-label="Expand sidebar"]' },
  { name: 'brand link (Modulo)', selector: 'a[aria-label="Modulo"]' },
  { name: 'search button', selector: '[aria-label="Search pages"]' },
  { name: 'theme toggle button', selector: '[aria-label="Toggle theme"]' },
] as const

const AVATAR_SELECTOR = 'a[aria-label="User profile"]'
const BELL_SELECTOR = '[data-testid="rail-bell"]'

const railProps = {
  isSystemAdmin: true,
  userRole: 'admin',
  userPermissions: [],
  userEmail: 'admin@modulo.run',
  userInitial: 'A',
  isLight: true,
}

function mountRail(size?: 'default' | 'touch') {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(SidebarRail, {
    props: { ...railProps, ...(size ? { size } : {}) },
    global: {
      plugins: [pinia],
      stubs: { NotificationBell: NotificationBellStub },
    },
  })
}

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

/** Mount AppSidebar (same recipe as AppSidebar.spec.ts) with the bell stubbed. */
function mountSidebar(props = {}, opts: { mobileRail?: boolean } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePlanStore()
  store.features['mobile_sidebar_rail'] = opts.mobileRail === true
  return mount(AppSidebar, {
    props: { ...railProps, ...props },
    global: {
      plugins: [pinia],
      stubs: { NotificationBell: NotificationBellStub },
    },
  })
}

describe('SidebarRail touch targets', () => {
  beforeEach(() => {
    localStorage.clear()
    delete (window as unknown as { matchMedia?: unknown }).matchMedia
  })

  describe('default size (desktop collapsed rail path)', () => {
    it('keeps h-10 w-10 (40x40) on all four sized controls: expand, brand, search, theme', () => {
      const wrapper = mountRail()
      for (const control of SIZED_CONTROLS) {
        const el = wrapper.find(control.selector)
        expect(el.exists(), `${control.name} must exist`).toBe(true)
        expect(el.classes(), `${control.name} keeps 40x40 in default mode`).toContain('h-10')
        expect(el.classes(), `${control.name} keeps 40x40 in default mode`).toContain('w-10')
        expect(el.classes(), `${control.name} must not be 44px in default mode`).not.toContain('h-11')
        expect(el.classes(), `${control.name} must not be 44px in default mode`).not.toContain('w-11')
      }
    })

    it('leaves the avatar link without any box class in default mode', () => {
      const wrapper = mountRail()
      const avatar = wrapper.find(AVATAR_SELECTOR)
      expect(avatar.exists()).toBe(true)
      expect(avatar.classes()).toContain('avatar-ring')
      expect(avatar.classes()).not.toContain('h-11')
      expect(avatar.classes()).not.toContain('w-11')
    })

    it('renders the notification bell in its default size', () => {
      const wrapper = mountRail()
      const bell = wrapper.find(BELL_SELECTOR)
      expect(bell.exists()).toBe(true)
      expect(bell.attributes('data-bell-size')).toBe('default')
    })
  })

  describe('touch size (mobile rail path)', () => {
    it('widens the four sized controls to h-11 w-11 (44x44): expand, brand, search, theme', () => {
      const wrapper = mountRail('touch')
      for (const control of SIZED_CONTROLS) {
        const el = wrapper.find(control.selector)
        expect(el.exists(), `${control.name} must exist`).toBe(true)
        expect(el.classes(), `${control.name} must be 44px in touch mode`).toContain('h-11')
        expect(el.classes(), `${control.name} must be 44px in touch mode`).toContain('w-11')
        expect(el.classes(), `${control.name} must not stay 40px in touch mode`).not.toContain('h-10')
        expect(el.classes(), `${control.name} must not stay 40px in touch mode`).not.toContain('w-10')
      }
    })

    it('widens the avatar link to h-11 w-11 while keeping its ring', () => {
      const wrapper = mountRail('touch')
      const avatar = wrapper.find(AVATAR_SELECTOR)
      expect(avatar.exists()).toBe(true)
      expect(avatar.classes()).toContain('avatar-ring')
      expect(avatar.classes()).toContain('h-11')
      expect(avatar.classes()).toContain('w-11')
    })

    it('renders the notification bell in its touch size', () => {
      const wrapper = mountRail('touch')
      const bell = wrapper.find(BELL_SELECTOR)
      expect(bell.exists()).toBe(true)
      expect(bell.attributes('data-bell-size')).toBe('touch')
    })

    it('differs from default mode on every assertion target (cannot pass vacuously if the prop were ignored)', () => {
      const def = mountRail()
      const touch = mountRail('touch')

      const sizeSignature = (wrapper: ReturnType<typeof mountRail>) => {
        const controls = SIZED_CONTROLS.map((c) => wrapper.find(c.selector).classes().join(' '))
        controls.push(wrapper.find(AVATAR_SELECTOR).classes().join(' '))
        controls.push(wrapper.find(BELL_SELECTOR).attributes('data-bell-size') ?? '')
        return controls.join('|')
      }

      expect(sizeSignature(touch)).not.toBe(sizeSignature(def))
      // And the bell seam specifically: default 'default' vs touch 'touch'.
      expect(def.find(BELL_SELECTOR).attributes('data-bell-size')).toBe('default')
      expect(touch.find(BELL_SELECTOR).attributes('data-bell-size')).toBe('touch')
    })
  })

  describe('behaviour preserved across both sizes', () => {
    it.each(['default', 'touch'] as const)(
      'keeps aria-labels, nav targets and emits in %s mode',
      async (size) => {
        const wrapper = mountRail(size)
        await flushPromises()

        // The five interactive controls exist with their labels (4 sized + avatar).
        expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(true)
        expect(wrapper.find('a[aria-label="Modulo"]').exists()).toBe(true)
        expect(wrapper.find(AVATAR_SELECTOR).exists()).toBe(true)
        expect(wrapper.find('[aria-label="Search pages"]').exists()).toBe(true)
        expect(wrapper.find('[aria-label="Toggle theme"]').exists()).toBe(true)

        // Router targets preserved.
        expect(wrapper.find('a[aria-label="Modulo"]').attributes('href')).toBe('/')
        expect(wrapper.find(AVATAR_SELECTOR).attributes('href')).toBe('/admin/my-profile')

        // Emits preserved.
        await wrapper.find('[aria-label="Expand sidebar"]').trigger('click')
        expect(wrapper.emitted('expand')).toHaveLength(1)
        await wrapper.find('[aria-label="Search pages"]').trigger('click')
        expect(wrapper.emitted('open-command-palette')).toHaveLength(1)
        await wrapper.find('[aria-label="Toggle theme"]').trigger('click')
        expect(wrapper.emitted('toggle-theme')).toHaveLength(1)
      },
    )
  })

  describe('AppSidebar wiring', () => {
    it('passes touch sizing to the rail on the MOBILE rail path (flag ON)', async () => {
      const wrapper = mountSidebar({}, { mobileRail: true })
      await flushPromises()

      const expand = wrapper.find('[aria-label="Expand sidebar"]')
      expect(expand.exists()).toBe(true)
      expect(expand.classes()).toContain('h-11')
      expect(expand.classes()).toContain('w-11')

      const bell = wrapper.find(BELL_SELECTOR)
      expect(bell.exists()).toBe(true)
      expect(bell.attributes('data-bell-size')).toBe('touch')
    })

    it('keeps default sizing on the DESKTOP collapsed rail (unchanged chrome)', async () => {
      mockMatchMedia(true)
      const wrapper = mountSidebar()
      await flushPromises()

      // Collapse to the rail, then assert the rail's expand control stays 40x40.
      await wrapper.find('[aria-label="Collapse sidebar"]').trigger('click')
      await flushPromises()

      const expand = wrapper.find('[aria-label="Expand sidebar"]')
      expect(expand.exists()).toBe(true)
      expect(expand.classes()).toContain('h-10')
      expect(expand.classes()).toContain('w-10')
      expect(expand.classes()).not.toContain('h-11')

      const bell = wrapper.find(BELL_SELECTOR)
      expect(bell.exists()).toBe(true)
      expect(bell.attributes('data-bell-size')).toBe('default')
    })
  })
})
