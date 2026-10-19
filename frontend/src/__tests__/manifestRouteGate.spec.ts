/**
 * FAR-1950 — path-level manifest gate binding.
 *
 * The router looks a route up in the manifest by `to.name`
 * (`hydrateManifestMeta` in `router/index.ts`). The existing binding tests
 * (`systemAdminGuard.spec.ts`, `manifestGating.spec.ts`) look the record up
 * BY NAME, so they passed while real navigation resolved the PATH to an
 * unnamed, empty-path default child (Vue Router R0103): `to.name` was
 * `undefined`, hydration early-returned, and the role/tier gate for
 * `/admin/runners/{profiles,concurrency}` silently never ran for any caller.
 *
 * This spec is the missing assertion: it iterates EVERY gated manifest route,
 * resolves it the way the browser does (by path, params substituted), runs the
 * real hydration, and asserts the gate fields populate from the manifest —
 * and that a route resolving to an unnamed child still hydrates.
 */
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'
import yaml from 'js-yaml'

// Restore the REAL vue-router (the shared vitest setup mocks it globally) so
// the router module carries the real route records and `router.resolve` is the
// real matcher — same override pattern as systemAdminGuard.spec.ts.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

import router, { hydrateManifestMeta, findUnhydratedGatedManifestPath } from '../router'

interface ManifestRoute {
  name: string
  breadcrumb?: string
  required_tier?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
  system_admin_only?: boolean
  sidebar_group?: string | null
}

interface Manifest {
  routes: Record<string, ManifestRoute>
  sidebar_groups: Record<string, { system_admin_only?: boolean }>
}

const MANIFEST = yaml.load(readFileSync(resolvePath(__dirname, '../manifest.yaml'), 'utf-8')) as Manifest

/** The two routes whose empty-path default child broke the gate (FAR-1950). */
const RUNNER_GATED_PATHS = ['/admin/runners/profiles', '/admin/runners/concurrency'] as const

const MIN_GATED_ROUTES = 25

/** Substitute route params so a manifest pattern can be resolved as a URL. */
function concretePath(path: string): string {
  return path.replace(/:([A-Za-z0-9_]+)\??/g, 'far-1950-param')
}

/** Mirrors the manifest-side gate signals `hydrateManifestMeta` reads. */
function routeDeclaresGate(entry: ManifestRoute | undefined): boolean {
  if (!entry) return false
  const groupId = entry.sidebar_group
  return Boolean(
    entry.required_tier ||
      entry.required_roles?.length ||
      entry.required_permissions?.length ||
      entry.system_admin_only ||
      (groupId && MANIFEST.sidebar_groups[groupId]?.system_admin_only),
  )
}

/**
 * Resolve a manifest path the way navigation does, then build the route object
 * the beforeEach guard receives and run the real hydration on it. `options`
 * reproduces the original defect shape (`to.name === undefined`).
 */
function hydrateRouteForPath(path: string, options: { clearName?: boolean } = {}) {
  const resolved = router.resolve(concretePath(path))
  const route = {
    name: options.clearName ? undefined : resolved.name,
    matched: resolved.matched,
    path: resolved.path,
    meta: {} as Record<string, unknown>,
  }
  hydrateManifestMeta(route as never)
  return { resolved, route }
}

