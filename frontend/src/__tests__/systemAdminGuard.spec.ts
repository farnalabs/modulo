import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockManifest = vi.hoisted(() => ({
  sidebar_groups: {
    build: { label: 'BUILD', order: 1, default_expanded: true },
    system: { label: 'SYSTEM', order: 6, default_expanded: false, system_admin_only: true },
  },
  routes: {
    '/settings/license': { name: 'settings-license', breadcrumb: 'License', sidebar_group: 'system', sidebar_order: 1, type: 'form_page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/housekeeping': { name: 'admin-housekeeping', breadcrumb: 'Housekeeping', sidebar_group: 'system', sidebar_order: 6, type: 'page', required_tier: null, required_roles: null, required_permissions: null },
    '/admin/feature-flags': { name: 'admin-feature-flags', breadcrumb: 'Feature Flags', sidebar_group: 'system', sidebar_order: 4, type: 'list_page', required_tier: null, required_roles: null, required_permissions: null },
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
    it('sets requiresSystemAdmin=true for a system-group route', () => {
      const route = makeRoute('settings-license')
      hydrateManifestMeta(route)
      expect(route.meta.requiresSystemAdmin).toBe(true)
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
  })

  describe('enforceRoleTierVisibility blocks non-system-admins from system routes', () => {
    it('redirects a system-group route when the token is not a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: false })
      const result = await enforceRoleTierVisibility(
        makeRoute('settings-license', { requiresSystemAdmin: true }),
        'tok',
      )
      expect(result).toEqual({ name: 'dashboard' })
    })

    it('allows a system-group route when the token is a system admin', async () => {
      decodeJwtPayload.mockReturnValue({ is_system_admin: true })
      const result = await enforceRoleTierVisibility(
        makeRoute('settings-license', { requiresSystemAdmin: true }),
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
  })
})
