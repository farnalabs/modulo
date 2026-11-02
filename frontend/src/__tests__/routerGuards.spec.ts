import { beforeEach, describe, expect, it, vi } from 'vitest'

// Use the real router implementation so the permission assertions below cover
// the navigation guard rather than only the extracted helper.
vi.mock('vue-router', async () => {
  const actual = await vi.importActual<typeof import('vue-router')>('vue-router')
  return actual
})

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import yaml from 'js-yaml'

// Unit-test the extracted router guard helpers (FAR: S3776 decomposition) in
// isolation by mocking their two external dependencies. This deterministically
// covers every branch of the new functions — navigation-based tests cannot
// reach the "allowed" paths without mounting the target Vue components, which
// is slow and flaky under jsdom.
const { decodeJwtPayload, usePlanStore } = vi.hoisted(() => ({
  decodeJwtPayload: vi.fn(),
  usePlanStore: vi.fn(),
}))

vi.mock('../lib/jwt', () => ({ decodeJwtPayload }))
vi.mock('../stores/planStore', () => ({ usePlanStore }))

import router, {
  documentTitleFor,
  enforceRoleTierVisibility,
  focusRouteContent,
  hydrateManifestMeta,
} from '../router'
import { clearAccessToken, setAccessToken } from '../lib/api/auth'

it('registers the risk assessment route in the real router', () => {
  expect(router.getRoutes().some((route) => route.name === 'risk-assessment' && route.path === '/risk-assessment')).toBe(true)
})

describe('document title', () => {
  it('prefixes the manifest title with the product name', () => {
    expect(documentTitleFor({ title: 'Pipelines' })).toBe('Pipelines | Modulo')
  })

  it('uses the manifest breadcrumb when the route carries no title', () => {
    expect(documentTitleFor({ breadcrumb: 'Runs' })).toBe('Runs | Modulo')
  })

  it('falls back to the bare product name rather than "Modulo | Modulo"', () => {
    // A bare route (e.g. /assistant) has neither meta.title nor a manifest
    // breadcrumb; the old template produced "Modulo | Modulo".
    expect(documentTitleFor({})).toBe('Modulo')
  })
})

describe('route content focus', () => {
  it('does nothing when the entered route renders neither a heading nor the shell landmark', () => {
    // A route whose content is still resolving has neither `[data-page-heading]`
    // nor the shell `<main data-main-landmark>`; there is no target to move
    // focus to, and guessing one would steal it from wherever it belongs.
    const focusSpy = vi.spyOn(HTMLElement.prototype, 'focus')
    focusRouteContent()
    expect(focusSpy).not.toHaveBeenCalled()
    focusSpy.mockRestore()
  })

  it('does not re-focus a heading that already holds focus', () => {
    const heading = document.createElement('h1')
    heading.tabIndex = -1
    heading.dataset.pageHeading = ''
    document.body.append(heading)
    heading.focus()
    const focusSpy = vi.spyOn(heading, 'focus')

    focusRouteContent()

    expect(focusSpy).not.toHaveBeenCalled()
    heading.remove()
    focusSpy.mockRestore()
  })

  it('leaves focus alone when it is already inside the page heading container', () => {
    // A composite page header renders its own focusable controls; a user who
    // has tabbed into one of them must not be yanked back to the container.
    const header = document.createElement('div')
    header.dataset.pageHeading = ''
    const control = document.createElement('a')
    control.href = '#'
    header.append(control)
    document.body.append(header)
    control.focus()
    const focusSpy = vi.spyOn(header, 'focus')

    focusRouteContent()

    expect(focusSpy).not.toHaveBeenCalled()
    header.remove()
    focusSpy.mockRestore()
  })
})

it('keeps organisation email settings reachable for a team org admin', async () => {
  decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
  const resolved = router.resolve('/settings/email')
  const route = {
    name: resolved.name,
    matched: resolved.matched,
    path: resolved.path,
    meta: {} as Record<string, unknown>,
  }

  hydrateManifestMeta(route as never)

  expect(route.name).toBe('settings-email')
  expect(route.path).toBe('/settings/email')
  expect(route.meta.requiresSystemAdmin).toBeUndefined()
  expect(route.meta.requiredRoles).toEqual(['admin'])
  expect(route.meta.requiredTier).toBe('team')
  await expect(enforceRoleTierVisibility(route as never, adminToken())).resolves.toBe(true)
})

function makeRoute(name: string | undefined, meta: Record<string, unknown> = {}): any {
  return { name, meta }
}

