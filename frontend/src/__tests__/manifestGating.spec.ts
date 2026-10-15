/**
 * Regression test: the manifest's YAML merge-key (`<<: *team` /
 * `<<: *community`) anchors MUST expand when frontend/src/manifest.yaml is
 * parsed with the project's js-yaml.
 *
 * js-yaml v5 dropped merge-key expansion. Under v5 every merged route keeps a
 * literal `'<<'` key and `required_tier` / `required_roles` /
 * `required_permissions` are undefined, which silently disables ALL sidebar
 * (config/navigation.ts) and router (hydrateManifestMeta →
 * enforceRoleTierVisibility) tier/role gating. This test parses the REAL
 * manifest from disk with the INSTALLED js-yaml so a future major bump fails
 * here instead of shipping silently.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'
import { hydrateManifestMeta } from '../router'

const MANIFEST_PATH = resolve(__dirname, '../manifest.yaml')

interface ManifestRoute {
  required_tier?: string | null
  required_roles?: string[] | null
  required_permissions?: string[] | null
  [key: string]: unknown
}

interface Manifest {
  routes: Record<string, ManifestRoute>
}

function loadManifest(): Manifest {
  const raw = readFileSync(MANIFEST_PATH, 'utf-8')
  return yaml.load(raw) as Manifest
}

describe('manifest merge-key gating (js-yaml regression guard)', () => {
  const manifest = loadManifest()
  const routes = manifest.routes

  it('parses a routes map from the real manifest (vacuity guard)', () => {
    expect(routes).toBeDefined()
    const routeCount = Object.keys(routes).length
    expect(routeCount).toBeGreaterThan(0)
  })

  it('no route object carries a literal "<<" key (js-yaml v5 regression signature)', () => {
    const routesWithMergeKey = Object.entries(routes)
      .filter(([, route]) => Object.prototype.hasOwnProperty.call(route, '<<'))
      .map(([path]) => path)
    expect(routesWithMergeKey.length).toBe(0)
  })

  it("/settings/sso is gated to team tier with the admin role", () => {
    const ssoRoute = routes['/settings/sso']
    expect(ssoRoute).toBeDefined()
    expect(ssoRoute.required_tier).toBe('team')
    expect(ssoRoute.required_roles).toContain('admin')
  })

  it('/admin/org is community-tier with the admin role (FAR-1269: the org HITL review window is reachable on every tier)', () => {
    // The /admin/org route itself must NOT tier-block community admins: the
    // org HITL review window endpoint (GET/PUT
    // /api/v1/admin/org/hitl-review-window) is not plan-gated, so the route
    // gate has to let community in — otherwise the card outside the in-page
    // FeatureGate is unreachable no matter where it sits in the template.
    // required_roles stays [admin] so navigation is still admin-only.
    const orgRoute = routes['/admin/org']
    expect(orgRoute).toBeDefined()
    expect(orgRoute.required_tier).toBe('community')
    expect(orgRoute.required_roles).toContain('admin')
  })

  it('at least 25 routes have a non-null required_tier (non-vacuity guard)', () => {
    const gatedRoutes = Object.values(routes).filter((route) => {
      const tier = route.required_tier
      return tier !== null && tier !== undefined
    })
    expect(gatedRoutes.length).toBeGreaterThanOrEqual(25)
  })

  it('route-level system-admin gates carry system_admin_only: true (manifest is the single source)', () => {
    // These three routes have a blank sidebar_group, so the sidebar-group
    // derivation alone can never gate them — the route entry must carry the
    // signal itself. Regression guard for an inline-meta-only gate.
    for (const path of ['/admin/system/orgs', '/admin/system/config', '/admin/product-analytics']) {
      const route = routes[path]
      expect(route, `${path} must exist`).toBeDefined()
      expect(route.system_admin_only).toBe(true)
    }
  })

  it('/admin/product-analytics keeps the Team tier gate but drops the accidental admin org-role gate (FAR-1795)', () => {
    // `<<: *team` injects BOTH `required_tier: team` AND `required_roles: [admin]`.
    // A system_admin_only route is an INSTANCE-level gate; the org-ROLE
    // requirement does not apply and wrongly redirected a system admin whose
    // org_role != 'admin'. The explicit `required_roles:` after `<<:` must
    // override the merged value to null while the tier gate survives.
    const route = routes['/admin/product-analytics']
    expect(route).toBeDefined()
    expect(route.system_admin_only).toBe(true)
    expect(route.required_tier).toBe('team')
    expect(route.required_roles == null).toBe(true)
  })

  it('drives the REAL derivation: /admin/product-analytics is system-admin gated with no org-role requirement (FAR-1795)', () => {
    // Exercise the actual router hydration (vite yaml plugin → hydrateManifestMeta),
    // not a source-text scrape: requiresSystemAdmin must still be set and the
    // accidental org-role gate must be gone.
    const routeName = routes['/admin/product-analytics'].name as string
    expect(typeof routeName).toBe('string')
    expect(routeName.length).toBeGreaterThan(0)
    const route = { name: routeName, meta: {} as Record<string, unknown> }
    hydrateManifestMeta(route as never)
    expect(route.meta.requiresSystemAdmin).toBeTruthy()
    expect(route.meta.requiredRoles).toBeFalsy()
  })
})
