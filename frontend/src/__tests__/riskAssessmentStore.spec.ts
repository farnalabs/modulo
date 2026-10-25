import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const mockGet = vi.hoisted(() => vi.fn())
vi.mock('../lib/api/client', () => ({
  api: { GET: mockGet },
  getAccessToken: vi.fn().mockReturnValue('token'),
  clearAccessToken: vi.fn(),
}))

import { useRiskAssessmentStore } from '../stores/riskAssessment'
import {
  RISK_ACTIONS,
  buildRiskAssessmentQuery,
  defaultRiskAssessmentFilters,
  riskAssessmentRouteQuery,
} from '../lib/riskAssessment'

const payload = {
  summary: { total_rows: 2, action_counts: { block: 1, warn: 1 }, facet_degraded: false },
  rows: [],
}

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } }
}

beforeEach(() => {
  vi.clearAllMocks()
  setActivePinia(createPinia())
})

describe('risk assessment shared vocabulary', () => {
  it('exposes the four gate actions once', () => {
    expect(RISK_ACTIONS).toEqual(['block', 'escalate', 'warn', 'continue'])
  })

  it('builds the wire query from non-empty filters only', () => {
    expect(buildRiskAssessmentQuery(defaultRiskAssessmentFilters())).toEqual({ days: 7, limit: 100 })
    expect(
      buildRiskAssessmentQuery({
        keyPrefix: 'risk.',
        pipelineId: 'p-1',
        days: 30,
        action: 'block',
        limit: 10,
      }),
    ).toEqual({ key_prefix: 'risk.', pipeline_id: 'p-1', days: 30, action: 'block', limit: 10 })
  })

  it('omits zero-valued day and limit filters', () => {
    expect(
      buildRiskAssessmentQuery({ ...defaultRiskAssessmentFilters(), days: 0, limit: 0 }),
    ).toEqual({})
    expect(riskAssessmentRouteQuery({ ...defaultRiskAssessmentFilters(), days: 0 })).toEqual({
      days: '0',
    })
  })

  it('builds the deep-link query without the limit', () => {
    expect(riskAssessmentRouteQuery(defaultRiskAssessmentFilters())).toEqual({ days: '7' })
    expect(
      riskAssessmentRouteQuery({
        keyPrefix: 'risk.',
        pipelineId: 'p-1',
        days: 1,
        action: 'warn',
        limit: 100,
      }),
    ).toEqual({ key_prefix: 'risk.', pipeline_id: 'p-1', days: '1', action: 'warn' })
  })
})

describe('useRiskAssessmentStore', () => {
  it('loads the projection and exposes derived summary values', async () => {
    mockGet.mockResolvedValue(ok(payload))
    const store = useRiskAssessmentStore()

    await store.fetchRiskAssessment()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/evidence/risk-assessment', {
      params: { query: { days: 7, limit: 100 } },
    })
    expect(store.rows).toEqual([])
    expect(store.totalRows).toBe(2)
    expect(store.actionCounts).toEqual({ block: 1, warn: 1 })
    expect(store.facetDegraded).toBe(false)
    expect(store.isLoading).toBe(false)
    expect(store.error).toBeNull()
  })

  it('sends every configured filter', async () => {
    mockGet.mockResolvedValue(ok(payload))
    const store = useRiskAssessmentStore()
    store.setFilters({ keyPrefix: 'risk.', pipelineId: 'p-1', days: 30, action: 'block', limit: 10 })

    await store.fetchRiskAssessment()

    expect(mockGet).toHaveBeenCalledWith('/api/v1/evidence/risk-assessment', {
      params: {
        query: { key_prefix: 'risk.', pipeline_id: 'p-1', days: 30, action: 'block', limit: 10 },
      },
    })
  })

  it('records a problem-detail error', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: { detail: 'Database unavailable' },
      response: { ok: false, status: 503 },
    })
    const store = useRiskAssessmentStore()

    await store.fetchRiskAssessment()

    expect(store.error).toBe('Database unavailable')
    expect(store.isLoading).toBe(false)
  })

  it('records an HTTP error when the body is missing', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: undefined, response: { ok: true, status: 200 } })
    const store = useRiskAssessmentStore()

    await store.fetchRiskAssessment()

    expect(store.error).toBe('HTTP 200')
  })

  it('reports the HTTP status when the request fails without a problem body', async () => {
    mockGet.mockResolvedValue({
      data: undefined,
      error: undefined,
      response: { ok: false, status: 500 },
    })
    const store = useRiskAssessmentStore()

    await store.fetchRiskAssessment()

    expect(store.error).toBe('HTTP 500')
  })

  it('reports HTTP 0 when no response object is supplied', async () => {
    mockGet.mockResolvedValue({ data: undefined, error: undefined, response: undefined })
    const store = useRiskAssessmentStore()

    await store.fetchRiskAssessment()

    expect(store.error).toBe('HTTP 0')
  })

  it('does not issue a second request while one is in flight', async () => {
    const store = useRiskAssessmentStore()
    store.isLoading = true

    await store.fetchRiskAssessment()

    expect(mockGet).not.toHaveBeenCalled()
  })

  it('merges partial filters and resets to the shared defaults', () => {
    const store = useRiskAssessmentStore()

    store.setFilters({ action: 'warn' })
    expect(store.filters).toEqual({ ...defaultRiskAssessmentFilters(), action: 'warn' })

    store.resetFilters()
    expect(store.filters).toEqual(defaultRiskAssessmentFilters())
  })

  it('falls back to empty values before the first response', () => {
    const store = useRiskAssessmentStore()

    expect(store.rows).toEqual([])
    expect(store.summary).toBeUndefined()
    expect(store.totalRows).toBe(0)
    expect(store.actionCounts).toEqual({})
    expect(store.facetDegraded).toBe(false)
  })
})
