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

  it('at least 25 routes have a non-null required_tier (non-vacuity guard)', () => {
    const gatedRoutes = Object.values(routes).filter((route) => {
      const tier = route.required_tier
      return tier !== null && tier !== undefined
    })
    expect(gatedRoutes.length).toBeGreaterThanOrEqual(25)
  })
})
