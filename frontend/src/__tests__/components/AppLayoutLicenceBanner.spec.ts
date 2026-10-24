import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'

import AppLayout from '../../components/AppLayout.vue'
import { api, getAccessToken } from '../../lib/api/client'

// Mutable current route so a test can place the shell on a route that is NOT
// the licence page. Hoisted because AppLayout transitively imports the app's
// router module, which builds its router at IMPORT time — before this module's
// body would otherwise have initialised these.
const { mockRoute, routerPush, mockRouter } = vi.hoisted(() => {
  const mockRoute: Record<string, any> = {
    path: '/pipelines',
    fullPath: '/pipelines',
    name: 'pipelines',
    params: {},
    query: {},
    hash: '',
    matched: [],
    meta: {},
  }

  const routerPush = vi.fn(async (target: { name?: string; path?: string }) => {
    if (target?.name) mockRoute.name = target.name
    if (target?.name === 'settings-license') {
      mockRoute.path = '/settings/license'
      mockRoute.fullPath = '/settings/license'
    }
  })

  const mockRouter = {
    install: vi.fn(),
    push: routerPush,
    replace: vi.fn(),
    resolve: vi.fn(),
    go: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    beforeEach: vi.fn(),
    afterEach: vi.fn(),
    onError: vi.fn(),
    getRoutes: vi.fn(() => []),
    addRoute: vi.fn(),
    removeRoute: vi.fn(),
    hasRoute: vi.fn(() => false),
    currentRoute: { value: mockRoute },
    isReady: vi.fn(() => Promise.resolve(true)),
  }

  return { mockRoute, routerPush, mockRouter }
})

// The app router module is imported transitively (useUiCommandExecutor) and
// builds a router at module scope, so createRouter/createWebHistory must exist
// on the mock as well.
vi.mock('vue-router', () => ({
  useRoute: () => mockRoute,
  useRouter: () => mockRouter,
  createRouter: vi.fn(() => mockRouter),
  createWebHistory: vi.fn(() => ({})),
}))

vi.mock('../../lib/api/client', () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
  getAccessToken: vi.fn(),
  clearAccessTokenForLogout: vi.fn(),
  isDemoSession: vi.fn().mockReturnValue(false),
  // useEventStream registers a module-scope re-arm listener on import (FAR-1842).
  onAuthChange: vi.fn(() => vi.fn()),
}))

function tokenFor(payload: Record<string, unknown>): string {
  return `x.${btoa(JSON.stringify(payload))}.y`
}

const ADMIN_TOKEN = tokenFor({ sub: 'admin@modulo.run', org_role: 'admin', is_system_admin: false })
const VIEWER_TOKEN = tokenFor({ sub: 'viewer@modulo.run', org_role: 'viewer', is_system_admin: false })
// A system admin whose ORG role is below admin still holds `org.license.manage`
// (the permission list resolves a system admin at the admin floor) and the
// licence read is system-OR-org admin, so they are in the notice's audience.
const SYSTEM_ADMIN_OPERATOR_TOKEN = tokenFor({ sub: 'sys@modulo.run', org_role: 'operator', is_system_admin: true })

const RENEWAL_DUE = {
  has_license: true,
  tier: 'team',
  features: ['sso'],
  expires_at: '2026-06-20T10:00:00Z',
  org_id: 'org-1',
  renewal_mode: 'manual',
  licence_state: 'renewal_due',
  days_until_expiry: 9,
}

const GRACE = {
  ...RENEWAL_DUE,
  licence_state: 'grace',
  days_until_expiry: -2,
  grace_ends_at: '2026-07-04T10:00:00Z',
}

const ACTIVE = { ...RENEWAL_DUE, licence_state: 'active', days_until_expiry: 300 }

function licenceGets(): number {
  const calls = (api.GET as ReturnType<typeof vi.fn>).mock.calls as unknown[][]
  return calls.filter(([path]) => path === '/api/v1/admin/license').length
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

// The shell's licence audience is derived from the caller's server-resolved
// permission list (`GET /api/v1/auth/permissions`) — the same registry the
// licence endpoints enforce — so every shell mount needs that read answered.
function resolveLicenceWith(
  status: Record<string, unknown>,
  permissions: string[] = ['org.license.manage'],
) {
  ;(api.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) => {
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: status, error: undefined })
    }
    if (path === '/api/v1/auth/permissions') {
      return Promise.resolve({ data: { permissions }, error: undefined })
    }
    return Promise.resolve({ data: null, error: undefined })
  })
}

// The shell's chrome is not under test here; keep only the renewal notice
// mounted for real.
const shellStubs = {
  AppSidebar: true,
  Breadcrumb: true,
  CommandPalette: true,
  SpotlightOverlay: true,
  AssistantPanel: true,
  OnboardingBanner: true,
  DbCapacityBanner: true,
  DemoBanner: true,
  ProductAnalyticsConsentPrompt: true,
}

