import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'

// FAR-1797: the router looks a route up by `to.name` in `manifestByName`
// (router/index.ts), so the router route NAME must equal the REAL manifest
// route `name:` — a mismatch makes hydrateManifestMeta early-return and
// silently drops requiresSystemAdmin. The mock manifest below cannot prove
// that binding (it compares two literals in this file), so the binding test
// reads the REAL manifest from disk — the same approach as manifestGating.spec.ts.
const REAL_MANIFEST = yaml.load(
  readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
) as { routes: Record<string, { name?: string; system_admin_only?: boolean }> }

// The three route-entry system-admin gates FAR-1797 pins. Used as the
// vacuity guard so the real-manifest loop cannot silently become a no-op.
const EXPECTED_GATED_PATHS = [
  '/admin/system/orgs',
  '/admin/system/config',
  '/admin/product-analytics',
] as const

// Restore the REAL vue-router (the shared vitest setup mocks it globally with a
// stub whose getRoutes() is always []) so the default router export carries its
// real route records — same override pattern as routerFeatureFlagGuard.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    build: { label: 'BUILD', order: 1, default_expanded: true },
    admin: { label: 'ADMIN', order: 5, default_expanded: false },
    system: { label: 'SYSTEM', order: 6, default_expanded: false, system_admin_only: true },
  },
  routes: {
    // FAR-2104: the licence is org-manageable, so it is NOT in the system group
    // and is gated by the permission the licence endpoints enforce.
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'admin', sidebar_order: 12, type: 'form_page', required_tier: 'community', required_roles: null, required_permissions: ['org.license.manage'] },
    '/admin/housekeeping': { name: 'admin-housekeeping', breadcrumb: 'Housekeeping', sidebar_group: 'system', sidebar_order: 6, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/feature-flags': { name: 'admin-feature-flags', breadcrumb: 'Feature Flags', sidebar_group: 'system', sidebar_order: 4, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    // Route-level system-admin gate with NO system-admin sidebar group — the
    // route entry itself carries the signal (the manifest-side replacement for
    // an inline router meta).
    '/admin/system/orgs': { name: 'admin-system-orgs', breadcrumb: 'Organisations', sidebar_group: null, sidebar_order: null, system_admin_only: true, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    // FAR-1797: the other two route-entry system-admin gates, pinned so the
    // router route name ↔ manifest route name binding is asserted, not assumed.
    '/admin/system/config': { name: 'admin-system-config', breadcrumb: 'System Config', sidebar_group: null, sidebar_order: null, system_admin_only: true, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/product-analytics': { name: 'admin-product-analytics', breadcrumb: 'Product Analytics', sidebar_group: null, sidebar_order: null, system_admin_only: true, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    // A normal grouped route (non-system sidebar group) — must stay ungated.
    '/library': { name: 'library', breadcrumb: 'Library', sidebar_group: 'build', sidebar_order: 2, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
    '/': { name: 'dashboard', breadcrumb: 'Dashboard', sidebar_group: 'build', sidebar_order: 1, type: 'page', required_tier: null, required_roles: null, required_permissions: null, exact: true },
  },
}))

vi.mock('@/manifest.yaml', () => ({
  default: mockManifest,
}))

const { decodeJwtPayload, usePlanStore } = vi.hoisted(() => ({
  decodeJwtPayload: vi.fn(),
  usePlanStore: vi.fn(),
}))

vi.mock('../lib/jwt', () => ({ decodeJwtPayload }))
vi.mock('../stores/planStore', () => ({ usePlanStore }))

import { hydrateManifestMeta, enforceRoleTierVisibility } from '../router'
import router from '../router'

function makeRoute(name: string, meta: Record<string, unknown> = {}): any {
  return { name, meta }
}

const planStore = {
  loaded: true,
  features: {} as Record<string, unknown>,
  devMode: false,
  isAtMinimumTier: vi.fn(() => true),
  featureEnabled: vi.fn(() => false),
  fetchPlan: vi.fn(async () => {}),
}

beforeEach(() => {
  vi.clearAllMocks()
  planStore.loaded = true
  planStore.features = {}
  planStore.devMode = false
  planStore.isAtMinimumTier.mockReturnValue(true)
  planStore.featureEnabled.mockReturnValue(false)
  planStore.fetchPlan.mockResolvedValue(undefined)
  usePlanStore.mockReturnValue(planStore)
  decodeJwtPayload.mockReturnValue(null)
})

