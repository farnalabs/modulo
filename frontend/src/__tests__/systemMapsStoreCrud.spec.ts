import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())
const mockDelete = vi.hoisted(() => vi.fn())
const mockGetAccessToken = vi.hoisted(() => vi.fn())
const mockPlanStore = vi.hoisted(() => ({
  loaded: true,
  currentTier: 'community',
  devMode: true,
  userPermissions: ['system_maps_access', 'system.config.manage'],
  fetchPlan: vi.fn(),
  fetchPermissions: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet, POST: mockPost, DELETE: mockDelete },
  getAccessToken: mockGetAccessToken,
}))
vi.mock('../stores/planStore', () => ({ usePlanStore: () => mockPlanStore }))
const mockIsOperator = vi.hoisted(() => ({ value: true }))
const mockIsSystemAdmin = vi.hoisted(() => ({ value: false }))
vi.mock('../composables/useCurrentUser', () => ({
  useCurrentUser: () => ({ isOperator: mockIsOperator, isSystemAdmin: mockIsSystemAdmin }),
}))

import { useSystemMapsStore } from '../stores/systemMaps'

describe('useSystemMapsStore CRUD', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
    mockDelete.mockReset()
    mockGetAccessToken.mockReturnValue('header.eyJzdWIiOiJvcGVyYXRvci0xIn0.signature')
  })

  it('fetches a page of maps and records pagination', async () => {
    mockGet.mockResolvedValue({ data: { items: [{ id: 'map-1' }], total: 41, page: 2, page_size: 20 } })
    const store = useSystemMapsStore()

    await store.fetchMaps(2)

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps', { params: { query: { page: 2, page_size: 20 } } })
    expect(store.maps).toHaveLength(1)
    expect(store.total).toBe(41)
    expect(store.page).toBe(2)
    expect(store.loaded).toBe(true)
  })

  it('defaults missing list fields and reports an error response', async () => {
    mockGet.mockResolvedValueOnce({ data: {} })
    const store = useSystemMapsStore()
    await store.fetchMaps()
    expect(store.maps).toEqual([])
    expect(store.total).toBe(0)
    expect(store.page).toBe(1)

    mockGet.mockResolvedValueOnce({ error: { detail: 'down' } })
    await store.fetchMaps()
    expect(store.error).toBe('Could not load system maps')
  })

  it('creates a map and reloads the first page', async () => {
    mockPost.mockResolvedValue({ data: { id: 'map-new' } })
    mockGet.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } })
    const store = useSystemMapsStore()

    const created = await store.createMap('New map', 'desc')

    expect(created).toEqual({ id: 'map-new' })
    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps', { body: { name: 'New map', description: 'desc', visibility: 'org', default_map: false } })
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps', { params: { query: { page: 1, page_size: 20 } } })
  })

  it('throws when creating a map fails', async () => {
    mockPost.mockResolvedValue({ error: { detail: 'exists' } })
    const store = useSystemMapsStore()

    await expect(store.createMap('New map')).rejects.toThrow('Could not create system map')
  })

  it('deletes a map and clears it when it is active', async () => {
    mockDelete.mockResolvedValue({ data: undefined })
    mockGet.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } })
    const store = useSystemMapsStore()
    store.activeMap = { id: 'map-1', name: 'Active' } as never
    store.graph = { nodes: [] } as never

    await store.deleteMap('map-1')

    expect(mockDelete).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}', { params: { path: { map_id: 'map-1' } } })
    expect(store.activeMap).toBeNull()
    expect(store.graph).toBeNull()
  })

  it('keeps a different active map and surfaces delete errors', async () => {
    mockDelete.mockResolvedValueOnce({ data: undefined })
    mockGet.mockResolvedValue({ data: { items: [], total: 0, page: 1, page_size: 20 } })
    const store = useSystemMapsStore()
    store.activeMap = { id: 'map-2', name: 'Other' } as never

    await store.deleteMap('map-1')
    expect((store.activeMap as { id?: string } | null)?.id).toBe('map-2')

    mockDelete.mockResolvedValueOnce({ error: { detail: 'nope' } })
    await expect(store.deleteMap('map-1')).rejects.toThrow('Could not delete system map')
  })

  it('loads a map without a published revision', async () => {
    mockGet.mockResolvedValue({ data: { id: 'map-1', name: 'Map', published_revision_id: null } })
    const store = useSystemMapsStore()

    await store.loadMap('map-1')

    expect(store.activeMap?.id).toBe('map-1')
    expect(store.graph).toBeNull()
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('loads the published revision graph with a map', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ data: { id: 'map-1', published_revision_id: 'rev-1' } })
      return Promise.resolve({ data: { revision: { id: 'rev-1', revision_number: 1 }, nodes: [], relationships: [] } })
    })
    const store = useSystemMapsStore()

    await store.loadMap('map-1')

    expect(store.activeRevision?.id).toBe('rev-1')
    expect(store.graph).toEqual({ revision: { id: 'rev-1', revision_number: 1 }, nodes: [], relationships: [] })
    expect(store.revisions.map((item) => item.id)).toEqual(['rev-1'])
  })

  it('reports map load failures', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'missing' } })
    const store = useSystemMapsStore()

    await store.loadMap('map-1')

    expect(store.error).toBe('Could not load system map')
  })

  it('accepts a bare revision response from loadRevision', async () => {
    mockGet.mockResolvedValue({ data: { id: 'rev-7', revision_number: 7 } })
    const store = useSystemMapsStore()

    await store.loadRevision('map-1', 'rev-7')

    expect(store.activeRevision?.id).toBe('rev-7')
    expect(store.graph).toBeNull()
  })

  it('does not duplicate an already known revision and reports failures', async () => {
    mockGet.mockResolvedValue({ data: { revision: { id: 'rev-1', revision_number: 1 }, nodes: [], relationships: [] } })
    const store = useSystemMapsStore()
    await store.loadRevision('map-1', 'rev-1')
    await store.loadRevision('map-1', 'rev-1')
    expect(store.revisions).toHaveLength(1)

    mockGet.mockResolvedValue({ error: { detail: 'missing' } })
    await expect(store.loadRevision('map-1', 'rev-2')).rejects.toThrow('Could not load map revision')
  })

  it('loads the complete revision history', async () => {
    mockGet.mockResolvedValue({ data: { system_map_id: 'map-1', revisions: [
      { revision: { id: 'rev-1', revision_number: 1 } },
      { revision: { id: 'rev-2', revision_number: 2 } },
    ] } })
    const store = useSystemMapsStore()

    await store.loadRevisionHistory('map-1')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/history', { params: { path: { map_id: 'map-1' } } })
    expect(store.revisions.map((item) => item.id)).toEqual(['rev-1', 'rev-2'])
  })

  it('reports revision history failures', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'down' } })
    const store = useSystemMapsStore()

    await expect(store.loadRevisionHistory('map-1')).rejects.toThrow('Could not load revision history')
  })

  it('loads the current revision through the version-aware endpoint', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}/revisions/current') {
        return Promise.resolve({ data: { revision: { id: 'rev-3', revision_number: 3 } } })
      }
      return Promise.resolve({ data: { revision: { id: 'rev-3', revision_number: 3 }, nodes: [], relationships: [] } })
    })
    const store = useSystemMapsStore()

    await store.loadCurrentRevision('map-1')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/current', { params: { path: { map_id: 'map-1' } } })
    expect(store.activeRevision?.id).toBe('rev-3')
  })

  it('loads the revision active at a timestamp and rejects an unmatched time', async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}/revisions/by-time') {
        return Promise.resolve({ data: { matched: true, requested_at: '2026-01-15T10:30:00Z', revision: { id: 'rev-2', revision_number: 2 } }, response: { status: 200 } })
      }
      return Promise.resolve({ data: { revision: { id: 'rev-2', revision_number: 2 }, nodes: [], relationships: [] } })
    })
    const store = useSystemMapsStore()

    await store.loadRevisionByTime('map-1', '2026-01-15T10:30:00Z')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/by-time', {
      params: { path: { map_id: 'map-1' }, query: { at: '2026-01-15T10:30:00Z' } },
    })
    expect(store.activeRevision?.id).toBe('rev-2')

    // The wire contract raises 404 (never matched=false) for an unmatched time.
    mockGet.mockResolvedValue({
      error: { detail: 'No revision active at 2025-01-01T00:00:00+00:00' },
      response: { status: 404 },
    })
    await expect(store.loadRevisionByTime('map-1', '2025-01-01T00:00:00Z')).rejects.toThrow('No revision was active at that time')
  })

  it('reports failures from the current and by-time endpoints', async () => {
    mockGet.mockResolvedValue({ error: { detail: 'missing' }, response: { status: 500 } })
    const store = useSystemMapsStore()

    await expect(store.loadCurrentRevision('map-1')).rejects.toThrow('missing')
    await expect(store.loadRevisionByTime('map-1', '2026-01-15T10:30:00Z')).rejects.toThrow('missing')
  })

  it('treats a contract-violating by-time response without a revision as a load failure', async () => {
    mockGet.mockResolvedValue({
      data: { matched: true, requested_at: '2026-01-15T10:30:00Z', revision: null },
      response: { status: 200 },
    })
    const store = useSystemMapsStore()

    await expect(store.loadRevisionByTime('map-1', '2026-01-15T10:30:00Z')).rejects.toThrow('Could not load the revision for that time')
  })

  it('saves a draft and appends the new revision', async () => {
    mockPost.mockResolvedValue({ data: { id: 'rev-2', revision_number: 2 } })
    mockGet.mockResolvedValue({ data: { revision: { id: 'rev-2', revision_number: 2 }, nodes: [], relationships: [] } })
    const store = useSystemMapsStore()

    const saved = await store.saveDraft('map-1', [], [], 'Manual save')

    expect(saved.id).toBe('rev-2')
    expect(store.revisions.map((item) => item.id)).toEqual(['rev-2'])
  })

  it('throws when saving a draft fails', async () => {
    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    const store = useSystemMapsStore()

    await expect(store.saveDraft('map-1', [], [], 'notes')).rejects.toThrow('Could not save draft revision')
  })

  it('publishes a revision and reloads the map', async () => {
    mockPost.mockResolvedValue({ data: undefined })
    mockGet.mockResolvedValue({ data: { id: 'map-1', published_revision_id: null } })
    const store = useSystemMapsStore()

    await store.publish('map-1', 'rev-1')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/publish', { params: { path: { map_id: 'map-1', rev_id: 'rev-1' } } })
    expect(mockGet).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}', { params: { path: { map_id: 'map-1' } } })

    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    await expect(store.publish('map-1', 'rev-1')).rejects.toThrow('Could not publish revision')
  })

  it('archives a revision and reloads the map', async () => {
    mockPost.mockResolvedValue({ data: undefined })
    mockGet.mockResolvedValue({ data: { id: 'map-1', published_revision_id: null } })
    const store = useSystemMapsStore()

    await store.archive('map-1', 'rev-1')

    expect(mockPost).toHaveBeenCalledWith('/api/v1/system-maps/{map_id}/revisions/{rev_id}/archive', { params: { path: { map_id: 'map-1', rev_id: 'rev-1' } } })

    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    await expect(store.archive('map-1', 'rev-1')).rejects.toThrow('Could not archive revision')
  })

  it('discovers a map and loads the created draft revision', async () => {
    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-9', draft_revision_id: 'rev-9', inferred_nodes: [], inferred_edges: [] } })
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ data: { id: 'map-9', published_revision_id: null } })
      return Promise.resolve({ data: { revision: { id: 'rev-9' }, nodes: [], relationships: [] } })
    })
    const store = useSystemMapsStore()

    const report = await store.discover('Discovered', 'desc')

    expect(report.draft_revision_id).toBe('rev-9')
    expect(store.discovery).toEqual(report)
    expect(store.activeRevision?.id).toBe('rev-9')
  })

  it('keeps a discovery report when loading its draft revision fails', async () => {
    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-9', draft_revision_id: 'rev-9', inferred_nodes: [], inferred_edges: [] } })
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ data: { id: 'map-9', published_revision_id: null } })
      return Promise.resolve({ error: { detail: 'missing' } })
    })
    const store = useSystemMapsStore()

    const report = await store.discover()

    expect(report.draft_map_id).toBe('map-9')
    expect(store.discovery).toEqual(report)
    expect(store.error).toBe('Could not load map revision')
  })

  it('skips draft revision loading when discovery returns none', async () => {
    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-9', inferred_nodes: [], inferred_edges: [] } })
    mockGet.mockResolvedValue({ data: { id: 'map-9', published_revision_id: null } })
    const store = useSystemMapsStore()

    await store.discover()

    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('throws when discovery fails', async () => {
    mockPost.mockResolvedValue({ error: { detail: 'nope' } })
    const store = useSystemMapsStore()

    await expect(store.discover()).rejects.toThrow('Could not discover system map')
  })

  it('clears all active authoring state', async () => {
    const store = useSystemMapsStore()
    store.activeMap = { id: 'map-1' } as never
    store.activeRevision = { id: 'rev-1' } as never
    store.graph = { nodes: [] } as never
    store.discovery = { inferred_nodes: [] } as never

    store.clearActive()

    expect(store.activeMap).toBeNull()
    expect(store.activeRevision).toBeNull()
    expect(store.graph).toBeNull()
    expect(store.discovery).toBeNull()
  })
})

