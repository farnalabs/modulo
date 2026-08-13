import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { Mock } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('../../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import { useLifecycleMapsStore } from '../../stores/lifecycleMaps'
import type { LifecycleMap, LifecycleMapStage, LifecycleMapVersion, LifecycleStage } from '../../stores/lifecycleMaps'
import type { JourneySummary, LifecycleMapTransfer } from '../../types/lifecycleMap'

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

const stage = (overrides: Partial<LifecycleMapStage> = {}): LifecycleMapStage => ({
  id: 'stage-1',
  name: 'Build',
  description: null,
  type: 'modulo',
  owner_badge: null,
  graduated: false,
  pipeline_id: null,
  external_url: null,
  ...overrides,
})

const canvasStage = (overrides: Partial<LifecycleStage> = {}): LifecycleStage => ({
  id: 'stage-1',
  name: 'Build',
  type: 'modulo',
  x: 0,
  y: 0,
  pipeline_id: null,
  external_url: null,
  owner: null,
  ...overrides,
})

const map = (overrides: Partial<LifecycleMap> = {}): LifecycleMap => ({
  id: 'map-1',
  name: 'Launch Flow',
  description: null,
  owner: null,
  owner_team_id: null,
  stages: [stage(), stage({ id: 'stage-2', name: 'Prod', graduated: true, type: 'manual' })],
  transitions: [],
  versions: [{ version: 1, created_at: '2026-01-01T00:00:00Z', created_by: 'alice' }],
  current_version: 1,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
})

const version = (overrides: Partial<LifecycleMapVersion> = {}): LifecycleMapVersion => ({
  version: 2,
  created_at: '2026-01-02T00:00:00Z',
  created_by: 'alice',
  ...overrides,
})

const journey = (overrides: Partial<JourneySummary> = {}): JourneySummary => ({
  kind: 'pr',
  ref: '123',
  canonical_work_item_id: '00000000-0000-0000-0000-000000000001',
  current_stage: {
    map_id: 'map-1',
    version: 1,
    stage_id: 'stage-1',
    stage_name: 'Build',
    position: 0,
  },
  status: 'running',
  provenance: 'derived',
  run_count: 3,
  latest_run_id: '00000000-0000-0000-0000-000000000003',
  updated_at: '2026-01-03T00:00:00Z',
  ...overrides,
})

describe('useLifecycleMapsStore', () => {
  it('starts with default state', () => {
    const store = useLifecycleMapsStore()
    expect(store.maps).toEqual([])
    expect(store.currentMap).toBeNull()
    expect(store.currentMapVersion).toBeNull()
    expect(store.pipelines).toEqual([])
    expect(store.isLoading).toBe(false)
    expect(store.isLoadingDetail).toBe(false)
    expect(store.saving).toBe(false)
    expect(store.error).toBeNull()
    expect(store.detailError).toBeNull()
    expect(store.graduatedCount).toBe(0)
    expect(store.manualCount).toBe(0)
  })

  it('fetchMaps populates the list from a bare array response', async () => {
    fetchMock.mockResolvedValue(okJsonResponse([{ id: 'map-1', name: 'Launch Flow' }]))
    const store = useLifecycleMapsStore()

    await store.fetchMaps()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/lifecycle-maps', expect.objectContaining({ method: 'GET' }))
    expect(store.maps).toHaveLength(1)
    expect(store.maps[0].name).toBe('Launch Flow')
    expect(store.isLoading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('fetchMaps populates the list from an { items } envelope', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [{ id: 'map-1', name: 'Launch Flow' }] }))
    const store = useLifecycleMapsStore()

    await store.fetchMaps()

    expect(store.maps).toHaveLength(1)
    expect(store.maps[0].id).toBe('map-1')
  })

  it('fetchMaps tolerates an unexpected payload shape', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ weird: true }))
    const store = useLifecycleMapsStore()

    await store.fetchMaps()

    expect(store.maps).toEqual([])
    expect(store.error).toBeNull()
  })

  it('fetchMaps ignores re-entrant calls while a fetch is in flight', async () => {
    fetchMock.mockImplementation(() => new Promise(() => {}))
    const store = useLifecycleMapsStore()

    const first = store.fetchMaps()
    const second = store.fetchMaps()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    first.finally(() => {})
    second.finally(() => {})
  })

  it('fetchMaps surfaces the error and empties the list', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'maps down'))
    const store = useLifecycleMapsStore()
    store.maps = [{ id: 'old' } as never]

    await store.fetchMaps()

    expect(store.error).toBe('maps down')
    expect(store.maps).toEqual([])
    expect(store.isLoading).toBe(false)
  })

  it('fetchMap loads the current map and its version', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(map()))
    const store = useLifecycleMapsStore()

    await store.fetchMap('map-1')

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/lifecycle-maps/map-1', expect.objectContaining({ method: 'GET' }))
    expect(store.currentMap?.name).toBe('Launch Flow')
    expect(store.currentMapVersion).toBe(1)
    expect(store.isLoadingDetail).toBe(false)
    expect(store.detailError).toBeNull()
  })

  it('fetchMap reports not-found when the API returns falsy data', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(null))
    const store = useLifecycleMapsStore()

    await store.fetchMap('map-missing')

    expect(store.detailError).toBe('Lifecycle map not found')
    expect(store.currentMap).toBeNull()
  })

  it('fetchMap surfaces the error and clears the current map', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'gone'))
    const store = useLifecycleMapsStore()
    store.currentMap = map()

    await store.fetchMap('map-missing')

    expect(store.detailError).toBe('gone')
    expect(store.currentMap).toBeNull()
    expect(store.isLoadingDetail).toBe(false)
  })

  it('fetchMapVersion loads a specific historical version', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(map({ current_version: 3 })))
    const store = useLifecycleMapsStore()

    await store.fetchMapVersion('map-1', 2)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/versions/2',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(store.currentMapVersion).toBe(2)
  })

  it('fetchMapVersion reports not-found on falsy data', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(undefined))
    const store = useLifecycleMapsStore()

    await store.fetchMapVersion('map-1', 9)

    expect(store.detailError).toBe('Lifecycle map version not found')
    expect(store.currentMap).toBeNull()
  })

  it('derives graduated and manual counts from the current map', () => {
    const store = useLifecycleMapsStore()
    store.currentMap = map()
    expect(store.graduatedCount).toBe(1)
    expect(store.manualCount).toBe(1)
  })

  it('computeds are zero without a current map', () => {
    const store = useLifecycleMapsStore()
    expect(store.graduatedCount).toBe(0)
    expect(store.manualCount).toBe(0)
  })

  it('saveVersion posts stages/edges/notes and returns the version', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(version()))
    const store = useLifecycleMapsStore()
    const stages = [canvasStage()]
    const edges = [{ id: 'e1', source: 'stage-1', target: 'stage-2', trigger_type: null, trigger_description: null, condition: null, estimated_frequency: null }]

    const result = await store.saveVersion('map-1', stages, edges, 'second cut')

    const [, init] = fetchMock.mock.calls[0]
    expect(init).toMatchObject({ method: 'POST', body: JSON.stringify({ stages, edges, notes: 'second cut' }) })
    expect(result.version).toBe(2)
    expect(store.saving).toBe(false)
    expect(store.error).toBeNull()
  })

  it('saveVersion defaults notes to an empty string', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(version()))
    const store = useLifecycleMapsStore()

    await store.saveVersion('map-1', [], [])

    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({ stages: [], edges: [], notes: '' })
  })

  it('saveVersion rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(400, 'bad stages'))
    const store = useLifecycleMapsStore()

    await expect(store.saveVersion('map-1', [], [])).rejects.toThrow('bad stages')
    expect(store.error).toBe('bad stages')
    expect(store.saving).toBe(false)
  })

  it('updateVersion PUTs the version payload and returns it', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(version({ version: 3 })))
    const store = useLifecycleMapsStore()

    const result = await store.updateVersion('map-1', 'version-2', [canvasStage()], [])

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/versions/version-2',
      expect.objectContaining({ method: 'PUT' }),
    )
    expect(result.version).toBe(3)
  })

  it('updateVersion rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(409, 'conflict'))
    const store = useLifecycleMapsStore()

    await expect(store.updateVersion('map-1', 'version-2', [], [])).rejects.toThrow('conflict')
    expect(store.error).toBe('conflict')
    expect(store.saving).toBe(false)
  })

  it('graduateStage PATCHes the pipeline id and returns the version', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(version()))
    const store = useLifecycleMapsStore()

    const result = await store.graduateStage('map-1', 'version-1', 'stage-1', 'pipe-9')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/versions/version-1/stages/stage-1/graduate',
      expect.objectContaining({ method: 'PATCH' }),
    )
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({ pipeline_id: 'pipe-9' })
    expect(result.version).toBe(2)
  })

  it('graduateStage rethrows and records the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(422, 'invalid pipeline'))
    const store = useLifecycleMapsStore()

    await expect(store.graduateStage('map-1', 'version-1', 'stage-1', 'pipe-9')).rejects.toThrow('invalid pipeline')
    expect(store.error).toBe('invalid pipeline')
    expect(store.saving).toBe(false)
  })

  it('fetchPipelines populates the pipeline options', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [{ id: 'pipe-1', name: 'Deploy' }] }))
    const store = useLifecycleMapsStore()

    await store.fetchPipelines()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/pipelines?limit=200', expect.objectContaining({ method: 'GET' }))
    expect(store.pipelines).toHaveLength(1)
    expect(store.pipelines[0].name).toBe('Deploy')
  })

  it('fetchPipelines tolerates a missing items key', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useLifecycleMapsStore()

    await store.fetchPipelines()

    expect(store.pipelines).toEqual([])
    expect(store.error).toBeNull()
  })

  it('fetchPipelines records the error and leaves pipelines empty', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'pipelines down'))
    const store = useLifecycleMapsStore()
    store.pipelines = [{ id: 'stale' } as never]

    await store.fetchPipelines()

    expect(store.pipelines).toEqual([])
    expect(store.error).toBe('pipelines down')
  })

  it('fetchJourneys populates the journeys list', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [journey()], next_cursor: null }))
    const store = useLifecycleMapsStore()

    await store.fetchJourneys('map-1')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/journeys?limit=200',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(store.journeys).toHaveLength(1)
    expect(store.journeys[0].kind).toBe('pr')
    expect(store.isLoadingJourneys).toBe(false)
    expect(store.journeysError).toBeNull()
  })

  it('fetchJourneys tolerates a missing items key', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useLifecycleMapsStore()

    await store.fetchJourneys('map-1')

    expect(store.journeys).toEqual([])
    expect(store.journeysError).toBeNull()
  })

  it('fetchJourneys groups journeys by their current stage', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({
      items: [
        journey(),
        journey({ ref: '456', current_stage: { ...journey().current_stage!, stage_id: 'stage-2' } }),
      ],
    }))
    const store = useLifecycleMapsStore()

    await store.fetchJourneys('map-1')

    expect(store.journeysByStage['stage-1']).toHaveLength(1)
    expect(store.journeysByStage['stage-2']).toHaveLength(1)
    expect(store.journeysByStage['stage-1'][0].ref).toBe('123')
  })

  it('fetchJourneys drops journeys without a current stage from the stage grouping', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({
      items: [journey({ current_stage: null })],
    }))
    const store = useLifecycleMapsStore()

    await store.fetchJourneys('map-1')

    expect(store.journeys).toHaveLength(1)
    expect(store.journeysByStage).toEqual({})
  })

  it('fetchJourneys surfaces the error and empties the list', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'map gone'))
    const store = useLifecycleMapsStore()
    store.journeys = [journey()]

    await store.fetchJourneys('map-1')

    expect(store.journeys).toEqual([])
    expect(store.journeysError).toBe('map gone')
    expect(store.isLoadingJourneys).toBe(false)
  })

  it('fetchJourneyDetail loads the journey with its run history', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({
      ...journey(),
      runs: [{ run_id: 'run-1', status: 'complete', completed_at: '2026-01-04T00:00:00Z', provenance: 'derived' }],
    }))
    const store = useLifecycleMapsStore()

    await store.fetchJourneyDetail('map-1', 'pr', '123')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/journeys/pr/123',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(store.selectedJourneyKey).toBe('pr:123')
    expect(store.journeyDetail?.runs).toHaveLength(1)
    expect(store.journeyDetail?.runs[0].status).toBe('complete')
    expect(store.isLoadingJourneyDetail).toBe(false)
  })

  it('fetchJourneyDetail URL-encodes the ref path segment', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ ...journey(), runs: [] }))
    const store = useLifecycleMapsStore()

    await store.fetchJourneyDetail('map-1', 'issue', 'PROJ/42')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/journeys/issue/PROJ%2F42',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('fetchJourneyDetail records the error and keeps the selected key', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'journey gone'))
    const store = useLifecycleMapsStore()

    await store.fetchJourneyDetail('map-1', 'pr', '999')

    expect(store.journeyDetailError).toBe('journey gone')
    expect(store.journeyDetail).toBeNull()
    expect(store.selectedJourneyKey).toBe('pr:999')
    expect(store.isLoadingJourneyDetail).toBe(false)
  })

  it('clearJourneyDetail resets the detail state', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ ...journey(), runs: [] }))
    const store = useLifecycleMapsStore()
    await store.fetchJourneyDetail('map-1', 'pr', '123')

    store.clearJourneyDetail()

    expect(store.journeyDetail).toBeNull()
    expect(store.journeyDetailError).toBeNull()
    expect(store.selectedJourneyKey).toBeNull()
  })

  it('exportMap fetches the export envelope for the map', async () => {
    const envelope = {
      primitive_type: 'lifecycle_map',
      format_version: '1',
      name: 'Launch Flow',
      description: null,
      content_json: { stages: [{ id: 'stage-1', name: 'Build', type: 'manual' }], edges: [] },
    }
    fetchMock.mockResolvedValue(okJsonResponse(envelope))
    const store = useLifecycleMapsStore()

    const result = await store.exportMap('map-1')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/map-1/export',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(result).toEqual(envelope)
  })

  it('exportMap rethrows the error', async () => {
    fetchMock.mockResolvedValue(errorResponse(404, 'map gone'))
    const store = useLifecycleMapsStore()

    await expect(store.exportMap('map-missing')).rejects.toThrow('map gone')
  })

  it('importMap posts the envelope and returns the created map', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ id: 'map-9', name: 'Imported SDLC' }))
    const store = useLifecycleMapsStore()
    const envelope: LifecycleMapTransfer = {
      primitive_type: 'lifecycle_map',
      format_version: '1',
      name: 'Imported SDLC',
      description: null,
      content_json: { stages: [], edges: [] },
    }

    const result = await store.importMap(envelope)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/lifecycle-maps/import',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(envelope) }),
    )
    expect(result.id).toBe('map-9')
    expect(store.error).toBeNull()
  })

  it('importMap throws when the API returns no map', async () => {
    fetchMock.mockResolvedValue(okJsonResponse(null))
    const store = useLifecycleMapsStore()

    await expect(
      store.importMap({ primitive_type: 'lifecycle_map', format_version: '1', name: 'X', description: null, content_json: {} } as never),
    ).rejects.toThrow('Import returned no map')
  })
})
