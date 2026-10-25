import { readFileSync } from 'node:fs'
import path from 'node:path'
import yaml from 'js-yaml'
import type { Page } from '@playwright/test'
import type { TestEnv } from './env'
import { apiBaseFor, apiFetch, apiLogin } from './realstack-api'

/**
 * Tier-aware navigation guard for the real-stack targets.
 *
 * The staging deployment is the CI-only E2E environment and deliberately runs
 * on the COMMUNITY tier (no licence secret; see the staging Fly secret set and
 * the FAR-2091 startup contract). Every manifest route with
 * `required_tier: team` is therefore redirected to the dashboard by the
 * router guard — which is correct product behaviour, but a page spec that
 * asserts the team page's heading fails on a redirect it can never satisfy.
 *
 * Rather than duplicating a `test.skip(...)` precondition in every team-gated
 * spec (and missing the next one), the `page` fixture installs one guard that
 * knows the manifest's team-tier routes and skips a test the moment it tries
 * to navigate to one on a target whose licence tier is below team. The guard
 * applies ONLY to the staging target, and is further inert when:
 *
 *  - staging's real `GET /api/v1/plan` reports team/enterprise (a licence was
 *    configured there), and
 *  - a spec registers its own `/api/v1/plan` handler (it has explicitly taken
 *    control of the tier the router sees, so the guard must not second-guess
 *    it).
 *
 * The `app` (production) target is deliberately NOT guarded: production is
 * meant to be Team-licensed, so a community tier there must fail loudly
 * instead of silently skipping the spec.
 *
 * Skipping is the established contract for licence-gated surfaces on a real
 * target (`journeys/teams-journey.spec.ts` does the same for team RBAC), and
 * the spec starts running again automatically once the target carries a Team
 * licence — no test is deleted or weakened.
 */

interface ManifestRoute {
  required_tier?: string | null
}

interface Manifest {
  routes?: Record<string, ManifestRoute>
}

const TIER_RANK: Record<string, number> = { community: 0, team: 1, enterprise: 2 }

/** True when *tier* unlocks the manifest's Team-tier floor. */
export function isAtLeastTeamTier(tier: string): boolean {
  return (TIER_RANK[tier] ?? 0) >= TIER_RANK.team
}

/**
 * Manifest paths whose route gate requires the Team tier, longest first so a
 * nested route (`/admin/costs/limits`) is matched before its parent.
 *
 * Dynamic suffixes (`/admin/errors/:id`) are reduced to their static prefix;
 * a real navigation can never contain the literal `:id` segment anyway.
 */
function loadTeamGatedPaths(): string[] {
  // Same resolution pattern as mobile-layout.spec.ts: this file lives in
  // frontend/tests/e2e/setup, so three levels up is frontend/.
  const manifestPath = path.join(__dirname, '../../../src/manifest.yaml')
  const manifest = yaml.load(readFileSync(manifestPath, 'utf-8')) as Manifest | undefined
  return Object.entries(manifest?.routes ?? {})
    .filter(([, route]) => route?.required_tier === 'team')
    .map(([routePath]) => routePath.replace(/\/:[^/]+/g, ''))
    .sort((a, b) => b.length - a.length)
}

const TEAM_GATED_PATHS = loadTeamGatedPaths()

/**
 * Legacy paths that the router redirects onto a Team-tier manifest route.
 *
 * The manifest lists the CURRENT paths only; `src/router/index.ts` keeps
 * one-release aliases alive (`/admin/sandbox-concurrency` →
 * `/admin/runners/concurrency`, `/environment-profiles/**` →
 * `/admin/runners/profiles/**`, ...). A spec that navigates to an alias is
 * gated exactly as its target is, so the alias has to be matched here too.
 * Keep this list in step with the router's redirects to a Team-tier path.
 */