describe('useSystemMapsStore derived access and non-Error failures', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
    mockPost.mockReset()
  })

  it('requires the flag, a role and the permission for hasAccess', () => {
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockIsOperator.value = true
    mockIsSystemAdmin.value = false
    let store = useSystemMapsStore()

    expect(store.hasAccess).toBe(false)

    store.systemMapsEnabled = true
    expect(store.hasAccess).toBe(true)

    setActivePinia(createPinia())
    mockPlanStore.userPermissions = []
    store = useSystemMapsStore()
    store.systemMapsEnabled = true
    expect(store.hasAccess).toBe(false)

    setActivePinia(createPinia())
    mockPlanStore.userPermissions = ['system_maps_access', 'system.config.manage']
    mockIsOperator.value = false
    mockIsSystemAdmin.value = true
    store = useSystemMapsStore()
    store.systemMapsEnabled = true
    expect(store.hasAccess).toBe(true)

    setActivePinia(createPinia())
    mockIsSystemAdmin.value = false
    store = useSystemMapsStore()
    store.systemMapsEnabled = true
    expect(store.hasAccess).toBe(false)
  })

  it('uses a localized message when loading a discovered draft fails with a non-Error', async () => {
    mockPost.mockResolvedValue({ data: { draft_map_id: 'map-9', draft_revision_id: 'rev-9' } })
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/v1/system-maps/{map_id}') return Promise.resolve({ data: { id: 'map-9', published_revision_id: null } })
      return Promise.reject('offline')
    })
    const store = useSystemMapsStore()

    const report = await store.discover()

    expect(report.draft_map_id).toBe('map-9')
    expect(store.error).toBe('Could not load the discovered draft')
  })
})
