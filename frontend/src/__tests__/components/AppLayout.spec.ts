import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'
import { nextTick } from 'vue'
import { focusRouteContent } from '../../router'

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn().mockResolvedValue({ data: null, error: undefined }) },
  getAccessToken: vi.fn().mockReturnValue('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhZG1pbkBtb2R1bG8ucnVuIiwib3JnX3JvbGUiOiJhZG1pbiJ9.fakesignature'),
  clearAccessToken: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
  // useEventStream registers a module-scope re-arm listener on import (FAR-1842).
  onAuthChange: vi.fn(() => vi.fn()),
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
  // FAR-1237: the plan store hydrates feature flags from localStorage at
  // creation — clear it so one test's persisted flag map cannot decide the
  // next test's layout.
  localStorage.clear()
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
import { useAssistantStore } from '../../composables/useAssistantStore'
import { MOBILE_NAV_PENDING_TESTID } from '../../composables/useSidebarMode'
import { api, getAccessToken } from '../../lib/api/client'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: { template: '<h1 tabindex="-1" data-page-heading>Dashboard</h1>' } },
    { path: '/next', name: 'next', component: { template: '<h1 tabindex="-1" data-page-heading>Next route</h1>' } },
  ],
})

describe('AppLayout', () => {
  it('focuses the newly entered route heading after out-in navigation', () => {
    const shellMain = document.createElement('main')
    shellMain.dataset.mainLandmark = ''
    const outgoingHeading = document.createElement('h1')
    outgoingHeading.tabIndex = -1
    outgoingHeading.dataset.pageHeading = ''
    outgoingHeading.textContent = 'Outgoing route'
    const enteredHeading = document.createElement('h1')
    enteredHeading.tabIndex = -1
    enteredHeading.dataset.pageHeading = ''
    enteredHeading.textContent = 'Entered route'
    document.body.append(shellMain, outgoingHeading, enteredHeading)
    outgoingHeading.focus()

    outgoingHeading.remove()
    enteredHeading.remove()
    document.body.append(enteredHeading)
    focusRouteContent()

    expect(document.activeElement).toBe(enteredHeading)
    expect(document.activeElement).not.toBe(shellMain)
    expect(document.activeElement).not.toBe(outgoingHeading)
    shellMain.remove()
    outgoingHeading.remove()
    enteredHeading.remove()
  })

  it('falls back to focusing the main landmark when the route has no page heading', () => {
    const shellMain = document.createElement('main')
    shellMain.tabIndex = -1
    shellMain.dataset.mainLandmark = ''
    document.body.append(shellMain)
    ;(document.activeElement as HTMLElement | null)?.blur()

    focusRouteContent()

    expect(document.activeElement).toBe(shellMain)
    shellMain.remove()
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

  describe('mobile — hamburger drawer (flag OFF, default)', () => {
    it('applies pt-14 to main and shows the hamburger header on mobile when the flag is off', async () => {
      mockMatchMedia(false)
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      // Resolve the flag OFF (payload applied): the drawer is the resolved
      // mode here — an unresolved store must render the pending placeholder
      // instead (asserted in the FAR-1237 block below).
      usePlanStore().features['mobile_sidebar_rail'] = false
      // The onboarding banner renders in normal flow and reserves its own
      // height, so its active/dismissed state does not change <main>'s padding
      // class at all. Dismiss it here purely for determinism.
      const onboarding = useOnboardingStore()
      onboarding.dismissed = true
      await nextTick()
      await nextTick()
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-14')
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(true)
    })

    it('keeps header-only padding when the mobile header and the onboarding banner are both active', async () => {
      // Regression pin: the onboarding banner is in normal flow, so it reserves
      // its own height and <main> must NOT add a banner offset. The padding here
      // is identical to the banner-inactive case ("pt-14" on mobile) — any
      // banner term would double-count the gap.
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
      // Pin the premise: the banner must actually be RENDERED for the
      // no-offset assertions below to mean anything. The trigger button lives
      // inside OnboardingBanner's `v-if="store.isActive"` root, so it only
      // exists when the banner is active (same locator the active-banner and
      // inactive-banner tests in this file use).
      expect(wrapper.find('[data-testid="onboarding-banner-trigger"]').exists()).toBe(true)
      const main = wrapper.find('main')
      expect(main.classes()).toContain('pt-14')
      expect(main.classes()).not.toContain('pt-[calc(3.5rem+8.25rem)]')
      expect(main.classes()).not.toContain('pt-[8.25rem]')
      expect(main.classes()).not.toContain('md:pt-20')
    })

    it('adds no padding when the rail (flag ON) replaces the fixed header, even with the banner active', async () => {
      // The rail is in-flow, so there is no fixed header to clear and the
      // in-flow banner needs no offset — the padding matches the
      // banner-inactive rail case (empty). Banner state does not vary padding.
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
      expect(main.classes()).not.toContain('pt-14')
      expect(main.classes()).not.toContain('pt-[8.25rem]')
      expect(main.classes()).not.toContain('pt-[calc(3.5rem+8.25rem)]')
      expect(main.classes()).not.toContain('md:pt-20')
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

  describe('mobile — nav flag unresolved (FAR-1237 pending placeholder)', () => {
    it('renders neither chrome on first paint — pending placeholder + header offset, no hamburger, no rail', async () => {
      mockMatchMedia(false)
      // Hold the plan-bootstrap response so the store is genuinely IN FLIGHT
      // ('none') at assertion time. The store currently reads the flags from
      // `/api/v1/admin/feature-flags`; FAR-1809 (PR #1863) repoints that read
      // to the caller-scoped `/api/v1/plan`, so BOTH paths are held — the
      // test stays genuine on either side of that merge. Without the hold the
      // no-payload mock would settle the store to terminal 'unavailable'
      // (FAR-1805) — the settled case is pinned by the companion test next
      // door. The flushPromises() below makes the discrimination real: it
      // drains the settle chain, so a hold that misses the live path leaves
      // flagsSource 'none' only by timing luck and the assertion fails once
      // the chain settles (proven by mutation: a bogus hold path fails this
      // test).
      ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
        if (path === '/api/v1/admin/feature-flags' || path === '/api/v1/plan') {
          return new Promise(() => {})
        }
        return Promise.resolve({ data: null, error: undefined })
      })
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      const onboarding = useOnboardingStore()
      onboarding.dismissed = true
      // Drain the microtask chain fully: license/tiers settle (no-payload),
      // and the held bootstrap request must STILL leave the store in 'none'.
      // Without this the assertions below could pass on timing luck alone,
      // before an unheld request had time to settle the store.
      await flushPromises()

      expect(usePlanStore().flagsSource).toBe('none')
      const pending = wrapper.find(`[data-testid="${MOBILE_NAV_PENDING_TESTID}"]`)
      expect(pending.exists()).toBe(true)
      // A labelled live region with a non-interactive hamburger affordance, so
      // an attempted tap reads as pending nav rather than a dead blank slot.
      // The native <output> carries the implicit status role (Web:S6819).
      expect(pending.element.tagName).toBe('OUTPUT')
      expect(pending.attributes('role')).toBeUndefined()
      expect(pending.attributes('aria-busy')).toBe('true')
      expect(pending.find('[data-testid="mobile-nav-pending-hamburger"]').exists()).toBe(true)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(false)
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(false)
      // The placeholder occupies the fixed header slot, so the offset matches.
      expect(wrapper.find('main').classes()).toContain('pt-14')
    })

    it("unmounts the placeholder once the fetch settles unreadable — hamburger + drawer render (FAR-1805)", async () => {
      mockMatchMedia(false)
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      const onboarding = useOnboardingStore()
      onboarding.dismissed = true
      await flushPromises()

      // The no-payload mock has settled the store: terminal 'unavailable',
      // logged by the store (console.warn is spied in beforeEach).
      expect(usePlanStore().flagsSource).toBe('unavailable')
      expect(wrapper.find(`[data-testid="${MOBILE_NAV_PENDING_TESTID}"]`).exists()).toBe(false)
      expect(wrapper.find('[aria-controls="mobile-sidebar"]').exists()).toBe(true) // interactive
      expect(wrapper.find('[aria-label="Expand sidebar"]').exists()).toBe(false) // drawer fallback, not rail
      expect(wrapper.find('main').classes()).toContain('pt-14') // header slot still occupied
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

  describe('full-width main content (Assistant panel is an overlay, not a layout column)', () => {
    it('never reserves right-side padding on main, even when the Assistant panel is docked', async () => {
      const wrapper = mount(AppLayout, {
        global: {
          plugins: [createPinia(), router],
          stubs: { LogoMark: true },
        },
      })
      await nextTick()
      await nextTick()
      // The premise: the panel is docked. The old layout
      // bound `paddingRight: panelSize.width`px onto <main> in this state,
      // reserving 440px on every page.
      const assistantStore = useAssistantStore()
      assistantStore.setPanelState('docked')
      expect(assistantStore.panelState).toBe('docked')
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
