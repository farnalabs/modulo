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

function draftPayload(attemptId = 'attempt-1', proposalId = PROPOSAL_ID) {
  return {
    proposal: { proposal_id: proposalId, attempt: { id: attemptId } },
    nodes: [{ id: 'build', node_type: 'agent', label: 'Build' }],
    edges: [{ source: 'build', target: 'build', edge_type: 'normal', hitl_review: false }],
  }
}

function comparisonPayload() {
  return {
    contract_version: 'alpha.determination-comparison.v1',
    base: { side: 'base', attempt_id: 'attempt-1', proposal_id: PROPOSAL_ID },
    target: { side: 'target', attempt_id: 'attempt-2', proposal_id: PROPOSAL_ID },
    nodes_added: [],
    nodes_removed: [],
    nodes_changed: [],
    edges_added: [],
    edges_removed: [],
    edges_changed: [],
    behaviour_changes: [],
  }
}

function rerunPayload() {
  return {
    contract_version: 'alpha.determination-rerun.v1',
    proposal: { proposal_id: PROPOSAL_ID, attempt: { id: 'attempt-2' } },
    prior_attempt: {
      attempt_id: 'attempt-1',
      proposal_id: PROPOSAL_ID,
      status: 'completed',
      confidence: 'high',
      uncertainty: [],
      provenance: [],
      unavailable_evidence: [],
      source_inventory: [],
    },
    current_attempt: {
      attempt_id: 'attempt-2',
      proposal_id: PROPOSAL_ID,
      status: 'completed',
      confidence: 'high',
      uncertainty: [],
      provenance: [],
      unavailable_evidence: [],
      source_inventory: [],
    },
    corrections: {},
    source_changes: {},
    input_set_unchanged: true,
    element_changes: [],
    incomplete: false,
    incomplete_reasons: [],
  }
}