const planStore = {
  loaded: true,
  features: {} as Record<string, unknown>,
  devMode: false,
  userPermissions: [] as string[],
  isAtMinimumTier: vi.fn(() => true),
  featureEnabled: vi.fn(() => false),
  fetchPlan: vi.fn(async () => {}),
  fetchPermissions: vi.fn(async () => {}),
}

beforeEach(() => {
  vi.clearAllMocks()
  planStore.loaded = true
  planStore.features = {}
  planStore.devMode = false
  planStore.userPermissions = []
  planStore.isAtMinimumTier.mockReturnValue(true)
  planStore.featureEnabled.mockReturnValue(false)
  planStore.fetchPlan.mockResolvedValue(undefined)
  planStore.fetchPermissions.mockResolvedValue(undefined)
  clearAccessToken()
  usePlanStore.mockReturnValue(planStore)
  decodeJwtPayload.mockReturnValue(null)
})

function adminToken(): string {
  const b64url = (obj: Record<string, unknown>) =>
    btoa(JSON.stringify(obj)).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
  return `${b64url({ alg: 'none', typ: 'JWT' })}.${b64url({ org_role: 'admin' })}.test-signature`
}

describe('router permission navigation guard', () => {
  it('allows the compliance evidence index route when its permission exists', async () => {
    setAccessToken(adminToken())
    planStore.userPermissions = ['compliance.evidence_index.read']

    await router.push({ name: 'admin-compliance-evidence-index' })

    expect(router.currentRoute.value.name).toBe('admin-compliance-evidence-index')
    expect(planStore.fetchPermissions).toHaveBeenCalled()
  })

  it('maps both evidence-index URLs to the same permission and view', () => {
    const canonical = router.getRoutes().find((route) => route.name === 'admin-compliance-evidence-index')
    const legacy = router.getRoutes().find((route) => route.name === 'admin-evidence-index')

    expect(canonical?.path).toBe('/admin/compliance/evidence-index')
    expect(legacy?.path).toBe('/admin/evidence')
    expect(canonical?.components?.default).toBeTruthy()
    expect(legacy?.components?.default).toBeTruthy()
    expect(canonical?.meta).toBeDefined()
    expect(legacy?.meta).toBeDefined()

    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as {
      routes: Record<string, { deprecated?: boolean }>
      features: Record<string, { behaviours?: string[] }>
    }
    expect(manifest.routes['/admin/evidence'].deprecated).toBe(true)

    const evidenceBehaviour = manifest.features['feat-evidence'].behaviours?.find(
      (behaviour) => behaviour.includes('deprecated /admin/evidence route'),
    )
    expect(evidenceBehaviour).toContain('ComplianceEvidenceIndexView.vue')
    expect(evidenceBehaviour).toContain('compatibility alias')
  })

  it('redirects the compliance evidence index route when its permission is missing', async () => {
    setAccessToken(adminToken())
    planStore.userPermissions = []
    await router.push({ name: 'dashboard' })

    await router.push({ name: 'admin-compliance-evidence-index' })

    expect(router.currentRoute.value.name).toBe('dashboard')
  })
})

