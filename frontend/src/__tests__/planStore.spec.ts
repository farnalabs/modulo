import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'

const mockFlagsResponse = {
  license: { tier: 'team', has_license_key: true, is_valid: true },
  flags: [
    { name: 'parallel_branches', description: 'Parallel branches', tier: 'team', currently_active: true, depends_on: null },
    { name: 'eval_system', description: 'Eval system', tier: 'team', currently_active: false, depends_on: null },
    { name: 'hitl_gates', description: 'HITL gates', tier: 'community', currently_active: true, depends_on: null },
  ],
  would_activate: [],
}

const mockTiersResponse = {
  tiers: [
    { tier_id: 'community', label: 'Community', rank: 0 },
    { tier_id: 'team', label: 'Team', rank: 1 },
    { tier_id: 'enterprise', label: 'Enterprise', rank: 2 },
  ],
}

const mockLicenseResponse = {
  has_license: true,
  tier: 'team',
  features: [],
  expires_at: '2026-12-31T23:59:59Z',
  org_id: 'Acme Corp',
}

vi.mock('../lib/api/client', () => ({
  api: {
    GET: vi.fn().mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: mockFlagsResponse, error: null })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersResponse, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    }),
  },
}))

describe('usePlanStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts with default state', () => {
    const store = usePlanStore()
    expect(store.currentTier).toBe('community')
    expect(store.features).toEqual({})
    expect(store.isLoading).toBe(false)
    expect(store.isTeam).toBe(false)
  })

  it('fetchPlan populates state from API', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersResponse, error: null })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.currentTier).toBe('team')
    expect(store.features).toEqual({
      parallel_branches: true,
      eval_system: false,
      hitl_gates: true,
    })
    expect(store.isTeam).toBe(true)
    expect(store.isLoading).toBe(false)
  })

  it('featureEnabled returns correct boolean', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: mockFlagsResponse, error: null })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.featureEnabled('parallel_branches')).toBe(true)
    expect(store.featureEnabled('eval_system')).toBe(false)
    expect(store.featureEnabled('nonexistent')).toBe(false)
  })

  it('fetchPlan sets error on failure', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: null, error: 'Network error' })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.error).toContain('Network error')
    expect(store.isLoading).toBe(false)
  })

  it('fetchPlan catches exceptions', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockRejectedValue(new Error('Request failed'))

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.error).toContain('Request failed')
    expect(store.isLoading).toBe(false)
  })

  it('fetchPlan populates license info from license endpoint', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({
          data: { has_license: true, tier: 'team', features: [], expires_at: '2026-12-31T23:59:59Z', org_id: 'Acme Corp' },
          error: undefined,
        })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersResponse, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.expiresAt).toBe('2026-12-31T23:59:59Z')
    expect(store.orgName).toBe('Acme Corp')
    expect(store.currentTier).toBe('team')
  })
})