describe('FAR-1950: manifest gates hydrate for routes resolved BY PATH', () => {
  const entries = Object.entries(MANIFEST.routes)

  it('the manifest carries gated routes to check (vacuity guard)', () => {
    expect(entries.filter(([, entry]) => routeDeclaresGate(entry)).length).toBeGreaterThanOrEqual(
      MIN_GATED_ROUTES,
    )
  })

  it('every manifest route resolves by path to a record named as the manifest names it (R0103 unnamed-child trap)', () => {
    const wrongName: string[] = []
    for (const [path, entry] of entries) {
      const resolved = router.resolve(concretePath(path))
      if (typeof resolved.name !== 'string') {
        wrongName.push(`${path} (manifest name "${entry.name}") resolved to an unnamed record`)
      } else if (resolved.name !== entry.name) {
        wrongName.push(`${path} resolved to "${resolved.name}" but the manifest names it "${entry.name}"`)
      }
    }
    expect(wrongName, wrongName.join('\n')).toEqual([])
  })

  it('hydrates requiredRoles / requiredTier / requiresSystemAdmin for every gated manifest route', () => {
    const failures: string[] = []
    let checked = 0
    for (const [path, entry] of entries) {
      if (!routeDeclaresGate(entry)) continue
      checked += 1
      const { resolved, route } = hydrateRouteForPath(path)
      const label = `${path} (resolved name ${String(resolved.name)})`

      if (route.meta.requiredTier !== entry.required_tier) {
        failures.push(
          `${label}: requiredTier expected ${String(entry.required_tier)}, got ${String(route.meta.requiredTier)}`,
        )
      }
      const expectedRoles = entry.required_roles ?? undefined
      if (JSON.stringify(route.meta.requiredRoles) !== JSON.stringify(expectedRoles)) {
        failures.push(
          `${label}: requiredRoles expected ${JSON.stringify(expectedRoles)}, got ${JSON.stringify(route.meta.requiredRoles)}`,
        )
      }
      const expectedPermissions = entry.required_permissions ?? undefined
      if (JSON.stringify(route.meta.requiredPermissions) !== JSON.stringify(expectedPermissions)) {
        failures.push(
          `${label}: requiredPermissions expected ${JSON.stringify(expectedPermissions)}, got ${JSON.stringify(route.meta.requiredPermissions)}`,
        )
      }
      const expectsSystemAdmin =
        entry.system_admin_only === true ||
        Boolean(entry.sidebar_group && MANIFEST.sidebar_groups[entry.sidebar_group]?.system_admin_only)
      if (expectsSystemAdmin && route.meta.requiresSystemAdmin !== true) {
        failures.push(
          `${label}: requiresSystemAdmin expected true, got ${String(route.meta.requiresSystemAdmin)}`,
        )
      }
      if (route.meta.breadcrumb !== entry.breadcrumb) {
        failures.push(
          `${label}: breadcrumb expected ${String(entry.breadcrumb)}, got ${String(route.meta.breadcrumb)}`,
        )
      }
    }
    expect(checked).toBeGreaterThanOrEqual(MIN_GATED_ROUTES)
    expect(failures, failures.join('\n')).toEqual([])
  })
})

describe('FAR-1950 regression: a route resolving to an unnamed child still hydrates its gate', () => {
  // The original defect: /admin/runners/{profiles,concurrency} resolved to the
  // unnamed, empty-path default child, so `to.name` was `undefined`. The
  // manifest lookup must still reach the gated entry (via `to.matched`) — this
  // assertion fails if that fallback is removed, naming both routes.
  it('hydrates requiredTier and the admin role with an undefined route name', () => {
    const failures: string[] = []
    for (const path of RUNNER_GATED_PATHS) {
      const entry = MANIFEST.routes[path]
      const { route } = hydrateRouteForPath(path, { clearName: true })

      if (route.meta.requiredTier !== entry.required_tier) {
        failures.push(
          `${path}: requiredTier not hydrated for a route with an undefined name (expected ${String(entry.required_tier)}, got ${String(route.meta.requiredTier)})`,
        )
      }
      const roles = route.meta.requiredRoles
      if (!Array.isArray(roles) || !roles.includes('admin')) {
        failures.push(
          `${path}: requiredRoles not hydrated for a route with an undefined name (got ${JSON.stringify(roles)})`,
        )
      }
    }
    expect(failures, failures.join('\n')).toEqual([])
  })
})

describe('FAR-1950 fail-closed backstop: an unhydrated gated manifest path is surfaced', () => {
  it('returns the gated manifest path when the route meta carries no gate', () => {
    // A route that reaches the guard with no gate but whose path the manifest
    // gates — the exact silent state /admin/runners/{profiles,concurrency}
    // were in before hydration worked. The guard warns + redirects on this.
    const unhydrated = {
      name: undefined,
      matched: [],
      path: '/settings/sso',
      meta: {},
    }
    expect(findUnhydratedGatedManifestPath(unhydrated as never)).toBe('/settings/sso')
  })

  it('does not fire for any real gated manifest route (guard never over-redirects)', () => {
    for (const [path] of Object.entries(MANIFEST.routes)) {
      const { route } = hydrateRouteForPath(path)
      expect(
        findUnhydratedGatedManifestPath(route as never),
        `backstop must not fire for a correctly hydrated route: ${path}`,
      ).toBeUndefined()
    }
  })

  it('does not fire for a route the manifest does not declare', () => {
    const unknown = { name: 'dev-metrics', matched: [], path: '/dev/metrics', meta: {} }
    expect(findUnhydratedGatedManifestPath(unknown as never)).toBeUndefined()
  })
})
