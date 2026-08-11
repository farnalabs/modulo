import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useViews, type SavedView } from '../../composables/useViews'

const mockViews: SavedView[] = [
  {
    id: 'v1',
    name: 'Active Runs',
    view_type: 'runs',
    filters: { status: 'active' },
    sort_by: 'created_at',
    sort_order: 'desc',
    created_by: 'user-1',
    created_at: '2026-01-15T10:00:00Z',
  },
  {
    id: 'v2',
    name: 'Completed Runs',
    view_type: 'runs',
    filters: { status: 'completed' },
    sort_order: 'desc',
    created_by: 'user-1',
    created_at: '2026-01-16T10:00:00Z',
  },
]

vi.mock('../../lib/api/client', () => ({
  api: {
    GET: vi.fn(),
  },
}))

describe('useViews', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('starts with default state', () => {
    const composable = useViews('runs')
    expect(composable.views.value).toEqual([])
    expect(composable.currentViewId.value).toBeNull()
    expect(composable.currentView.value).toBeNull()
    expect(composable.loading.value).toBe(false)
    expect(composable.error.value).toBeNull()
  })

  it('fetchViews populates views from API', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { items: mockViews }, error: null })

    const composable = useViews('runs')
    await composable.fetchViews()

    expect(composable.views.value).toEqual(mockViews)
    expect(composable.loading.value).toBe(false)
    expect(composable.error.value).toBeNull()
  })

  it('fetchViews passes view_type query param', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { items: [] }, error: null })

    const composable = useViews('pipelines')
    await composable.fetchViews()

    expect(api.GET).toHaveBeenCalledWith('/api/v1/views', {
      params: { query: { view_type: 'pipelines' } },
    })
  })

  it('fetchViews sets error on API error', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: null, error: { message: 'Not found' } })

    const composable = useViews('runs')
    await composable.fetchViews()

    expect(composable.error.value).toBe('Not found')
    expect(composable.views.value).toEqual([])
    expect(composable.loading.value).toBe(false)
  })

  it('fetchViews catches exceptions', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockRejectedValue(new Error('Network failure'))

    const composable = useViews('runs')
    await composable.fetchViews()

    expect(composable.error.value).toBe('Network failure')
    expect(composable.loading.value).toBe(false)
  })

  it('setCurrentView updates current view', () => {
    const composable = useViews('runs')
    composable.setCurrentView('v1')
    expect(composable.currentViewId.value).toBe('v1')
  })

  it('setCurrentView with null clears current view', () => {
    const composable = useViews('runs')
    composable.setCurrentView('v1')
    composable.setCurrentView(null)
    expect(composable.currentViewId.value).toBeNull()
  })

  it('currentView returns matching view from list', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValue({ data: { items: mockViews }, error: null })

    const composable = useViews('runs')
    await composable.fetchViews()
    composable.setCurrentView('v1')

    expect(composable.currentView.value).toEqual(mockViews[0])
  })

  it('currentView returns null for unknown id', () => {
    const composable = useViews('runs')
    composable.setCurrentView('nonexistent')
    expect(composable.currentView.value).toBeNull()
  })

  it('flips loading while fetchViews is in flight', async () => {
    const { api } = await import('../../lib/api/client')
    let resolveGet!: (value: unknown) => void
    ;(api.GET as any).mockImplementation(() => new Promise((resolve) => { resolveGet = resolve }))

    const composable = useViews('runs')
    const pending = composable.fetchViews()

    expect(composable.loading.value).toBe(true)
    expect(composable.error.value).toBeNull()

    resolveGet({ data: { items: mockViews }, error: null })
    await pending
    expect(composable.loading.value).toBe(false)
  })

  it('ignores a second fetchViews call while already loading', async () => {
    const { api } = await import('../../lib/api/client')
    let resolveGet!: (value: unknown) => void
    ;(api.GET as any).mockImplementation(() => new Promise((resolve) => { resolveGet = resolve }))

    const composable = useViews('runs')
    const first = composable.fetchViews()
    const second = composable.fetchViews()

    resolveGet({ data: { items: mockViews }, error: null })
    await Promise.all([first, second])

    expect(api.GET).toHaveBeenCalledTimes(1)
    expect(composable.views.value).toEqual(mockViews)
  })

  it('force re-fetches even while loading', async () => {
    const { api } = await import('../../lib/api/client')
    let resolveFirst!: (value: unknown) => void
    let resolveSecond!: (value: unknown) => void
    let call = 0
    ;(api.GET as any).mockImplementation(() => {
      call++
      return new Promise((resolve) => {
        if (call === 1) resolveFirst = resolve
        else resolveSecond = resolve
      })
    })

    const composable = useViews('runs')
    const first = composable.fetchViews()

    const second = composable.fetchViews(true)
    resolveFirst({ data: { items: [] }, error: null })
    await first

    resolveSecond({ data: { items: mockViews }, error: null })
    await second

    expect(api.GET).toHaveBeenCalledTimes(2)
    expect(composable.views.value).toEqual(mockViews)
  })

  it('clears a previous error on a successful fetchViews', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValueOnce({ data: null, error: { message: 'boom' } })
    ;(api.GET as any).mockResolvedValueOnce({ data: { items: mockViews }, error: null })

    const composable = useViews('runs')
    await composable.fetchViews()
    expect(composable.error.value).toBe('boom')

    await composable.fetchViews()
    expect(composable.error.value).toBeNull()
    expect(composable.views.value).toEqual(mockViews)
  })

  it('keeps prior views when a subsequent fetch fails', async () => {
    const { api } = await import('../../lib/api/client')
    ;(api.GET as any).mockResolvedValueOnce({ data: { items: mockViews }, error: null })
    ;(api.GET as any).mockResolvedValueOnce({ data: null, error: { message: 'down' } })

    const composable = useViews('runs')
    await composable.fetchViews()
    await composable.fetchViews()

    expect(composable.error.value).toBe('down')
    expect(composable.views.value).toEqual(mockViews)
  })
})
