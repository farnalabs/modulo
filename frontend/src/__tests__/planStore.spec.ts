import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { usePlanStore } from '../stores/planStore'
import { getHandlers, clearAllRegistrations } from '../stores/syncRegistry'
import type { EventBusEvent } from '../types/events'

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
    { tier_id: 'v1', label: 'V1', rank: 2 },
    { tier_id: 'v2', label: 'V2', rank: 3 },
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
    GET: vi.fn(),
    PUT: vi.fn(),
    DELETE: vi.fn(),
  },
}))

async function mockApiSuccess() {
  const { api } = await import('../lib/api/client')
  ;(api.GET as any).mockImplementation((path: string) => {
    if (path === '/api/v1/admin/feature-flags') {
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    }
    if (path === '/api/v1/admin/license') {
      return Promise.resolve({ data: mockLicenseResponse, error: null })
    }
    if (path === '/api/v1/admin/tiers') {
      return Promise.resolve({ data: mockTiersResponse, error: null })
    }
    if (path.includes('/org-override')) {
      return Promise.resolve({ data: { override: null }, error: null })
    }
    return Promise.resolve({ data: null, error: null })
  })
  ;(api.PUT as any).mockResolvedValue({ data: null, error: null })
  ;(api.DELETE as any).mockResolvedValue({ data: null, error: null })
}

function syncEvent(overrides: Partial<EventBusEvent> = {}): EventBusEvent {
  return {
    type: 'team',
    id: 'evt-1',
    action: 'updated',
    version: 1,
    org_id: 'org-1',
    ...overrides,
  }
}

