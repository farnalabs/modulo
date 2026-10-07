import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchErrorGroup,
  fetchErrorGroupEvents,
  fetchInstanceErrorGroup,
  fetchInstanceErrorGroupEvents,
  fetchInstanceErrorGroups,
} from '../lib/api/errors'
import type { ErrorGroupDetail, ErrorListResponse } from '../lib/api/errors'

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }))

vi.mock('../lib/api/client', () => ({
  api: { GET: apiGet, PATCH: vi.fn() },
  getAccessToken: vi.fn().mockReturnValue('mock-token'),
}))

const groupDetail: ErrorGroupDetail = {
  id: 'eg-1',
  fingerprint: 'abcdef1234567890',
  level_peak: 'error',
  status: 'new',
  count: 2,
  first_seen: '2026-08-01T10:00:00Z',
  last_seen: '2026-08-02T12:00:00Z',
  assigned_to: null,
} as unknown as ErrorGroupDetail

const listResponse: ErrorListResponse = {
  items: [{ id: 'eg-1' }],
  total: 1,
} as unknown as ErrorListResponse

describe('lib/api/errors — instance scope helpers (FAR-1555)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    apiGet.mockResolvedValue({ data: listResponse, error: undefined })
  })

  it('fetchInstanceErrorGroups reads the sentinel list endpoint with the shared filters', async () => {
    const result = await fetchInstanceErrorGroups({ level: 'error', search: 'boom', limit: 20, offset: 40 })

    expect(apiGet).toHaveBeenCalledWith('/api/v1/errors/instance', {
      params: { query: { level: 'error', search: 'boom', limit: 20, offset: 40 } },
    })
    expect(result).toBe(listResponse)
  })

  it('fetchInstanceErrorGroup reads the sentinel detail endpoint for the given group', async () => {
    apiGet.mockResolvedValue({ data: groupDetail, error: undefined })

    const result = await fetchInstanceErrorGroup('eg-1')

    expect(apiGet).toHaveBeenCalledWith('/api/v1/errors/instance/{error_id}', {
      params: { path: { error_id: 'eg-1' } },
    })
    expect(result).toBe(groupDetail)
  })

  it('fetchInstanceErrorGroupEvents reads the sentinel events endpoint with paging', async () => {
    const page = { items: [{ id: 'evt-1' }], total: 1 }
    apiGet.mockResolvedValue({ data: page, error: undefined })

    const result = await fetchInstanceErrorGroupEvents('eg-1', { limit: 20, offset: 20 })

    expect(apiGet).toHaveBeenCalledWith('/api/v1/errors/instance/{error_id}/events', {
      params: { path: { error_id: 'eg-1' }, query: { limit: 20, offset: 20 } },
    })
    expect(result).toBe(page)
  })

  it('keeps the throwOnError contract: an instance-read error surfaces as a thrown Error', async () => {
    apiGet.mockResolvedValue({ data: undefined, error: { detail: 'Forbidden' } })

    await expect(fetchInstanceErrorGroup('eg-1')).rejects.toThrow('Forbidden')
  })

  it('routes the tenant reads to the non-instance endpoints (contrast)', async () => {
    apiGet.mockResolvedValue({ data: groupDetail, error: undefined })
    await fetchErrorGroup('eg-1')
    expect(apiGet).toHaveBeenCalledWith('/api/v1/errors/{error_id}', {
      params: { path: { error_id: 'eg-1' } },
    })

    apiGet.mockResolvedValue({ data: { items: [], total: 0 }, error: undefined })
    await fetchErrorGroupEvents('eg-1', { limit: 20, offset: 0 })
    expect(apiGet).toHaveBeenLastCalledWith('/api/v1/errors/{error_id}/events', {
      params: { path: { error_id: 'eg-1' }, query: { limit: 20, offset: 0 } },
    })
  })
})
