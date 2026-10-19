/**
 * FAR-1950 — branch coverage for the path-level manifest gate binding.
 *
 * The sibling `manifestRouteGate.spec.ts` proves the binding against the REAL
 * manifest. This spec drives the remaining branches with a small, crafted
 * manifest: a null entry, an entry with no name, an ungated entry, and a
 * gated path that a navigation resolves to by name from a *different*
 * (ungated) entry. That last shape is the only way the guard's fail-closed
 * backstop can fire — the manifest gated `/library`, but the resolved record's
 * name maps to an ungated entry, so hydration leaves `to.meta` ungated while
 * `manifestPathForRoute` still sees the gated path.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

// Restore the REAL vue-router (the shared vitest setup mocks it globally) so
// the actual beforeEach guard runs — same override pattern as
// routerFeatureFlagGuard.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

// A crafted manifest covering every module-init and predicate branch.
// - `/null-entry` is `null`            -> the `if (!entry) continue` arm
// - `/library` has NO `name`           -> the `if (entry.name)` false arm
// - `/decoy` IS named `library`        -> hydration resolves the name, not path
// - `/ungated` is a named, ungated route
vi.mock('@/manifest.yaml', () => ({
  default: {
    sidebar_groups: {
      sg_admin: { system_admin_only: true },
      sg_plain: { system_admin_only: false },
    },
    routes: {
      '/null-entry': null,
      '/library': {
        breadcrumb: 'Library',
        type: 'list_page',
        required_tier: 'team',
        required_roles: null,
        required_permissions: null,
        feature_flag: null,
      },
      '/decoy': {
        name: 'library',
        breadcrumb: 'Decoy',
        type: 'list_page',
        required_tier: null,
        required_roles: null,
        required_permissions: null,
        feature_flag: null,
      },
      '/ungated': {
        name: 'ungated',
        breadcrumb: 'Ungated',
        type: 'list_page',
        required_tier: null,
        required_roles: null,
        required_permissions: null,
        feature_flag: null,
      },
      // Permissions-only gate (no tier / role / admin): the shape the
      // FAR-1950 reviewer flagged. Hydration copies `required_permissions`
      // onto meta, so the fail-closed backstop must treat the route as
      // hydrated and NOT redirect a permission-holding user to the dashboard.
      '/perms-only': {
        name: 'perms-only',
        breadcrumb: 'Perms Only',
        type: 'list_page',
        required_tier: null,
        required_roles: null,
        required_permissions: ['run:read'],
        feature_flag: null,
      },
    },
  },
}))

import router, { entryCarriesGate, hydrateManifestMeta, findUnhydratedGatedManifestPath } from '../router'
import { clearAccessToken, setAccessToken } from '../lib/api/auth'

beforeEach(() => {
  localStorage.clear()
  clearAccessToken()
  vi.restoreAllMocks()
})

describe('FAR-1950: entryCarriesGate signals (every OR operand)', () => {
  it('is true for each declared gate signal in isolation', () => {
    expect(entryCarriesGate({ required_tier: 'team' } as never)).toBe(true)
    expect(entryCarriesGate({ required_tier: null, required_roles: ['admin'] } as never)).toBe(true)
    expect(
      entryCarriesGate({ required_roles: null, required_permissions: ['run:read'] } as never),
    ).toBe(true)
    expect(entryCarriesGate({ required_permissions: null, system_admin_only: true } as never)).toBe(
      true,
    )
    expect(
      entryCarriesGate({ system_admin_only: false, sidebar_group: 'sg_admin' } as never),
    ).toBe(true)
  })

  it('is false for an ungated entry, a non-admin sidebar group, and undefined', () => {
    expect(entryCarriesGate({} as never)).toBe(false)
    expect(
      entryCarriesGate({ system_admin_only: false, sidebar_group: 'sg_plain' } as never),
    ).toBe(false)
    expect(entryCarriesGate(undefined)).toBe(false)
  })
})

describe('FAR-1950: a permissions-only gate stays symmetric between the two halves', () => {
  it('does not fail-closed when hydration carried only the declared permissions gate', () => {
    const to = {
      name: undefined,
      matched: [{ path: '/perms-only' }],
      path: '/perms-only',
      meta: { requiredPermissions: ['run:read'] } as Record<string, unknown>,
    }
    expect(findUnhydratedGatedManifestPath(to as never)).toBeUndefined()
  })

  it('hydrates requiredPermissions onto meta and the backstop does not fire', () => {
    const route = {
      name: undefined,
      matched: [{ name: undefined, path: '/perms-only' }],
      path: '/perms-only',
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    expect(route.meta.requiredPermissions).toEqual(['run:read'])
    expect(findUnhydratedGatedManifestPath(route as never)).toBeUndefined()
  })
})

describe('FAR-1950: resolveManifestEntry matched-record path fallback', () => {
  it('resolves an unnamed child by its matched record path', () => {
    const route = {
      name: undefined,
      matched: [{ name: undefined, path: '/library' }],
      path: '/library',
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    expect(route.meta.requiredTier).toBe('team')
  })

  it('returns without hydrating when no matched record is in the manifest', () => {
    const route = {
      name: undefined,
      matched: [{ name: 'not-in-manifest', path: '/not-in-manifest' }],
      path: '/not-in-manifest',
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    expect(route.meta).toEqual({})
  })
})

describe('FAR-1950: manifestPathForRoute branches', () => {
  it('finds a gated manifest path through a matched record', () => {
    const to = { name: undefined, matched: [{ path: '/library' }], path: '/whatever', meta: {} }
    expect(findUnhydratedGatedManifestPath(to as never)).toBe('/library')
  })

  it('falls back to to.path when there is no matched array', () => {
    const to = { name: undefined, path: '/ungated', meta: {} }
    expect(findUnhydratedGatedManifestPath(to as never)).toBeUndefined()
  })

  it('returns undefined when neither the matched records nor to.path are manifest paths', () => {
    const to = { name: undefined, matched: [{ path: '/nope' }], path: '/nope', meta: {} }
    expect(findUnhydratedGatedManifestPath(to as never)).toBeUndefined()
  })
})

describe('FAR-1950: the guard fail-closed backstop fires end-to-end', () => {
  it('redirects to the dashboard and warns when a gated manifest path hydrated to ungated meta', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    setAccessToken('test-token')
    await router.push({ name: 'library' })
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('manifest gate for /library'))
  }, 20_000)

  it('allows an ungated route through with no warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    setAccessToken('test-token')
    await router.push({ name: 'dashboard' })
    expect(router.currentRoute.value.name).toBe('dashboard')
    expect(warn).not.toHaveBeenCalled()
  }, 20_000)
})
