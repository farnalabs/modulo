import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchVariantBatch,
  fetchVariantBatches,
  softDeleteVariantBatch,
  reFireVariantBatch,
  type VariantBatchDetail,
  type VariantBatchListResponse,
} from '../lib/api/variantBatches'

const { apiGet, apiPost, apiDelete } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiDelete: vi.fn(),
}))

vi.mock('../composables/useApi', () => ({
  useApi: () => ({
    get: apiGet,
    post: apiPost,
    put: vi.fn(),
    patch: vi.fn(),
    delete: apiDelete,
  }),
}))

const batchDetail: VariantBatchDetail = {
  batch_id: 'b-1',
  name: 'Batch One',
  pipeline_id: 'p-1',
  pipeline_name: 'Pipeline A',
  status: 'complete',
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T01:00:00Z',
  runs: [
    {
      run_id: 'r-1',
      variant_name: 'baseline',
      snapshot_label: 'snap-1',
      input_label: 'frozen input',
      run_status: 'complete',
      pass_rate: 1,
      total_cost_usd: 0.5,
      total_tokens: 1200,
      eval_results: [
        { eval_id: 'e-1', node_id: 'n-1', passed: true, score: 0.9, detail: null },
      ],
      node_outputs: null,
    },
    {
      run_id: 'r-2',
      variant_name: 'aggressive',
      snapshot_label: 'snap-1',
      input_label: 'temperature=1.0',
      run_status: 'failed',
      pass_rate: 0.25,
      total_cost_usd: 1.25,
      total_tokens: 3400,
      eval_results: [],
      node_outputs: { step: { ok: false } },
    },
  ],
}

const batchList: VariantBatchListResponse = {
  items: [
    {
      batch_id: 'b-1',
      name: 'Batch One',
      pipeline_name: 'Pipeline A',
      status: 'complete',
      run_count: 2,
      created_at: '2026-09-01T00:00:00Z',
    },
  ],
  total: 1,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('fetchVariantBatch', () => {
  it('returns the batch detail on success', async () => {
    apiGet.mockResolvedValue(batchDetail)
    const result = await fetchVariantBatch('b-1')
    expect(apiGet).toHaveBeenCalledWith('/api/v1/variant-batches/b-1')
    expect(result.error).toBeUndefined()
    expect(result.data).toEqual(batchDetail)
  })

  it('surfaces a formatted error string instead of throwing', async () => {
    apiGet.mockRejectedValue(new Error('request failed: 500'))
    const result = await fetchVariantBatch('b-1')
    expect(result.data).toBeUndefined()
    expect(result.error).toBe('request failed: 500')
  })
})

describe('fetchVariantBatches', () => {
  it('returns the batch list on success', async () => {
    apiGet.mockResolvedValue(batchList)
    const result = await fetchVariantBatches()
    expect(apiGet).toHaveBeenCalledWith('/api/v1/variant-batches')
    expect(result.data).toEqual(batchList)
    expect(result.data?.items).toHaveLength(1)
  })

  it('surfaces a formatted error string instead of throwing', async () => {
    apiGet.mockRejectedValue(new Error('network down'))
    const result = await fetchVariantBatches()
    expect(result.error).toBe('network down')
  })
})

describe('softDeleteVariantBatch', () => {
  it('DELETEs the batch and resolves without an error', async () => {
    apiDelete.mockResolvedValue(undefined)
    const result = await softDeleteVariantBatch('b-1')
    expect(apiDelete).toHaveBeenCalledWith('/api/v1/variant-batches/b-1')
    expect(result.error).toBeUndefined()
  })

  it('surfaces a formatted error string instead of throwing', async () => {
    apiDelete.mockRejectedValue(new Error('forbidden'))
    const result = await softDeleteVariantBatch('b-1')
    expect(result.error).toBe('forbidden')
  })
})

describe('reFireVariantBatch', () => {
  it('POSTs to the re-fire endpoint and returns the new batch detail', async () => {
    const refired: VariantBatchDetail = { ...batchDetail, batch_id: 'b-2', status: 'pending', runs: [] }
    apiPost.mockResolvedValue(refired)
    const result = await reFireVariantBatch('b-1')
    expect(apiPost).toHaveBeenCalledWith('/api/v1/variant-batches/b-1/re-fire')
    expect(result.data).toEqual(refired)
    expect(result.data?.batch_id).toBe('b-2')
  })

  it('surfaces a formatted error string instead of throwing', async () => {
    apiPost.mockRejectedValue(new Error('batch is already running'))
    const result = await reFireVariantBatch('b-1')
    expect(result.error).toBe('batch is already running')
  })
})
