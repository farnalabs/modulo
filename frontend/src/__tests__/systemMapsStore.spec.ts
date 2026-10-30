import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockIsOperator = vi.hoisted(() => ({ value: false }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
const mockPlanStore = vi.hoisted(() => ({
  loaded: false,
  currentTier: 'community',
  devMode: false,
  userPermissions: [] as string[],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))

import { useSystemMapsStore } from '../stores/systemMaps'

function jwtFor(sub: string, claims: Record<string, unknown> = {}): string {
  const payload = btoa(JSON.stringify({ sub, ...claims }))
  return `header.${payload}.signature`
}

function configureAuthorizedAccess(): void {
  mockIsOperator.value = true
  mockPlanStore.loaded = true
  mockPlanStore.devMode = true
  mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
}

describe('useSystemMapsStore access', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockGetAccessToken.mockReturnValue(jwtFor('operator-1'))
    mockIsOperator.value = false
    mockIsSystemAdmin.value = false
    mockPlanStore.loaded = false
    mockPlanStore.currentTier = 'community'
    mockPlanStore.devMode = false
    mockPlanStore.userPermissions = []
    mockPlanStore.fetchPlan.mockReset()
    mockPlanStore.fetchPermissions.mockReset()
  })

  it('does not probe the system-admin endpoint without system.config.manage', async () => {
    mockIsOperator.value = true
    mockPlanStore.loaded = true
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access']

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockGet).not.toHaveBeenCalled()
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
  })

  it('probes feature flags only when the caller has the real system capability', async () => {
    configureAuthorizedAccess()
    mockGet.mockResolvedValue({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/admin/feature-flags')
    expect(store.systemMapsEnabled).toBe(true)
  })

  it('allows a system admin with no org role to probe the system-admin endpoint', async () => {
    mockIsSystemAdmin.value = true
    mockPlanStore.loaded = true
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockGet.mockResolvedValue({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/admin/feature-flags')
    expect(store.systemMapsEnabled).toBe(true)
  })

  it('denies an ordinary admin without the system-admin capability', async () => {
    mockIsOperator.value = true
    mockPlanStore.loaded = true
    mockPlanStore.devMode = true
    mockPlanStore.userPermissions = ['system_maps_access']

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockGet).not.toHaveBeenCalled()
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('does not repeat an access probe after access has settled', async () => {
    configureAuthorizedAccess()
    mockGet.mockResolvedValue({ data: { flags: [] } })
    const store = useSystemMapsStore()

    await store.fetchAccess()
    await store.fetchAccess()

    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('resets access state when the token principal changes', async () => {
    configureAuthorizedAccess()
    mockGet.mockResolvedValue({ data: { flags: [] } })
    const store = useSystemMapsStore()

    await store.fetchAccess()
    expect(store.accessLoaded).toBe(true)

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    await store.fetchAccess()

    expect(mockGet).toHaveBeenCalledTimes(2)
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('revalidates when the same subject receives a demoted token', async () => {
    configureAuthorizedAccess()
    mockGet.mockResolvedValue({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    const store = useSystemMapsStore()

    await store.fetchAccess()
    expect(store.systemMapsEnabled).toBe(true)

    mockGetAccessToken.mockReturnValue(jwtFor('operator-1', { org_role: 'viewer' }))
    mockIsOperator.value = false
    mockPlanStore.userPermissions = []
    await store.fetchAccess()

    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(store.accessLoaded).toBe(true)
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('ignores an authorized response after a same-subject token is demoted', async () => {
    configureAuthorizedAccess()
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldRequest = resolve
        }),
    )
    const store = useSystemMapsStore()

    const oldRequest = store.fetchAccess()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-1', { org_role: 'viewer' }))
    mockIsOperator.value = false
    mockPlanStore.userPermissions = []
    const newRequest = store.fetchAccess()
    await newRequest

    resolveOldRequest?.({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    await oldRequest

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
    expect(store.error).toBeNull()
  })

  it('ignores an authorization error after a same-subject token is demoted', async () => {
    configureAuthorizedAccess()
    let rejectOldRequest: ((reason?: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectOldRequest = reject
        }),
    )
    const store = useSystemMapsStore()

    const oldRequest = store.fetchAccess()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-1', { org_role: 'viewer' }))
    mockIsOperator.value = false
    mockPlanStore.userPermissions = []
    await store.fetchAccess()

    rejectOldRequest?.(new Error('stale request failed'))
    await oldRequest

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.error).toBeNull()
    expect(store.accessLoaded).toBe(true)
  })

  it('does not let a previous principal re-enable access after a token replacement', async () => {
    configureAuthorizedAccess()
    let resolveOldRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOldRequest = resolve
        }),
    )
    const store = useSystemMapsStore()

    const oldRequest = store.fetchAccess()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    mockGetAccessToken.mockReturnValue(jwtFor('operator-2'))
    mockIsOperator.value = false
    mockPlanStore.userPermissions = []
    await store.fetchAccess()

    resolveOldRequest?.({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    await oldRequest

    expect(store.systemMapsEnabled).toBe(false)
    expect(store.error).toBeNull()
    expect(store.accessLoaded).toBe(true)
  })

  it('resets access state when reset is called', async () => {
    configureAuthorizedAccess()
    mockGet.mockResolvedValue({ data: { flags: [{ name: 'system_maps', currently_active: true }] } })
    const store = useSystemMapsStore()

    await store.fetchAccess()
    store.reset()

    expect(store.accessLoaded).toBe(false)
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.error).toBeNull()
    expect(store.loaded).toBe(false)
    expect(store.discoveryAvailable).toBe(false)
    expect(store.draft).toBeNull()
  })

  it('fails closed when no access token is present', async () => {
    mockGetAccessToken.mockReturnValue(null)
    const store = useSystemMapsStore()

    await store.fetchAccess()

    expect(mockGet).not.toHaveBeenCalled()
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
  })

  it('deduplicates concurrent access probes', async () => {
    configureAuthorizedAccess()
    let resolveRequest: ((value: unknown) => void) | undefined
    mockGet.mockImplementation(() => new Promise((resolve) => { resolveRequest = resolve }))
    const store = useSystemMapsStore()

    const first = store.fetchAccess()
    const second = store.fetchAccess()
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1))

    resolveRequest?.({ data: { flags: [] } })
    await Promise.all([first, second])
  })

  it('fetches the plan before permissions for an unloaded community-tier store', async () => {
    mockIsOperator.value = true
    mockPlanStore.currentTier = 'community'
    mockPlanStore.fetchPlan.mockResolvedValue(undefined)
    mockPlanStore.fetchPermissions.mockResolvedValue(undefined)

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockPlanStore.fetchPlan).toHaveBeenCalledTimes(1)
    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('does not fetch the plan for a known non-community tier', async () => {
    mockIsOperator.value = true
    mockPlanStore.currentTier = 'team'
    mockPlanStore.fetchPermissions.mockResolvedValue(undefined)

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(mockPlanStore.fetchPlan).not.toHaveBeenCalled()
    expect(mockPlanStore.fetchPermissions).toHaveBeenCalledTimes(1)
  })

  it('records an Error rejection and remains disabled', async () => {
    configureAuthorizedAccess()
    mockGet.mockRejectedValue(new Error('feature flags unavailable'))

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.error).toBe('feature flags unavailable')
    expect(store.systemMapsEnabled).toBe(false)
    expect(store.accessLoaded).toBe(true)
  })

  it('uses a stable message for non-Error failures', async () => {
    configureAuthorizedAccess()
    mockGet.mockRejectedValue('not an Error instance')

    const store = useSystemMapsStore()
    await store.fetchAccess()

    expect(store.error).toBe('Internal feature flags request failed')
    expect(store.systemMapsEnabled).toBe(false)
  })

  it('fails closed for malformed or missing feature-flag payloads', async () => {
    configureAuthorizedAccess()
    const store = useSystemMapsStore()

    for (const data of [undefined, {}, { flags: undefined }, { flags: 'not-an-array' }]) {
      mockGet.mockResolvedValueOnce({ data })
      store.reset()
      await store.fetchAccess()
      expect(store.systemMapsEnabled).toBe(false)
    }
  })

  it('keeps discovery fail-closed until its endpoint exists', async () => {
    const store = useSystemMapsStore()

    await store.fetchDiscovery()

    expect(store.loaded).toBe(true)
    expect(store.discoveryAvailable).toBe(false)
    expect(store.draft).toBeNull()
  })
})