describe('FAR-938: SYSTEM sidebar group system_admin_only guard', () => {
  describe('hydrateManifestMeta derives requiresSystemAdmin from manifest', () => {
    it('does NOT set requiresSystemAdmin for the license route (FAR-2104: org-manageable)', () => {
      const route = makeRoute('settings-license')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBeUndefined()
      expect(route.meta.requiredPermissions).toEqual(['org.license.manage'])
    })

    it('sets requiresSystemAdmin=true for housekeeping (was previously false)', () => {
      const route = makeRoute('admin-housekeeping')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBe(true)
    })

    it('sets requiresSystemAdmin=true for feature-flags', () => {
      const route = makeRoute('admin-feature-flags')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBe(true)
    })

    it('does NOT set requiresSystemAdmin for a non-system-group route', () => {
      const route = makeRoute('dashboard')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBeUndefined()
    })

    it('sets requiresSystemAdmin=true from the route-level system_admin_only field (no system sidebar group)', () => {
      const route = makeRoute('admin-system-orgs')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBe(true)
    })

    it('does NOT set requiresSystemAdmin for a normal grouped route (non-system sidebar group)', () => {
      const route = makeRoute('library')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBeUndefined()
    })
  })

  describe('enforceRoleTierVisibility blocks non-system-admins from system routes', () => {
    it('redirects a system-group route when the token is not a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: false })
      const result = await enforceRoleTierVisibility(
        makeRoute('admin-feature-flags', { requiresSystemAdmin: true }),
        'tok',
      )
      expect(result).toEqual({ name: 'dashboard' })
    })

    it('allows a system-group route when the token is a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: true })
      const result = await enforceRoleTierVisibility(
        makeRoute('admin-feature-flags', { requiresSystemAdmin: true }),
        'tok',
      )
      expect(result).toBe(true)
    })

    it('redirects housekeeping when not a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: false })
      const result = await enforceRoleTierVisibility(
        makeRoute('admin-housekeeping', { requiresSystemAdmin: true }),
        'tok',
      )
      expect(result).toEqual({ name: 'dashboard' })
    })

    it('allows housekeeping when system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: true })
      const result = await enforceRoleTierVisibility(
        makeRoute('admin-housekeeping', { requiresSystemAdmin: true }),
        'tok',
      )
      expect(result).toBe(true)
    })

    it('redirects a route gated only by the route-level manifest field when not a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: false })
      const route = makeRoute('admin-system-orgs')
      hydrateManifestMeta(route)
      const result = await enforceRoleTierVisibility(route, 'tok')
      expect(result).toEqual({ name: 'dashboard' })
    })

    it('allows a route gated only by the route-level manifest field when a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: true })
      const route = makeRoute('admin-system-orgs')
      hydrateManifestMeta(route)
      const result = await enforceRoleTierVisibility(route, 'tok')
      expect(result).toBe(true)
    })
  })

  // FAR-1797: pin the gating bindings. The router route NAME and the manifest
  // route name must agree for the three route-entry system-admin gates, and the
  // gate must be derived from the manifest — never duplicated as inline meta.
  describe('FAR-1797: route-entry system-admin gates are pinned to the real router records', () => {
    const systemRoutes = [
      { name: 'admin-system-orgs', path: '/admin/system/orgs' },
      { name: 'admin-system-config', path: '/admin/system/config' },
      { name: 'admin-product-analytics', path: '/admin/product-analytics' },
    ]

    it('binds every REAL-manifest system-admin route name to a real router record on the same path', () => {
      // Driven from the real manifest from disk, never the mock: a rename of
      // the real manifest route `name:` must fail here, because at runtime
      // hydrateManifestMeta looks the route up by `to.name` and a mismatch
      // silently drops requiresSystemAdmin with no failing test.
      const matchedPaths: string[] = []

      for (const [path, entry] of Object.entries(REAL_MANIFEST.routes)) {
        if (!entry?.name || entry.system_admin_only !== true) continue
        const record = router.getRoutes().find((r) => r.name === entry.name)
        expect(record, `router must expose a record named ${entry.name} at ${path}`).toBeDefined()
        expect(record!.path).toBe(path)
        matchedPaths.push(path)
      }

      // Vacuity guard: the loop above must actually have exercised every known
      // gate; otherwise the binding assertion is a silent no-op.
      for (const path of EXPECTED_GATED_PATHS) {
        expect(matchedPaths, `vacuity: ${path} must be covered by the real-manifest bind`).toContain(path)
      }
      expect(matchedPaths.length).toBeGreaterThanOrEqual(EXPECTED_GATED_PATHS.length)
    })

    it('the REAL manifest names each gated route exactly as its real router record', () => {
      for (const path of EXPECTED_GATED_PATHS) {
        const entry = REAL_MANIFEST.routes[path]
        expect(entry, `real manifest entry for ${path} must exist`).toBeDefined()
        expect(entry.system_admin_only).toBe(true)

        const record = router.getRoutes().find((r) => r.name === entry.name)
        expect(record, `real router record ${entry.name} must exist`).toBeDefined()
        expect(record!.path).toBe(path)
      }
    })

    it('the real router records carry NO inline requiresSystemAdmin meta', () => {
      for (const { name } of systemRoutes) {
        const record = router.getRoutes().find((r) => r.name === name)
        expect(record, `router record ${name} must exist`).toBeDefined()
        expect(record!.meta.requiresSystemAdmin).toBeFalsy()
      }
    })

    it('each route redirects a non-system-admin and admits a system admin via the manifest-derived gate', async () => {
      for (const { name } of systemRoutes) {
        decodeJwtPayload.mockReturnValue({ is_system_admin: false })
        const deniedRoute = makeRoute(name)
        hydrateManifestMeta(deniedRoute)
        expect(deniedRoute.meta.requiresSystemAdmin).toBe(true)
        const denied = await enforceRoleTierVisibility(deniedRoute, 'tok')
        expect(denied).toEqual({ name: 'dashboard' })

        decodeJwtPayload.mockReturnValue({ is_system_admin: true })
        const allowedRoute = makeRoute(name)
        hydrateManifestMeta(allowedRoute)
        const allowed = await enforceRoleTierVisibility(allowedRoute, 'tok')
        expect(allowed).toBe(true)
      }
    })
  })
})
