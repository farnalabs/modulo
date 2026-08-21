import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { Mock } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('../../lib/api/auth', () => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer token-1' })),
  attemptTokenRefresh: vi.fn(async () => true),
  clearAccessToken: vi.fn(),
  redirectToLogin: vi.fn(),
}))

import { useCompositeStore } from '../../stores/compositeStore'
import type { CompositeDefinition } from '../../types/pipeline'

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

const composite = (overrides: Partial<CompositeDefinition> = {}): CompositeDefinition => ({
  id: 'comp-1',
  name: 'Report Builder',
  description: null,
  version: '1.0.0',
  sub_pipeline_graph_json: {},
  parameter_ports_json: [],
  input_schema_id: null,
  output_schema_id: null,
  organisation_id: 'org-1',
  created_by: 'alice',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
})

describe('useCompositeStore', () => {
  it('starts with default state', () => {
    const store = useCompositeStore()
    expect(store.composites).toEqual([])
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('loadComposites populates the list and tolerates a missing items key', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [composite()] }))
    const store = useCompositeStore()

    await store.loadComposites()

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/composites', expect.objectContaining({ method: 'GET' }))
    expect(store.composites).toHaveLength(1)
    expect(store.composites[0].name).toBe('Report Builder')
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('loadComposites treats an empty payload as an empty list', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({}))
    const store = useCompositeStore()

    await store.loadComposites()

    expect(store.composites).toEqual([])
    expect(store.error).toBeNull()
  })

  it('loadComposites fetches only once across calls', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [composite()] }))
    const store = useCompositeStore()

    await store.loadComposites()
    await store.loadComposites()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('loadComposites ignores re-entrant calls while loading', async () => {
    fetchMock.mockImplementation(() => new Promise(() => {}))
    const store = useCompositeStore()

    const first = store.loadComposites()
    const second = store.loadComposites()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    first.finally(() => {})
    second.finally(() => {})
  })

  it('loadComposites surfaces the error and empties the list', async () => {
    fetchMock.mockResolvedValue(errorResponse(500, 'composites down'))
    const store = useCompositeStore()
    store.composites = [composite()]

    await store.loadComposites()

    expect(store.error).toBe('composites down')
    expect(store.composites).toEqual([])
    expect(store.loading).toBe(false)
  })

  it('loadComposites can retry successfully after a failure', async () => {
    fetchMock
      .mockResolvedValueOnce(errorResponse(500, 'transient'))
      .mockResolvedValueOnce(okJsonResponse({ items: [composite()] }))
    const store = useCompositeStore()

    await store.loadComposites()
    expect(store.error).toBe('transient')
    expect(store.composites).toEqual([])

    await store.loadComposites()
    expect(store.error).toBeNull()
    expect(store.composites).toHaveLength(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('getCompositeById returns the matching composite', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [composite(), composite({ id: 'comp-2', name: 'Other' })] }))
    const store = useCompositeStore()
    await store.loadComposites()

    expect(store.getCompositeById('comp-2')?.name).toBe('Other')
    expect(store.getCompositeById('comp-1')?.id).toBe('comp-1')
  })

  it('getCompositeById returns undefined for an unknown id', () => {
    const store = useCompositeStore()
    expect(store.getCompositeById('nope')).toBeUndefined()
  })

  it('disposeHandlers clears state and allows a fresh reload', async () => {
    fetchMock.mockResolvedValue(okJsonResponse({ items: [composite()] }))
    const store = useCompositeStore()
    await store.loadComposites()
    expect(store.composites).toHaveLength(1)

    store.disposeHandlers()
    expect(store.composites).toEqual([])
    expect(store.error).toBeNull()

    await store.loadComposites()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(store.composites).toHaveLength(1)
  })
})
