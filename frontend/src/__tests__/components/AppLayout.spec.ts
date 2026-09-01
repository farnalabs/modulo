import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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
  // afterEach calls vi.restoreAllMocks(), which resets the api.GET mock's
  // implementation to a no-op. Re-establish it here so every test (AppLayout
  // mounts ProductAnalyticsConsentPrompt, which fetches consent on mount) gets
  // a resolvable promise rather than undefined.
  ;(api.GET as ReturnType<typeof vi.fn>).mockResolvedValue({ data: null, error: undefined })
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
import { useRemyStore } from '../../composables/useRemyStore'
import { api, getAccessToken } from '../../lib/api/client'

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
      // The mocked onboarding API resolves ready=true with isFirstRun defaulting
      // to true, so the banner would otherwise be active. Dismiss it to isolate
      // the header-only offset path this test exercises.
      const onboarding = useOnboardingStore()
      onboarding.dismissed = true
      await nextTick()
      await nextTick()
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-14')
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(true)
    })

    it('combines header + banner offsets into one additive padding class when both are active on mobile', async () => {
      mockMatchMedia(false)
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
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-[calc(3.5rem+8.25rem)]')
      expect(main.classes()).not.toContain('pt-14')
      expect(main.classes()).not.toContain('pt-[8.25rem]')
    })

    it('uses banner-only padding on mobile when the rail header (flag ON) replaces the fixed header', async () => {
      mockMatchMedia(false)
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      const planStore = usePlanStore()
      planStore.features['mobile_sidebar_rail'] = true
      const onboarding = useOnboardingStore()
      onboarding.ready = true
      onboarding.isFirstRun = true
      onboarding.dismissed = false
      await nextTick()
      await nextTick()
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-[8.25rem]')
      expect(main.classes()).not.toContain('pt-14')
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

  describe('theme toggle — mutually-exclusive dark/light invariant (FAR-330)', () => {
    beforeEach(() => {
      // Boot state: <html> starts with class="dark" (dark by default, ADR 024).
      document.documentElement.setAttribute('class', 'dark')
    })

    afterEach(() => {
      document.documentElement.setAttribute('class', '')
      document.documentElement.removeAttribute('style')
    })

    function findToggle(wrapper: ReturnType<typeof mount>) {
      const toggle = wrapper.find('.toggle-switch input[type="checkbox"]')
      expect(toggle.exists()).toBe(true)
      return toggle
    }

    it('recovers from a corrupt both-classes-present state to a single valid mode', async () => {
      // Simulate the state the old toggle()/toggle() implementation could
      // produce (both `.dark` and `.light` present). The mutually-exclusive
      // implementation must collapse this back to exactly one valid mode.
      document.documentElement.setAttribute('class', 'dark light')

      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await nextTick()
      await nextTick()

      // The checkbox reflects `isLight`; in the corrupt state both classes are
      // present so it starts checked. Flip it to drive a real `change` that
      // fires toggleTheme.
      await findToggle(wrapper).setValue(false)
      const classes = Array.from(document.documentElement.classList)
      expect(classes).toContain('light')
      expect(classes).not.toContain('dark')
      expect(classes.filter(c => c === 'dark' || c === 'light')).toHaveLength(1)
    })

    it('toggles from dark to light, then back to dark, never holding both classes', async () => {
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await nextTick()
      await nextTick()

      expect(document.documentElement.classList.contains('dark')).toBe(true)
      expect(document.documentElement.classList.contains('light')).toBe(false)

      // First toggle: dark -> light (remove .dark, add .light).
      await findToggle(wrapper).setValue(true)
      expect(document.documentElement.classList.contains('light')).toBe(true)
      expect(document.documentElement.classList.contains('dark')).toBe(false)

      // Second toggle: light -> dark (remove .light, add .dark).
      await findToggle(wrapper).setValue(false)
      expect(document.documentElement.classList.contains('dark')).toBe(true)
      expect(document.documentElement.classList.contains('light')).toBe(false)

      // The both-classes-present invariant must never hold — the exact bug the
      // old toggle()/toggle() implementation could produce.
      const both = document.documentElement.classList.contains('light')
        && document.documentElement.classList.contains('dark')
      expect(both).toBe(false)
    })
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

  describe('full-width main content (Remy panel is an overlay, not a layout column)', () => {
    it('never reserves right-side padding on main, even when the Remy panel is docked', async () => {
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await nextTick()
      await nextTick()
      // The premise: the panel is docked (the store default). The old layout
      // bound `paddingRight: panelSize.width`px onto <main> in this state,
      // reserving 440px on every page.
      expect(useRemyStore().panelState).toBe('docked')
      const main = wrapper.find('main')
      expect(main.attributes('style')).toBeUndefined()
      expect(main.element.style.paddingRight).toBe('')
    })
  })

  describe('DbCapacityBanner admin gating', () => {
    const DB_CAPACITY_PATH = '/api/v1/admin/db-capacity'

    beforeEach(() => {
      // api.GET call history accumulates across the whole file (the banner
      // mounts in earlier tests too); isolate each gating assertion.
      vi.mocked(api.GET).mockClear()
    })

    function jwtWithClaims(claims: Record<string, unknown>): string {
      const encode = (o: object) =>
        btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
      return `${encode({ alg: 'HS256', typ: 'JWT' })}.${encode(claims)}.sig`
    }

    function dbCapacityCalls(): unknown[][] {
      return vi.mocked(api.GET).mock.calls.filter((c) => c[0] === DB_CAPACITY_PATH)
    }

    it('polls db-capacity for an org admin (role claim)', async () => {
      vi.mocked(getAccessToken).mockReturnValue(jwtWithClaims({ sub: 'a@modulo.run', org_role: 'admin' }))
      mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await flushPromises()
      expect(dbCapacityCalls()).toHaveLength(1)
    })

    it('polls db-capacity for a system admin (is_system_admin claim)', async () => {
      vi.mocked(getAccessToken).mockReturnValue(
        jwtWithClaims({ sub: 'a@modulo.run', org_role: 'member', is_system_admin: true }),
      )
      mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await flushPromises()
      expect(dbCapacityCalls()).toHaveLength(1)
    })

    it('never fetches db-capacity for a non-admin (no request, no console 401)', async () => {
      vi.mocked(getAccessToken).mockReturnValue(jwtWithClaims({ sub: 'u@modulo.run', org_role: 'member' }))
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await flushPromises()
      expect(dbCapacityCalls()).toHaveLength(0)
      expect(wrapper.find('[data-testid="db-capacity-banner"]').exists()).toBe(false)
    })
  })
})
