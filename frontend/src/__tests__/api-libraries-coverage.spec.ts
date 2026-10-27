import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({
  api: {
    GET: getMock,
    POST: postMock,
  },
}))

import { fetchDbCapacity } from '../lib/api/dbCapacity'
import { fetchRuns, requestRunCancellation, requestRunRerun } from '../lib/api/runs'

const capacity = {
  capacity_percent: 87,
  mode: 'fixed',
  alert_level: 'warn',
  used_bytes: 870,
  capacity_bytes: 1000,
}

const runResponse = {
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
  next_cursor: null,
  has_more: false,
}

describe('database capacity API', () => {
  beforeEach(() => getMock.mockReset())

  it('returns a real capacity payload', async () => {
    getMock.mockResolvedValue({ data: capacity })

    await expect(fetchDbCapacity()).resolves.toEqual(capacity)
    expect(getMock).toHaveBeenCalledWith('/api/v1/admin/db-capacity', {})
  })

  it('returns null for an API error, missing data, or a thrown request', async () => {
    getMock.mockResolvedValueOnce({ error: { detail: 'forbidden' } })
    await expect(fetchDbCapacity()).resolves.toBeNull()

    getMock.mockResolvedValueOnce({ data: undefined })
    await expect(fetchDbCapacity()).resolves.toBeNull()

    getMock.mockRejectedValueOnce(new Error('network down'))
    await expect(fetchDbCapacity()).resolves.toBeNull()
  })
})

describe('runs API', () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
  })

  afterEach(() => vi.restoreAllMocks())

  it('serialises every supported list filter and returns the real payload', async () => {
    getMock.mockResolvedValue({ data: runResponse })
    const params = {
      status: 'failed',
      trigger_type: 'webhook',
      search: 'deploy',
      pipeline_id: 'pipeline-1',
      page: 2,
      page_size: 25,
      cursor: 'cursor-2',
    }

    await expect(fetchRuns(params)).resolves.toEqual(runResponse)
    expect(getMock).toHaveBeenCalledWith('/api/v1/runs', {
      params: { query: params },
    })
  })

  it('omits empty and undefined list filters', async () => {
    getMock.mockResolvedValue({ data: runResponse })

    await fetchRuns({ status: '', trigger_type: '', search: '', pipeline_id: '', cursor: '', page: 0, page_size: 0 })

    expect(getMock).toHaveBeenCalledWith('/api/v1/runs', {
      params: { query: { page: 0, page_size: 0 } },
    })
  })

  it('throws a formatted error when listing runs fails', async () => {
    getMock.mockResolvedValue({ error: { detail: 'cannot list runs' } })

    await expect(fetchRuns()).rejects.toThrow('cannot list runs')
  })

  it('cancels a run successfully and formats API and thrown errors', async () => {
    postMock.mockResolvedValueOnce({})
    await expect(requestRunCancellation('run-1', 'Cancel failed:')).resolves.toEqual({})
    expect(postMock).toHaveBeenCalledWith('/api/v1/runs/{run_id}/cancel', {
      params: { path: { run_id: 'run-1' } },
    })

    postMock.mockResolvedValueOnce({ error: { detail: 'already complete' } })
    await expect(requestRunCancellation('run-1', 'Cancel failed:')).resolves.toEqual({
      error: 'Cancel failed: already complete',
    })

    postMock.mockRejectedValueOnce(new Error('connection lost'))
    await expect(requestRunCancellation('run-1', 'Cancel failed:')).resolves.toEqual({
      error: 'Cancel failed: connection lost',
    })
  })

  it('reruns a run with a returned id, missing id, API error, and thrown error', async () => {
    postMock.mockResolvedValueOnce({ data: { run_id: 'rerun-1' } })
    await expect(requestRunRerun('run-1', 'Rerun failed:')).resolves.toEqual({ runId: 'rerun-1' })

    postMock.mockResolvedValueOnce({ data: undefined })
    await expect(requestRunRerun('run-1', 'Rerun failed:')).resolves.toEqual({ runId: undefined })

    postMock.mockResolvedValueOnce({ error: { detail: 'not rerunnable' } })
    await expect(requestRunRerun('run-1', 'Rerun failed:')).resolves.toEqual({
      error: 'Rerun failed: not rerunnable',
    })

    postMock.mockRejectedValueOnce(new Error('connection lost'))
    await expect(requestRunRerun('run-1', 'Rerun failed:')).resolves.toEqual({
      error: 'Rerun failed: connection lost',
    })
  })
})
