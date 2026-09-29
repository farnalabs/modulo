import { beforeEach, describe, expect, it, vi } from 'vitest'
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

import {
  enforceRoleTierVisibility,
} from '../router'

function makeRoute(name: string | undefined, meta: Record<string, unknown> = {}): any {
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
    // redirect community admins to the dashboard (a RESOLVED, non-empty
    // feature map — the guard's fail-open on an empty map is not the path a
    // real community session takes). The org HITL review window endpoint is
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
