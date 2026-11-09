import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'

const mockGet = vi.hoisted(() => vi.fn())
const mockPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api/client', () => ({
  api: { GET: mockGet, POST: mockPost },
}))

import {
  useDetermination,
  type ReviewSubmission,
  type UseDeterminationResult,
} from '../../composables/useDetermination'

const PROPOSAL_ID = '11111111-1111-4111-8111-111111111111'

let state: UseDeterminationResult
const Harness = defineComponent({
  setup() {
    state = useDetermination()
    return () => null
  },
})

function mountComposable(): UseDeterminationResult {
  mount(Harness)
  return state
}

function ok(data: unknown) {
  return { data, error: undefined, response: { ok: true, status: 200 } }
}

function failure(detail: string) {
  return {
    data: undefined,
    error: { type: 'urn:problem:modulo:validation_error', title: 'Validation Error', status: 422, detail },
    response: { ok: false, status: 422 },
  }
}

function noDataNoError() {
  return { data: undefined, error: undefined, response: { ok: true, status: 200 } }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function submission(): ReviewSubmission {
  return {
    decision: 'needs_review',
    reason: null,
    correctedDerivedElements: [],
    removedDerivedElementIds: [],
    acknowledgedAssumptionIds: [],
  }
}

function draftPayload() {
  return { proposal: { proposal_id: PROPOSAL_ID } }
}

function reviewPayload() {
  return {
    decision: 'needs_review',
    review_state: 'needs_review',
    activation_status: 'not_activated',
    actor_username: 'dana',
    decided_at: '2026-01-02T03:04:05Z',
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useDetermination', () => {
  it('scans, drafts and records a review', async () => {
    mockGet.mockResolvedValue(ok({ samples: [], findings: [], summary: 'ok' }))
    mockPost.mockImplementation((path: string) =>
      Promise.resolve(path === '/api/v1/determination/draft' ? ok(draftPayload()) : ok(reviewPayload())),
    )

    const api = mountComposable()

    await api.runScan()
    expect(mockGet).toHaveBeenCalledWith('/api/v1/determination')
    expect(api.scan.value).toEqual({ samples: [], findings: [], summary: 'ok' })

    await api.generateDraft()
    expect(api.draft.value).not.toBeNull()

    const result = await api.submitReview(submission())
    expect(result).not.toBeNull()
    expect(api.review.value).not.toBeNull()
  })

  it('ignores a second scan while one is already in flight', async () => {
    const pending = deferred<unknown>()
    mockGet.mockReturnValue(pending.promise)

    const api = mountComposable()
    const first = api.runScan()
    const second = api.runScan()

    expect(mockGet).toHaveBeenCalledTimes(1)
    pending.resolve(ok({ samples: [], findings: [], summary: 'ok' }))
    await Promise.all([first, second])
  })

  it('ignores a second draft request while one is already in flight', async () => {
    const pending = deferred<unknown>()
    mockPost.mockReturnValue(pending.promise)

    const api = mountComposable()
    const first = api.generateDraft()
    const second = api.generateDraft()

    expect(mockPost).toHaveBeenCalledTimes(1)
    pending.resolve(ok(draftPayload()))
    await Promise.all([first, second])
  })

  it('surfaces the problem detail when a scan is refused', async () => {
    mockGet.mockResolvedValue(failure('scan exploded'))

    const api = mountComposable()
    await api.runScan()

    expect(api.scan.value).toBeNull()
    expect(api.scanError.value).toBe('scan exploded')
  })

  it('falls back to the translated message when a scan returns no data', async () => {
    mockGet.mockResolvedValue(noDataNoError())

    const api = mountComposable()
    await api.runScan()

    expect(api.scan.value).toBeNull()
    expect(api.scanError.value).toBeTruthy()
  })

  it('falls back to the translated message when the detail is unhelpful', async () => {
    mockGet.mockResolvedValue(failure('Unknown error'))

    const api = mountComposable()
    await api.runScan()

    expect(api.scanError.value).not.toBe('Unknown error')
    expect(api.scanError.value).toBeTruthy()
  })

  it('falls back to the translated message when the detail is blank', async () => {
    mockGet.mockResolvedValue(failure(''))

    const api = mountComposable()
    await api.runScan()

    expect(api.scanError.value).toBeTruthy()
  })

  it('surfaces the problem detail when a draft is refused', async () => {
    mockPost.mockResolvedValue(failure('draft exploded'))

    const api = mountComposable()
    await api.generateDraft()

    expect(api.draft.value).toBeNull()
    expect(api.draftError.value).toBe('draft exploded')
  })

  it('refuses to submit a review without a proposal', async () => {
    const api = mountComposable()

    const result = await api.submitReview(submission())

    expect(result).toBeNull()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('ignores a second review submission while one is in flight', async () => {
    mockPost.mockImplementation((path: string) =>
      Promise.resolve(path === '/api/v1/determination/draft' ? ok(draftPayload()) : ok(reviewPayload())),
    )
    const api = mountComposable()
    await api.generateDraft()

    const pending = deferred<unknown>()
    mockPost.mockReturnValue(pending.promise)

    const first = api.submitReview(submission())
    const second = api.submitReview(submission())
    expect(await second).toBeNull()

    pending.resolve(ok(reviewPayload()))
    expect(await first).not.toBeNull()
  })

  it('surfaces the problem detail when a review is refused', async () => {
    mockPost.mockImplementation((path: string) =>
      Promise.resolve(path === '/api/v1/determination/draft' ? ok(draftPayload()) : failure('review exploded')),
    )
    const api = mountComposable()
    await api.generateDraft()

    const result = await api.submitReview(submission())

    expect(result).toBeNull()
    expect(api.reviewError.value).toBe('review exploded')
  })
})