describe('usePlanStore', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    clearAllRegistrations()
    await mockApiSuccess()
  })

  it('starts with default state', () => {
    const store = usePlanStore()
    expect(store.currentTier).toBe('community')
    expect(store.features).toEqual({})
    expect(store.isLoading).toBe(false)
    expect(store.isTeam).toBe(false)
    expect(store.devMode).toBe(false)
    expect(store.loaded).toBe(false)
    expect(store.error).toBeNull()
    expect(store.expiresAt).toBeNull()
    expect(store.orgId).toBeNull()
    expect(store.tierLabels).toEqual({})
  })

  it('fetchPlan populates state from API', async () => {
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

  it('sets devMode and loaded from the feature-flags payload', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: { ...mockFlagsResponse, dev_mode: true }, error: null })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      return Promise.resolve({ data: mockTiersResponse, error: null })
    })

    const store = usePlanStore()
    expect(store.loaded).toBe(false)
    await store.fetchPlan()

    expect(store.devMode).toBe(true)
    expect(store.loaded).toBe(true)
  })

  it('populates tier labels and ranks from the tiers endpoint', async () => {
    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.tierLabels).toEqual({ community: 'Community', team: 'Team', v1: 'V1', v2: 'V2' })
    expect(store.tierRanks).toEqual({ community: 0, team: 1, v1: 2, v2: 3 })
    expect(store.isAtMinimumTier('community')).toBe(true)
    expect(store.isAtMinimumTier('v2')).toBe(false)
  })

  it('fetchPlan populates license info from license endpoint', async () => {
    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.expiresAt).toBe('2026-12-31T23:59:59Z')
    expect(store.orgId).toBe('Acme Corp')
    expect(store.currentTier).toBe('team')
  })

  it('featureEnabled returns correct boolean', async () => {
    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.featureEnabled('parallel_branches')).toBe(true)
    expect(store.featureEnabled('eval_system')).toBe(false)
    expect(store.featureEnabled('nonexistent')).toBe(false)
  })

  it('featureEnabled prefers an explicit org override over the global flag', async () => {
    const store = usePlanStore()
    await store.fetchPlan()
    expect(store.featureEnabled('parallel_branches')).toBe(true)

    store.orgOverrides.parallel_branches = false
    expect(store.featureEnabled('parallel_branches')).toBe(false)

    store.orgOverrides.parallel_branches = true
    expect(store.featureEnabled('parallel_branches')).toBe(true)

    store.orgOverrides.parallel_branches = null
    expect(store.featureEnabled('parallel_branches')).toBe(true)

    delete store.orgOverrides.parallel_branches
    expect(store.featureEnabled('parallel_branches')).toBe(true)
  })

  it('getTierLabel falls back to a capitalized tier id for unknown tiers', async () => {
    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.getTierLabel('team')).toBe('Team')
    expect(store.getTierLabel('v1')).toBe('V1')
    expect(store.getTierLabel('enterprise')).toBe('Enterprise')
  })

  it('isAtMinimumTier returns false when either tier is unknown', async () => {
    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.isAtMinimumTier('enterprise')).toBe(false)
    store.currentTier = 'premium'
    expect(store.isAtMinimumTier('team')).toBe(false)
    expect(store.isTeam).toBe(false)
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

  it('reports a license endpoint error while keeping flags and tiers state', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: null, error: 'License endpoint down' })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: mockTiersResponse, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.error).toContain('License endpoint down')
    expect(store.currentTier).toBe('team')
    expect(store.expiresAt).toBeNull()
    expect(store.orgId).toBeNull()
    expect(store.loaded).toBe(true)
  })

  it('aggregates partial failures while keeping successful endpoint state', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') {
        return Promise.resolve({ data: null, error: 'Flags endpoint down' })
      }
      if (path === '/api/v1/admin/tiers') {
        return Promise.resolve({ data: null, error: 'Tiers endpoint down' })
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      return Promise.resolve({ data: null, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.error).toContain('Flags endpoint down')
    expect(store.error).toContain('Tiers endpoint down')
    expect(store.currentTier).toBe('team')
    expect(store.expiresAt).toBe('2026-12-31T23:59:59Z')
    expect(store.orgId).toBe('Acme Corp')
    expect(store.loaded).toBe(false)
    expect(store.isLoading).toBe(false)
  })

  it('surfaces rejection reasons from failed requests', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/tiers') {
        return Promise.reject(new Error('tiers timeout'))
      }
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })

    const store = usePlanStore()
    await store.fetchPlan()

    expect(store.error).toContain('tiers timeout')
    expect(store.currentTier).toBe('team')
    expect(store.isLoading).toBe(false)
  })

  it('clears a previous error before re-fetching', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockRejectedValue(new Error('boom'))

    const store = usePlanStore()
    await store.fetchPlan()
    expect(store.error).toContain('boom')

    await mockApiSuccess()
    await store.fetchPlan()

    expect(store.error).toBeNull()
    expect(store.currentTier).toBe('team')
    expect(store.loaded).toBe(true)
  })

  it('toggles isLoading while the plan fetch is in flight', async () => {
    const { api } = await import('../lib/api/client')
    let releaseFlags!: (value: { data: unknown; error: null }) => void
    const flagsPromise = new Promise<{ data: unknown; error: null }>(resolve => {
      releaseFlags = resolve
    })
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path === '/api/v1/admin/feature-flags') return flagsPromise
      if (path === '/api/v1/admin/license') {
        return Promise.resolve({ data: mockLicenseResponse, error: null })
      }
      return Promise.resolve({ data: mockTiersResponse, error: null })
    })

    const store = usePlanStore()
    const pending = store.fetchPlan()
    expect(store.isLoading).toBe(true)

    releaseFlags({ data: mockFlagsResponse, error: null })
    await pending

    expect(store.isLoading).toBe(false)
    expect(store.currentTier).toBe('team')
  })

  it('deduplicates concurrent fetchPlan calls into a single network round', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockClear()
    const store = usePlanStore()

    await Promise.all([store.fetchPlan(), store.fetchPlan(), store.fetchPlan()])

    expect(api.GET).toHaveBeenCalledTimes(3)
    expect(store.currentTier).toBe('team')
  })

  it('fetchOrgFlagOverride returns the org override when present', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()

    ;(api.GET as any).mockImplementation((path: string) => {
      if (path.includes('/org-override')) {
        return Promise.resolve({ data: { override: true }, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })
    expect(await store.fetchOrgFlagOverride('parallel_branches')).toBe(true)

    ;(api.GET as any).mockImplementation((path: string) => {
      if (path.includes('/org-override')) {
        return Promise.resolve({ data: { override: false }, error: null })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })
    expect(await store.fetchOrgFlagOverride('parallel_branches')).toBe(false)
  })

  it('fetchOrgFlagOverride returns null when no override is configured', async () => {
    const store = usePlanStore()
    expect(await store.fetchOrgFlagOverride('parallel_branches')).toBeNull()
  })

  it('fetchOrgFlagOverride returns null on an API error', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.GET as any).mockImplementation((path: string) => {
      if (path.includes('/org-override')) {
        return Promise.resolve({ data: null, error: 'Forbidden' })
      }
      return Promise.resolve({ data: mockFlagsResponse, error: null })
    })

    const store = usePlanStore()
    expect(await store.fetchOrgFlagOverride('parallel_branches')).toBeNull()
  })

  it('fetchOrgFlagOverride returns null when the request throws', async () => {
    const { api } = await import('../lib/api/client')
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ;(api.GET as any).mockRejectedValue(new TypeError('Failed to fetch'))

    const store = usePlanStore()
    expect(await store.fetchOrgFlagOverride('parallel_branches')).toBeNull()
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
  })

  it('setOrgFlagOverride enables a feature via PUT and updates the override', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()

    const result = await store.setOrgFlagOverride('parallel_branches', true)

    expect(result).toBe(true)
    expect(api.PUT).toHaveBeenCalledWith(
      '/api/v1/admin/feature-flags/{flag_name}/org-override',
      { params: { path: { flag_name: 'parallel_branches' } }, body: { enabled: true } },
    )
    expect(store.orgOverrides.parallel_branches).toBe(true)
    expect(store.featureEnabled('parallel_branches')).toBe(true)
  })

  it('setOrgFlagOverride disables a feature via PUT', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    expect(store.featureEnabled('parallel_branches')).toBe(true)

    const result = await store.setOrgFlagOverride('parallel_branches', false)

    expect(result).toBe(true)
    expect(api.PUT).toHaveBeenCalledWith(
      '/api/v1/admin/feature-flags/{flag_name}/org-override',
      { params: { path: { flag_name: 'parallel_branches' } }, body: { enabled: false } },
    )
    expect(store.orgOverrides.parallel_branches).toBe(false)
    expect(store.featureEnabled('parallel_branches')).toBe(false)
  })

  it('setOrgFlagOverride with null clears the override via DELETE', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    await store.setOrgFlagOverride('parallel_branches', false)
    expect(store.featureEnabled('parallel_branches')).toBe(false)

    const result = await store.setOrgFlagOverride('parallel_branches', null)

    expect(result).toBe(true)
    expect(api.DELETE).toHaveBeenCalledWith(
      '/api/v1/admin/feature-flags/{flag_name}/org-override',
      { params: { path: { flag_name: 'parallel_branches' } } },
    )
    expect(store.orgOverrides.parallel_branches).toBeNull()
    expect(store.featureEnabled('parallel_branches')).toBe(true)
  })

  it('setOrgFlagOverride returns false when the API reports an error', async () => {
    const { api } = await import('../lib/api/client')
    ;(api.PUT as any).mockResolvedValue({ data: null, error: 'Conflict' })

    const store = usePlanStore()
    const result = await store.setOrgFlagOverride('parallel_branches', true)

    expect(result).toBe(false)
    expect(store.orgOverrides.parallel_branches).toBeUndefined()
  })

  it('setOrgFlagOverride returns false when the request throws', async () => {
    const { api } = await import('../lib/api/client')
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    ;(api.DELETE as any).mockRejectedValue(new TypeError('Failed to fetch'))

    const store = usePlanStore()
    const result = await store.setOrgFlagOverride('parallel_branches', null)

    expect(result).toBe(false)
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
  })

  it('re-fetches plan state when a team sync event arrives', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    const callsBefore = (api.GET as any).mock.calls.length

    for (const handler of getHandlers('team')) {
      handler(syncEvent())
    }

    await vi.waitFor(() => {
      expect((api.GET as any).mock.calls).toHaveLength(callsBefore + 3)
    })
  })

  it('re-fetches plan state for license and plan sync events too', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    const count = () => (api.GET as any).mock.calls.length

    for (const resource of ['license', 'plan'] as const) {
      const before = count()
      for (const handler of getHandlers(resource)) {
        handler(syncEvent({ type: resource, id: `evt-${resource}` }))
      }
      expect(store.isLoading).toBe(true)
      await vi.waitFor(() => expect(count()).toBe(before + 3))
      await vi.waitFor(() => expect(store.isLoading).toBe(false))
    }
  })

  it('deduplicates sync events by event id', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    const callsBefore = (api.GET as any).mock.calls.length
    const event = syncEvent()

    for (const handler of getHandlers('team')) handler(event)
    for (const handler of getHandlers('team')) handler(event)

    await vi.waitFor(() => {
      expect((api.GET as any).mock.calls).toHaveLength(callsBefore + 3)
    })
  })

  it('ignores sync events for unrelated resource types', async () => {
    const { api } = await import('../lib/api/client')
    const store = usePlanStore()
    await store.fetchPlan()
    const callsBefore = (api.GET as any).mock.calls.length

    for (const handler of getHandlers('team')) {
      handler(syncEvent({ type: 'run', id: 'evt-run' }))
    }

    expect((api.GET as any).mock.calls).toHaveLength(callsBefore)
  })

  it('disposeHandlers unregisters all sync handlers', async () => {
    const store = usePlanStore()
    expect(getHandlers('team').size).toBe(1)
    expect(getHandlers('license').size).toBe(1)
    expect(getHandlers('plan').size).toBe(1)

    store.disposeHandlers()

    expect(getHandlers('team').size).toBe(0)
    expect(getHandlers('license').size).toBe(0)
    expect(getHandlers('plan').size).toBe(0)
  })
})