async function mountShell() {
  const wrapper = mount(AppLayout, {
    global: { plugins: [createPinia()], stubs: shellStubs },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  localStorage.clear()
  mockMatchMedia(true)
  mockRoute.path = '/pipelines'
  mockRoute.fullPath = '/pipelines'
  mockRoute.name = 'pipelines'
  routerPush.mockClear()
  vi.mocked(getAccessToken).mockReturnValue(ADMIN_TOKEN)
  ;(api.GET as ReturnType<typeof vi.fn>).mockReset()
  resolveLicenceWith(RENEWAL_DUE)
})

afterEach(() => {
  vi.restoreAllMocks()
  delete (window as unknown as { matchMedia?: unknown }).matchMedia
})

describe('AppLayout licence renewal notice', () => {
  it('renders the renewal countdown on a route that is not the licence page', async () => {
    const wrapper = await mountShell()

    expect(mockRoute.path).toBe('/pipelines')
    const banner = wrapper.find('[data-testid="licence-renewal-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.attributes('data-renewal-mode')).toBe('manual')
  })

  it('shows the grace dialog in the shell so it reaches every route', async () => {
    resolveLicenceWith(GRACE)

    const wrapper = await mountShell()

    // PrimeVue's Dialog teleports its content to <body>, so it is not part of
    // the wrapper's root tree.
    expect(document.body.querySelector('[data-testid="licence-grace-dialog"]')).not.toBeNull()
    expect(document.body.querySelector('[data-testid="licence-grace-install-action"]')).not.toBeNull()
    wrapper.unmount()
  })

  it('renders nothing once the licence is active again', async () => {
    resolveLicenceWith(ACTIVE)

    const wrapper = await mountShell()

    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-lapsed-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-grace-dialog"]').exists()).toBe(false)
  })

  it('reads the licence status once per shell mount, not per navigation', async () => {
    const wrapper = await mountShell()
    expect(licenceGets()).toBe(1)

    // A navigation renders a different route component inside the same shell;
    // the shell itself is not re-created, so its query is not re-issued. (The
    // licence page reusing that query is pinned in SettingsLicenseView.spec.ts.)
    mockRoute.path = '/settings/license'
    mockRoute.fullPath = '/settings/license'
    mockRoute.name = 'settings-license'
    wrapper.vm.$forceUpdate()
    await flushPromises()
    await nextTick()

    expect(licenceGets()).toBe(1)
  })

  it('does not read the licence status nor show the notice to a principal without org.license.manage', async () => {
    vi.mocked(getAccessToken).mockReturnValue(VIEWER_TOKEN)
    resolveLicenceWith(RENEWAL_DUE, [])

    const wrapper = await mountShell()

    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="licence-lapsed-banner"]').exists()).toBe(false)
    expect(licenceGets()).toBe(0)
  })

  it('shows the notice to a system admin whose org role is below admin, because they hold the permission', async () => {
    // FAR-2104: the audience is the permission the endpoints enforce, not the
    // org role. `GET /api/v1/auth/permissions` resolves a system admin at the
    // admin floor, and `GET /api/v1/admin/license` is system-OR-org admin — so
    // this principal can read the status and act on it (system install / org
    // PUT), and therefore sees the warning.
    vi.mocked(getAccessToken).mockReturnValue(SYSTEM_ADMIN_OPERATOR_TOKEN)

    const wrapper = await mountShell()

    expect(wrapper.find('[data-testid="licence-renewal-banner"]').exists()).toBe(true)
    expect(licenceGets()).toBe(1)
  })

  it('takes an administrator from another route to the licence key input', async () => {
    const input = document.createElement('textarea')
    input.setAttribute('data-testid', 'license-key-input')
    document.body.appendChild(input)

    const wrapper = await mountShell()
    await wrapper.get('[data-testid="licence-renewal-install-action"]').trigger('click')

    expect(routerPush).toHaveBeenCalledWith({ name: 'settings-license' })
    await vi.waitFor(() => {
      expect(document.activeElement).toBe(input)
    })
    wrapper.unmount()
    input.remove()
  })

  it('does not navigate again when the administrator is already on the licence page', async () => {
    mockRoute.name = 'settings-license'
    mockRoute.path = '/settings/license'
    mockRoute.fullPath = '/settings/license'
    const input = document.createElement('textarea')
    input.setAttribute('data-testid', 'license-key-input')
    document.body.appendChild(input)

    const wrapper = await mountShell()
    await wrapper.get('[data-testid="licence-renewal-install-action"]').trigger('click')

    expect(routerPush).not.toHaveBeenCalled()
    await vi.waitFor(() => {
      expect(document.activeElement).toBe(input)
    })
    wrapper.unmount()
    input.remove()
  })

  it('waits for the licence key input to mount before focusing it', async () => {
    const wrapper = await mountShell()
    await wrapper.get('[data-testid="licence-renewal-install-action"]').trigger('click')

    // The licence page renders its textarea only after its own status read
    // resolves, so the shell must poll on later frames rather than focusing a
    // node that does not exist yet.
    const input = document.createElement('textarea')
    input.setAttribute('data-testid', 'license-key-input')
    document.body.appendChild(input)

    await vi.waitFor(() => {
      expect(document.activeElement).toBe(input)
    })
    wrapper.unmount()
    input.remove()
  })

  it('falls back to a timer when requestAnimationFrame is unavailable', async () => {
    const original = window.requestAnimationFrame
    ;(window as unknown as { requestAnimationFrame: unknown }).requestAnimationFrame = undefined

    try {
      const wrapper = await mountShell()
      await wrapper.get('[data-testid="licence-renewal-install-action"]').trigger('click')

      const input = document.createElement('textarea')
      input.setAttribute('data-testid', 'license-key-input')
      document.body.appendChild(input)

      await vi.waitFor(() => {
        expect(document.activeElement).toBe(input)
      })
      wrapper.unmount()
      input.remove()
    } finally {
      window.requestAnimationFrame = original
    }
  })
})