describe('enforceRoleTierVisibility', () => {
  it('redirects a requiresSystemAdmin route when the token is not a system admin', async () => {
    decodeJwtPayload.mockReturnValue({ is_system_admin: false })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-system-orgs', { requiresSystemAdmin: true }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows a requiresSystemAdmin route when the token is a system admin', async () => {
    decodeJwtPayload.mockReturnValue({ is_system_admin: true })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-system-orgs', { requiresSystemAdmin: true }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('redirects when requiredRoles is set and the org role is missing', async () => {
    decodeJwtPayload.mockReturnValue({})
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('redirects when requiredRoles is set and the org role does not match', async () => {
    decodeJwtPayload.mockReturnValue({ org_role: 'member' })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows when requiredRoles is set and the org role matches', async () => {
    decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', { requiredRoles: ['admin'] }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('redirects on requiredTier when below the minimum tier', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.features = { something: 1 }
    planStore.isAtMinimumTier.mockReturnValue(false)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows on requiredTier when at the minimum tier', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.isAtMinimumTier.mockReturnValue(true)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('fetches the plan when not yet loaded before evaluating tier', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = false
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(planStore.fetchPlan).toHaveBeenCalled()
    expect(result).toBe(true)
  })

  it('applies the tier check to a KNOWN-EMPTY flag map — empty ≠ unknown (FAR-1809)', async () => {
    // A payload applied with zero flags still means the TIER is known
    // (`loaded` is written together with `currentTier`). The old guard read
    // `Object.keys(features).length > 0`, so a resolved community org sailed
    // through a team route whenever the flag map came back empty.
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = true
    planStore.features = {} // known-empty, not unknown
    planStore.isAtMinimumTier.mockReturnValue(false)
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('skips the tier check while the tier is unknown — no payload ever applied (FAR-1809)', async () => {
    // `loaded` false after the guard's own fetch = the plan bootstrap settled
    // unreadable ('unavailable') or produced nothing, so `currentTier` is the
    // 'community' bootstrap DEFAULT, not observed truth. Tier-checking that
    // default would bounce a licensed user to the dashboard on a transient
    // plan failure; the backend 402 still enforces the real licence.
    decodeJwtPayload.mockReturnValue({})
    planStore.loaded = false
    planStore.features = {}
    planStore.isAtMinimumTier.mockReturnValue(false) // would redirect if consulted
    const result = await enforceRoleTierVisibility(
      makeRoute('some-tier-route', { requiredTier: 'team' }),
      'tok',
    )
    expect(planStore.fetchPlan).toHaveBeenCalled()
    expect(result).toBe(true)
  })

  it('redirects a private_preview route when devMode is off', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = false
    const result = await enforceRoleTierVisibility(
      makeRoute('settings-assistant', { visibility: 'private_preview' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('allows a private_preview route when devMode is on', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = true
    const result = await enforceRoleTierVisibility(
      makeRoute('settings-assistant', { visibility: 'private_preview' }),
      'tok',
    )
    expect(result).toBe(true)
  })

  it('redirects an in_dev route when devMode is off', async () => {
    decodeJwtPayload.mockReturnValue({})
    planStore.devMode = false
    const result = await enforceRoleTierVisibility(
      makeRoute('some-dev-route', { visibility: 'in_dev' }),
      'tok',
    )
    expect(result).toEqual({ name: 'dashboard' })
  })

  it('lets a community-tier admin through /admin/org with the REAL manifest entry (FAR-1269)', async () => {
    // Drive the guard with /admin/org's actual required_tier/required_roles
    // from frontend/src/manifest.yaml, in the exact plan state that used to
    // redirect community admins to the dashboard (a RESOLVED plan — the
    // guard's fail-open applies only while the tier is UNKNOWN, `loaded`
    // false, which is not the path a real community session takes). The org HITL review window endpoint is
    // not plan-gated, so this route must admit the community admin.
    const manifest = yaml.load(
      readFileSync(resolve(__dirname, '../manifest.yaml'), 'utf-8'),
    ) as { routes: Record<string, { required_tier?: string | null; required_roles?: string[] | null }> }
    const entry = manifest.routes['/admin/org']
    expect(entry).toBeDefined()
    expect(entry.required_tier).toBe('community')

    decodeJwtPayload.mockReturnValue({ org_role: 'admin' })
    planStore.features = { hitl_reviews: true }
    // A community org is at the minimum tier only when the route's required
    // tier is itself 'community'.
    planStore.isAtMinimumTier.mockReturnValue(entry.required_tier === 'community')

    const result = await enforceRoleTierVisibility(
      makeRoute('admin-org', {
        requiredRoles: entry.required_roles ?? undefined,
        requiredTier: entry.required_tier ?? undefined,
      }),
      'tok',
    )
    expect(result).toBe(true)
  })
})

describe('/runs/diff private_preview gate (FAR-2248)', () => {
  function hydratedRunsDiff(): any {
    const resolved = router.resolve('/runs/diff')
    const route = {
      name: resolved.name,
      matched: resolved.matched,
      path: resolved.path,
      meta: {} as Record<string, unknown>,
    }
    hydrateManifestMeta(route as never)
    return route
  }

  it('hydrates the runs-diff manifest entry as private_preview, not the run-detail entry', () => {
    const route = hydratedRunsDiff()

    expect(route.name).toBe('runs-diff')
    expect(route.meta.visibility).toBe('private_preview')
  })

  it('bounces to the dashboard and names the cause in the console when dev mode is off', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    planStore.devMode = false

    const result = await enforceRoleTierVisibility(hydratedRunsDiff(), adminToken())

    expect(result).toEqual({ name: 'dashboard' })
    expect(warn).toHaveBeenCalledWith(expect.stringContaining('runs-diff is a private_preview surface'))
    warn.mockRestore()
  })

  it('lets an admin through when dev mode is on', async () => {
    planStore.devMode = true

    const result = await enforceRoleTierVisibility(hydratedRunsDiff(), adminToken())

    expect(result).toBe(true)
  })
})