function reviewCorrections() {
  return {
    correctedDerivedElements: [{ id: 'element-1', statement: 'Corrected statement' }],
    removedDerivedElementIds: ['element-2'],
  }
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

  it('holds the current draft as the baseline and compares it with a later draft', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      return Promise.resolve(ok(comparisonPayload()))
    })
    const api = mountComposable()

    await api.generateDraft()
    api.holdBaseline()
    expect(api.baseline.value).not.toBeNull()

    const result = await api.compareDrafts()

    expect(result).not.toBeNull()
    expect(api.comparison.value).not.toBeNull()
    const call = mockPost.mock.calls.find(([path]) => path === '/api/v1/determination/compare')
    expect(call?.[1]?.body).toEqual({
      base: {
        proposal: draftPayload().proposal,
        nodes: draftPayload().nodes,
        edges: draftPayload().edges,
      },
      target: {
        proposal: draftPayload().proposal,
        nodes: draftPayload().nodes,
        edges: draftPayload().edges,
      },
    })
  })

  it('refuses to compare without a held baseline', async () => {
    mockPost.mockImplementation((path: string) =>
      Promise.resolve(path === '/api/v1/determination/draft' ? ok(draftPayload()) : ok(comparisonPayload())),
    )
    const api = mountComposable()
    await api.generateDraft()

    expect(await api.compareDrafts()).toBeNull()
    expect(mockPost.mock.calls.some(([path]) => path === '/api/v1/determination/compare')).toBe(false)
  })

  it('clears the previous comparison when a compare fails', async () => {
    let compareCalls = 0
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      compareCalls += 1
      return Promise.resolve(compareCalls === 1 ? ok(comparisonPayload()) : failure('duplicate node ids'))
    })
    const api = mountComposable()

    await api.generateDraft()
    api.holdBaseline()

    expect(await api.compareDrafts()).not.toBeNull()
    expect(api.comparison.value).not.toBeNull()

    expect(await api.compareDrafts()).toBeNull()
    expect(api.comparison.value).toBeNull()
    expect(api.comparisonError.value).toBe('duplicate node ids')
  })

  it('keeps the held baseline but drops a stale comparison when a new draft is generated', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      return Promise.resolve(ok(comparisonPayload()))
    })
    const api = mountComposable()

    await api.generateDraft()
    api.holdBaseline()
    await api.compareDrafts()
    expect(api.comparison.value).not.toBeNull()

    await api.generateDraft()

    expect(api.baseline.value).not.toBeNull()
    expect(api.comparison.value).toBeNull()
  })

  it('clears the baseline and the comparison together', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      return Promise.resolve(ok(comparisonPayload()))
    })
    const api = mountComposable()

    await api.generateDraft()
    api.holdBaseline()
    await api.compareDrafts()

    api.clearBaseline()

    expect(api.baseline.value).toBeNull()
    expect(api.comparison.value).toBeNull()
    expect(api.comparisonError.value).toBeNull()
  })

  it('ignores a hold when no draft has been generated', () => {
    const api = mountComposable()

    api.holdBaseline()

    expect(api.baseline.value).toBeNull()
  })

  it('marks the surface busy while a comparison is in flight', async () => {
    mockPost.mockImplementation((path: string) =>
      path === '/api/v1/determination/draft' ? Promise.resolve(ok(draftPayload())) : Promise.resolve(ok(comparisonPayload())),
    )
    const api = mountComposable()
    await api.generateDraft()
    api.holdBaseline()

    const pending = deferred<unknown>()
    mockPost.mockReturnValue(pending.promise)

    const inFlight = api.compareDrafts()
    expect(api.busy.value).toBe(true)

    pending.resolve(ok(comparisonPayload()))
    await inFlight
    expect(api.busy.value).toBe(false)
  })

  it('compares a draft that omits nodes and edges as empty graphs', async () => {
    const sparse = { proposal: { proposal_id: PROPOSAL_ID, attempt: { id: 'attempt-1' } } }
    mockPost.mockImplementation((path: string) =>
      path === '/api/v1/determination/draft' ? Promise.resolve(ok(sparse)) : Promise.resolve(ok(comparisonPayload())),
    )
    const api = mountComposable()

    await api.generateDraft()
    api.holdBaseline()

    expect(await api.compareDrafts()).not.toBeNull()
    const call = mockPost.mock.calls.find(([path]) => path === '/api/v1/determination/compare')
    expect(call?.[1]?.body).toEqual({
      base: { proposal: sparse.proposal, nodes: [], edges: [] },
      target: { proposal: sparse.proposal, nodes: [], edges: [] },
    })
  })

  it('reruns with the proposal on screen and the review corrections', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      return Promise.resolve(ok(rerunPayload()))
    })
    const api = mountComposable()
    await api.generateDraft()

    const result = await api.rerunDetermination(reviewCorrections())

    expect(result).not.toBeNull()
    expect(api.rerun.value).not.toBeNull()
    const call = mockPost.mock.calls.find(([path]) => path === '/api/v1/determination/rerun')
    expect(call?.[1]?.body).toEqual({
      prior_proposal: draftPayload().proposal,
      corrected_derived_elements: reviewCorrections().correctedDerivedElements,
      removed_derived_element_ids: reviewCorrections().removedDerivedElementIds,
    })
  })

  it('refuses to rerun without a proposal', async () => {
    const api = mountComposable()

    expect(await api.rerunDetermination(reviewCorrections())).toBeNull()
    expect(mockPost.mock.calls.some(([path]) => path === '/api/v1/determination/rerun')).toBe(false)
  })

  it('clears the previous rerun result when a rerun fails', async () => {
    let rerunCalls = 0
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      rerunCalls += 1
      return Promise.resolve(rerunCalls === 1 ? ok(rerunPayload()) : failure('duplicate derived element ids'))
    })
    const api = mountComposable()
    await api.generateDraft()

    expect(await api.rerunDetermination(reviewCorrections())).not.toBeNull()
    expect(api.rerun.value).not.toBeNull()

    expect(await api.rerunDetermination(reviewCorrections())).toBeNull()
    expect(api.rerun.value).toBeNull()
    expect(api.rerunError.value).toBe('duplicate derived element ids')
  })

  it('ignores a second rerun while one is already in flight', async () => {
    mockPost.mockImplementation((path: string) =>
      path === '/api/v1/determination/draft' ? Promise.resolve(ok(draftPayload())) : Promise.resolve(ok(rerunPayload())),
    )
    const api = mountComposable()
    await api.generateDraft()

    const pending = deferred<unknown>()
    mockPost.mockReturnValue(pending.promise)

    const first = api.rerunDetermination(reviewCorrections())
    const second = api.rerunDetermination(reviewCorrections())
    expect(await second).toBeNull()

    pending.resolve(ok(rerunPayload()))
    expect(await first).not.toBeNull()
  })

  it('drops a stale rerun result when a new draft is generated', async () => {
    mockPost.mockImplementation((path: string) => {
      if (path === '/api/v1/determination/draft') return Promise.resolve(ok(draftPayload()))
      return Promise.resolve(ok(rerunPayload()))
    })
    const api = mountComposable()
    await api.generateDraft()
    await api.rerunDetermination(reviewCorrections())
    expect(api.rerun.value).not.toBeNull()

    await api.generateDraft()

    expect(api.rerun.value).toBeNull()
    expect(api.rerunError.value).toBeNull()
  })

  it('marks the surface busy while a rerun is in flight', async () => {
    mockPost.mockImplementation((path: string) =>
      path === '/api/v1/determination/draft' ? Promise.resolve(ok(draftPayload())) : Promise.resolve(ok(rerunPayload())),
    )
    const api = mountComposable()
    await api.generateDraft()

    const pending = deferred<unknown>()
    mockPost.mockReturnValue(pending.promise)

    const inFlight = api.rerunDetermination(reviewCorrections())
    expect(api.busy.value).toBe(true)

    pending.resolve(ok(rerunPayload()))
    await inFlight
    expect(api.busy.value).toBe(false)
  })
})
