import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  PALETTE_PROVIDERS,
  connectorsProvider,
  pipelinesProvider,
  recentsProvider,
  resetPaletteProviderCaches,
  runsProvider,
  schemasProvider,
} from '../lib/commandPalette/providers'
import { clearRecentVisits, recordRecentVisit } from '../lib/commandPalette/recents'

const { apiGetMock, fetchRunsMock } = vi.hoisted(() => ({
  apiGetMock: vi.fn(),
  fetchRunsMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: { GET: apiGetMock },
  getAccessToken: vi.fn(() => null),
  isDemoSession: vi.fn(() => false),
}))

vi.mock('../lib/api/runs', () => ({
  fetchRuns: fetchRunsMock,
}))

function ok(items: unknown[]) {
  return { data: { items }, error: undefined, response: { status: 200 } }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetPaletteProviderCaches()
  clearRecentVisits()
})

describe('command-palette user-data providers (FAR-53)', () => {
  it('registers every source, recents first', () => {
    expect(PALETTE_PROVIDERS.map((provider) => provider.id)).toEqual([
      'recents',
      'pipelines',
      'runs',
      'schemas',
      'connectors',
    ])
  })

  it('maps and filters pipelines through the same endpoint the views use', async () => {
    apiGetMock.mockResolvedValue(ok([
      { id: 'p1', name: 'Payments', description: 'pay flow' },
      { id: 'p2', name: 'Billing', description: 'invoices' },
    ]))

    const results = await pipelinesProvider.search('pay')
    expect(apiGetMock).toHaveBeenCalledWith('/api/v1/pipelines', {
      params: { query: { page_size: 100 } },
    })
    expect(results).toHaveLength(1)
    expect(results[0]).toMatchObject({
      id: 'pipeline:p1',
      label: 'Payments',
      subtitle: 'pay flow',
      icon: 'GitFork',
      path: '/pipelines/p1/editor',
      type: 'pipeline',
      sectionKey: 'components.CommandPalette.section_pipelines',
    })
  })

  it('maps schemas to the schema editor route', async () => {
    apiGetMock.mockResolvedValue(ok([{ id: 's1', name: 'Invoice', description: null }]))

    const results = await schemasProvider.search('inv')
    expect(apiGetMock).toHaveBeenCalledWith('/api/v1/schemas', {
      params: { query: { page_size: 100 } },
    })
    expect(results).toEqual([
      expect.objectContaining({
        id: 'schema:s1',
        label: 'Invoice',
        path: '/schemas/editor/s1',
        type: 'schema',
      }),
    ])
  })

  it('maps connectors to the connectors list page', async () => {
    apiGetMock.mockResolvedValue(ok([{ id: 'c1', name: 'GitHub', connector_type_id: 'github' }]))

    const results = await connectorsProvider.search('git')
    expect(apiGetMock).toHaveBeenCalledWith('/api/v1/connectors', {
      params: { query: { page_size: 100 } },
    })
    expect(results[0]).toMatchObject({
      id: 'connector:c1',
      label: 'GitHub',
      subtitle: 'github',
      path: '/admin/connectors',
      type: 'connector',
    })
  })

  it('uses the runs server-side search filter and human labels', async () => {
    fetchRunsMock.mockResolvedValue({
      items: [
        { run_id: 'abcdef12-3456', run_number: 42, pipeline_name: 'Payments' },
        { run_id: '99887766-0000', run_number: null, pipeline_name: 'Payments' },
      ],
    })

    const results = await runsProvider.search('pay')
    expect(fetchRunsMock).toHaveBeenCalledWith({ search: 'pay', page_size: 20 })
    expect(results).toEqual([
      expect.objectContaining({ label: '#42', subtitle: 'Payments', path: '/runs/abcdef12-3456', type: 'run' }),
      expect.objectContaining({ label: '#99887766', path: '/runs/99887766-0000' }),
    ])
  })

  it('returns an empty list when nothing matches', async () => {
    apiGetMock.mockResolvedValue(ok([{ id: 'p1', name: 'Payments', description: null }]))
    expect(await pipelinesProvider.search('zzz')).toEqual([])
  })

  it('fetches a list source once across queries, not per keystroke', async () => {
    apiGetMock.mockResolvedValue(ok([{ id: 'p1', name: 'Payments', description: null }]))

    await pipelinesProvider.search('pa')
    await pipelinesProvider.search('pay')
    await pipelinesProvider.search('paym')

    expect(apiGetMock).toHaveBeenCalledTimes(1)
  })

  it('disables a source the caller is not permitted to read, without retrying', async () => {
    apiGetMock.mockResolvedValue({
      data: undefined,
      error: { detail: 'Insufficient permissions' },
      response: { status: 403 },
    })

    expect(await connectorsProvider.search('gh')).toEqual([])
    expect(await connectorsProvider.search('github')).toEqual([])
    // A denied source is not re-requested on every keystroke.
    expect(apiGetMock).toHaveBeenCalledTimes(1)
  })

  it('surfaces non-permission failures so partial results can still render', async () => {
    apiGetMock.mockResolvedValue({
      data: undefined,
      error: { detail: 'Database unavailable' },
      response: { status: 503 },
    })

    await expect(connectorsProvider.search('gh')).rejects.toThrow('Database unavailable')
  })

  it('maps recent visits and narrows them by query', async () => {
    recordRecentVisit({ path: '/runs/r1', label: 'Run 1', icon: 'CirclePlay' })
    recordRecentVisit({ path: '/pipelines/p1/editor', label: 'Payments', icon: 'GitFork' })

    const all = await recentsProvider.search('')
    expect(all.map((item) => item.label)).toEqual(['Payments', 'Run 1'])
    expect(all[0]).toMatchObject({
      id: 'recent:/pipelines/p1/editor',
      icon: 'GitFork',
      path: '/pipelines/p1/editor',
      type: 'recent',
      sectionKey: 'components.CommandPalette.section_recent',
    })

    const narrowed = await recentsProvider.search('run')
    expect(narrowed.map((item) => item.label)).toEqual(['Run 1'])
  })
})
