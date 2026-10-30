import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({ api: { GET: mockGet } }))

import { useSystemMapsStore } from '../stores/systemMaps'

const published = { id: 'm1', name: 'Published', default_map: false, published_revision_id: 'rev-1' }
const defaultPublished = { id: 'm2', name: 'Default', default_map: true, published_revision_id: 'rev-2' }
const unpublished = { id: 'm3', name: 'Draft', default_map: false, published_revision_id: null }

function overviewFor(systemMapId: string) {
  return {
    system_map_id: systemMapId,
    revision_id: 'rev-1',
    revision_number: 1,
    generated_at: '2026-10-30T00:00:00Z',
    nodes: [],
    loop_configs: [],
    bottlenecks: [],
  }
}

function listPayload(items: unknown[]) {
  return { data: { items, total: items.length, page: 1, page_size: 100 } }
}

describe('useSystemMapsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockGet.mockReset()
  })

  it('selects the map referenced by the loaded overview', () => {
    const store = useSystemMapsStore()
    store.maps = [published, defaultPublished] as unknown as typeof store.maps
    store.overview = overviewFor('m1') as unknown as typeof store.overview

    expect(store.selectedMap?.id).toBe('m1')
  })

  it('returns null when the overview references a map that is not loaded', () => {
    const store = useSystemMapsStore()
    store.maps = [published] as unknown as typeof store.maps
    store.overview = overviewFor('missing') as unknown as typeof store.overview

    expect(store.selectedMap).toBeNull()
  })

  it('prefers the default published map when no overview is loaded', () => {
    const store = useSystemMapsStore()
    store.maps = [unpublished, published, defaultPublished] as unknown as typeof store.maps

    expect(store.selectedMap?.id).toBe('m2')
  })

  it('falls back to any published map when no default map is published', () => {
    const store = useSystemMapsStore()
    store.maps = [unpublished, published] as unknown as typeof store.maps

    expect(store.selectedMap?.id).toBe('m1')
  })

  it('returns null when no map is published', () => {
    const store = useSystemMapsStore()
    store.maps = [unpublished] as unknown as typeof store.maps

    expect(store.selectedMap).toBeNull()
  })

  it('reports whether any map has a published revision', () => {
    const store = useSystemMapsStore()

    store.maps = [unpublished] as unknown as typeof store.maps
    expect(store.hasPublishedMap).toBe(false)

    store.maps = [unpublished, published] as unknown as typeof store.maps
    expect(store.hasPublishedMap).toBe(true)
  })

  it('loads maps and the overview for the default published map', async () => {
    mockGet.mockImplementation((url: string) =>
      url === '/api/v1/system-maps'
        ? Promise.resolve(listPayload([unpublished, defaultPublished]))
        : Promise.resolve({ data: overviewFor('m2') }),
    )
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(store.maps.map((item) => item.id)).toEqual(['m3', 'm2'])
    expect(store.overview?.system_map_id).toBe('m2')
    expect(store.loaded).toBe(true)
    expect(store.loading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('stops after the list when no map is published', async () => {
    mockGet.mockResolvedValue(listPayload([unpublished]))
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(mockGet).toHaveBeenCalledTimes(1)
    expect(store.overview).toBeNull()
    expect(store.loaded).toBe(true)
  })

  it('treats a missing list body as an empty map list', async () => {
    mockGet.mockResolvedValue({ data: undefined })
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(store.maps).toEqual([])
    expect(store.loaded).toBe(true)
  })

  it('records an error when the list request fails', async () => {
    mockGet.mockResolvedValue({ error: new Error('list down') })
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(store.error).toContain('list down')
    expect(store.loaded).toBe(true)
    expect(store.loading).toBe(false)
  })

  it('records an error when the overview request fails', async () => {
    mockGet.mockImplementation((url: string) =>
      url === '/api/v1/system-maps'
        ? Promise.resolve(listPayload([defaultPublished]))
        : Promise.resolve({ error: new Error('overview down') }),
    )
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(store.error).toContain('overview down')
    expect(store.overview).toBeNull()
  })

  it('treats a missing overview body as no overview', async () => {
    mockGet.mockImplementation((url: string) =>
      url === '/api/v1/system-maps'
        ? Promise.resolve(listPayload([defaultPublished]))
        : Promise.resolve({ data: undefined }),
    )
    const store = useSystemMapsStore()

    await store.fetchOverview()

    expect(store.overview).toBeNull()
    expect(store.error).toBeNull()
  })
})