const LEGACY_TEAM_GATED_EXACT = new Set([
  '/admin',
  '/admin/sandbox-concurrency',
  '/admin/environments',
  '/admin/remy',
  '/settings/remy',
])
const LEGACY_TEAM_GATED_PREFIXES = ['/environment-profiles', '/admin/runners']

/** Return the team-gated manifest path *pathname* falls under, if any. */
export function matchTeamGatedPath(pathname: string): string | undefined {
  const gated = TEAM_GATED_PATHS.find(
    (path) => pathname === path || pathname.startsWith(`${path}/`),
  )
  if (gated) return gated
  if (LEGACY_TEAM_GATED_EXACT.has(pathname)) return pathname
  return LEGACY_TEAM_GATED_PREFIXES.find(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  )
}

function pathnameFor(url: string): string | undefined {
  try {
    return new URL(url, 'http://e2e.invalid').pathname
  } catch {
    return undefined
  }
}

/**
 * Resolve the real licence tier of a target once per worker.
 *
 * `/api/v1/plan` is the caller-scoped plan bootstrap the router itself reads
 * (FAR-1809), so it reports exactly the tier that will gate the navigation.
 */
const tierCache = new Map<string, Promise<string>>()

async function fetchTargetTier(env: TestEnv): Promise<string> {
  const apiBase = apiBaseFor(env)
  const token = await apiLogin(env)
  const res = await apiFetch<{ license?: { tier?: unknown } }>(
    apiBase,
    token,
    'GET',
    '/api/v1/plan',
  )
  if (res.status !== 200) {
    throw new Error(
      `[target-tier] GET /api/v1/plan failed on ${env.name}: ${res.status} ${res.text.slice(0, 200)}`,
    )
  }
  const tier = res.body?.license?.tier
  return typeof tier === 'string' && tier ? tier : 'community'
}

export function resolveTargetTier(env: TestEnv): Promise<string> {
  let resolved = tierCache.get(env.name)
  if (!resolved) {
    resolved = fetchTargetTier(env)
    tierCache.set(env.name, resolved)
  }
  return resolved
}

/** Drop the cached tier (tests of this helper; also makes a worker re-resolve). */
export function resetTargetTierCache(): void {
  tierCache.clear()
}

function patternIncludesPlan(pattern: string | RegExp): boolean {
  const source = typeof pattern === 'string' ? pattern : pattern.source
  return source.includes('/api/v1/plan')
}

/**
 * Install the team-tier navigation guard on *page*.
 *
 * Only the staging target is guarded (see the module docstring). `skip` is the
 * caller's `testInfo.skip` (injected to avoid importing the fixture module
 * back into this one). It is only invoked for a navigation to a manifest
 * Team-tier route when staging's real tier is below team, and never when the
 * spec owns the `/api/v1/plan` response.
 */
export function installTeamTierNavigationGuard(
  page: Page,
  env: TestEnv,
  skip: (reason: string) => void,
  resolveTier: (env: TestEnv) => Promise<string> = resolveTargetTier,
): void {
  if (env.name !== 'staging') return

  let planControlledBySpec = false
  const originalRoute = page.route.bind(page)
  page.route = ((pattern: string | RegExp, handler: Parameters<Page['route']>[1]) => {
    if (patternIncludesPlan(pattern)) planControlledBySpec = true
    return originalRoute(pattern, handler)
  }) as Page['route']

  const originalGoto = page.goto.bind(page)
  page.goto = (async (url: string, options?: Parameters<Page['goto']>[1]) => {
    const pathname = pathnameFor(url)
    const gated = pathname ? matchTeamGatedPath(pathname) : undefined
    if (gated && !planControlledBySpec) {
      const tier = await resolveTier(env)
      if (!isAtLeastTeamTier(tier)) {
        skip(
          `Team-tier route ${gated} is licence-gated on the "${env.name}" target (tier "${tier}"); ` +
            'the router redirects it to the dashboard. Set a Team licence on the target to run this spec.',
        )
      }
    }
    return originalGoto(url, options)
  }) as Page['goto']
}
