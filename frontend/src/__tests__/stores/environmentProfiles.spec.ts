import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { Mock } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('../../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import { useEnvironmentProfilesStore } from '../../stores/environmentProfiles'
import type { EnvironmentProfile } from '../../stores/environmentProfiles'

function okJsonResponse(data: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => data,
  } as unknown as Response
}

function errorResponse(status: number, message: string) {
  return {
    ok: false,
    status,
    statusText: 'Error',
    json: async () => ({ detail: message }),
  } as unknown as Response
}

let fetchMock: Mock

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

const profile = (overrides: Partial<EnvironmentProfile> = {}): EnvironmentProfile => ({
  id: 'env-1',
  name: 'Sandbox',
  description: 'Isolated sandbox',
  provider_type: 'e2b',
  image_ref: 'modulo/sandbox:latest',
  capabilities: ['code_exec', 'browser'],
  network_policy: 'isolated',
  initialisation_strategy: 'lazy',
  persistence_policy: 'ephemeral',
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
})

describe('useEnvironmentProfilesStore', () => {
  it('starts with default state', () => {
    const store = useEnvironmentProfilesStore()
    expect(store.profiles).toEqual([])
    expect(store.currentProfile).toBeNull()
    expect(store.isLoading).toBe(false)
    expect(store.isSaving).toBe(false)
    expect(store.error).toBeNull()
  })

  it('fetchProfiles populates the profile list from the API', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [profile()] }))
    const store = useEnvironmentProfilesStore()

    await store.fetchProfiles()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/environment-profiles', expect.objectContaining({ method: 'GET' }))
    expect(store.profiles).toHaveLength(1)
    expect(store.profiles[0].name).toBe('Sandbox')
    expect(store.isLoading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('fetchProfiles tolerates a missing items key', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useEnvironmentProfilesStore()

    await store.fetchProfiles()

    expect(store.profiles).toEqual([])
    expect(store.error).toBeNull()
  })

  it('fetchProfiles ignores re-entrant calls while a fetch is in flight', async () => {
    fetchMock.mockImplementation(() => new Promise(() => {}))
    const store = useEnvironmentProfilesStore()

    const first = store.fetchProfiles()
    const second = store.fetchProfiles()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    first.finally(() => {})
    second.finally(() => {})
  })

  it('fetchProfiles surfaces the API error and empties the list', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'boom'))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile()]

    await store.fetchProfiles()

    expect(store.error).toBe('boom')
    expect(store.profiles).toEqual([])
    expect(store.isLoading).toBe(false)
  })

  it('fetchProfile loads the current profile', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile()))
    const store = useEnvironmentProfilesStore()

    await store.fetchProfile('env-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/environment-profiles/env-1', expect.objectContaining({ method: 'GET' }))
    expect(store.currentProfile?.id).toBe('env-1')
    expect(store.isLoading).toBe(false)
  })

  it('fetchProfile clears the current profile on error', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'Not found'))
    const store = useEnvironmentProfilesStore()
    store.currentProfile = profile()

    await store.fetchProfile('env-missing')

    expect(store.error).toBe('Not found')
    expect(store.currentProfile).toBeNull()
  })

  it('fetchProfile ignores re-entrant calls while a fetch is in flight', async () => {
    fetchMock.mockImplementation(() => new Promise(() => {}))
    const store = useEnvironmentProfilesStore()

    const first = store.fetchProfile('env-1')
    const second = store.fetchProfile('env-2')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    first.finally(() => {})
    second.finally(() => {})
  })

  it('createProfile posts and appends a summary to the list', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile({ id: 'env-new', name: 'New' })))
    const store = useEnvironmentProfilesStore()

    await store.createProfile({ name: 'New', provider_type: 'e2b' })

    const [, init] = fetchMock.mock.calls[0]
    expect(init).toMatchObject({ method: 'POST', body: JSON.stringify({ name: 'New', provider_type: 'e2b' }) })
    expect(store.profiles).toHaveLength(1)
    expect(store.profiles[0].id).toBe('env-new')
    expect(store.profiles[0].name).toBe('New')
    expect(store.isSaving).toBe(false)
  })

  it('createProfile rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(409, 'Duplicate name'))
    const store = useEnvironmentProfilesStore()

    await expect(store.createProfile({ name: 'New' })).rejects.toThrow('Duplicate name')
    expect(store.error).toBe('Duplicate name')
    expect(store.isSaving).toBe(false)
    expect(store.profiles).toEqual([])
  })

  it('updateProfile replaces the matching list item', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile({ name: 'Renamed' })))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile()]

    await store.updateProfile('env-1', { name: 'Renamed' })

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/environment-profiles/env-1', expect.objectContaining({ method: 'PUT' }))
    expect(store.profiles).toHaveLength(1)
    expect(store.profiles[0].name).toBe('Renamed')
    expect(store.isSaving).toBe(false)
  })

  it('updateProfile leaves the list untouched when the id is absent', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile({ id: 'env-other' })))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile()]

    await store.updateProfile('env-missing', { name: 'Renamed' })

    expect(store.profiles[0].name).toBe('Sandbox')
  })

  it('updateProfile refreshes the current profile when it is the target', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile({ name: 'Current v2' })))
    const store = useEnvironmentProfilesStore()
    store.currentProfile = profile()

    await store.updateProfile('env-1', { name: 'Current v2' })

    expect(store.currentProfile?.name).toBe('Current v2')
  })

  it('updateProfile does not clobber an unrelated current profile', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(profile({ id: 'env-other', name: 'Other' })))
    const store = useEnvironmentProfilesStore()
    store.currentProfile = profile({ id: 'env-current', name: 'Keep me' })

    await store.updateProfile('env-1', { name: 'Other' })

    expect(store.currentProfile?.id).toBe('env-current')
    expect(store.currentProfile?.name).toBe('Keep me')
  })

  it('updateProfile rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'update failed'))
    const store = useEnvironmentProfilesStore()

    await expect(store.updateProfile('env-1', { name: 'x' })).rejects.toThrow('update failed')
    expect(store.error).toBe('update failed')
    expect(store.isSaving).toBe(false)
  })

  it('deleteProfile removes the item and its current-profile reference', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile(), profile({ id: 'env-2', name: 'Second' })]
    store.currentProfile = profile()

    await store.deleteProfile('env-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/environment-profiles/env-1', expect.objectContaining({ method: 'DELETE' }))
    expect(store.profiles.map((p) => p.id)).toEqual(['env-2'])
    expect(store.currentProfile).toBeNull()
  })

  it('deleteProfile keeps the current profile when deleting another id', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile()]
    store.currentProfile = profile()

    await store.deleteProfile('env-other')

    expect(store.currentProfile?.id).toBe('env-1')
  })

  it('deleteProfile rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(403, 'forbidden'))
    const store = useEnvironmentProfilesStore()
    store.profiles = [profile()]

    await expect(store.deleteProfile('env-1')).rejects.toThrow('forbidden')
    expect(store.error).toBe('forbidden')
    expect(store.profiles).toHaveLength(1)
    expect(store.isSaving).toBe(false)
  })
})
