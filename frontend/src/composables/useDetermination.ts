import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/schema'

/**
 * Determination surface state (FAR-2471, FAR-2473).
 *
 * Four explicitly user-triggered operations:
 *
 * 1. ``runScan`` reads ``GET /api/v1/determination`` — the sampled connector
 *    sources and the inferred SDLC findings.
 * 2. ``generateDraft`` reads ``POST /api/v1/determination/draft`` — the same
 *    scan plus the versioned ``ProposalContract`` the reviewer edits.
 * 3. ``submitReview`` writes ``POST /api/v1/determination/review`` — the
 *    reviewer's decision. The response records ``activation_status`` as
 *    ``not_activated``: reviewing a proposal never creates a pipeline, enables a
 *    trigger or writes to a connected tool.
 * 4. ``compareDrafts`` writes ``POST /api/v1/determination/compare`` — a pure,
 *    deterministic difference between a held baseline draft and the current
 *    draft. It persists nothing and activates nothing.
 *
 * Every type here is read from the generated OpenAPI schema — no wire shape is
 * declared by hand — so a backend contract change surfaces as a type error
 * rather than a silently ignored field.
 */

export type DeterminationScanResponse = components['schemas']['DeterminationResponse']
export type DeterminationDraftResponse = components['schemas']['DraftResponse']
export type DeterminationProposal = components['schemas']['ProposalContract']
export type DeterminationReviewResponse = components['schemas']['ProposalReviewResponse']
export type DeterminationSample = components['schemas']['SampleResponse']
export type DeterminationFinding = components['schemas']['FindingResponse']
export type DeterminationSourceInventoryItem = components['schemas']['SourceInventoryItem']
export type ProposalReviewDecision = components['schemas']['ProposalReviewRequest']['decision']
export type DerivedElementEdit = components['schemas']['DerivedElementEdit']
export type DerivedElement = components['schemas']['ProposalFact']
export type UnresolvedAssumption = components['schemas']['UnresolvedAssumption']
export type EvidenceReference = components['schemas']['EvidenceReference']
export type ProposalConfidence = components['schemas']['ProposalFact']['confidence']
export type ProposalReviewState = components['schemas']['ProposalContract']['review_state']
export type DeterminationComparison = components['schemas']['ProposalComparison']
type DeterminationComparisonSide = components['schemas']['ProposalComparisonSide']

/** The reviewer's edits, ready to be merged into a review request. */
export interface ReviewSubmission {
  decision: ProposalReviewDecision
  reason: string | null
  correctedDerivedElements: DerivedElementEdit[]
  removedDerivedElementIds: string[]
  acknowledgedAssumptionIds: string[]
}

export interface UseDeterminationResult {
  scan: Ref<DeterminationScanResponse | null>
  scanLoading: Ref<boolean>
  scanError: Ref<string | null>
  draft: Ref<DeterminationDraftResponse | null>
  draftLoading: Ref<boolean>
  draftError: Ref<string | null>
  review: Ref<DeterminationReviewResponse | null>
  reviewLoading: Ref<boolean>
  reviewError: Ref<string | null>
  baseline: Ref<DeterminationDraftResponse | null>
  comparison: Ref<DeterminationComparison | null>
  comparisonLoading: Ref<boolean>
  comparisonError: Ref<string | null>
  proposal: ComputedRef<DeterminationProposal | null>
  busy: ComputedRef<boolean>
  runScan: () => Promise<void>
  generateDraft: () => Promise<void>
  submitReview: (submission: ReviewSubmission) => Promise<DeterminationReviewResponse | null>
  /** Hold the current draft as the comparison baseline (in memory only). */
  holdBaseline: () => void
  /** Drop the held baseline and any comparison rendered against it. */
  clearBaseline: () => void
  compareDrafts: () => Promise<DeterminationComparison | null>
}

export function useDetermination(): UseDeterminationResult {
  const { t } = useI18n()

  const scan = ref<DeterminationScanResponse | null>(null)
  const scanLoading = ref(false)
  const scanError = ref<string | null>(null)

  const draft = ref<DeterminationDraftResponse | null>(null)
  const draftLoading = ref(false)
  const draftError = ref<string | null>(null)

  const review = ref<DeterminationReviewResponse | null>(null)
  const reviewLoading = ref(false)
  const reviewError = ref<string | null>(null)

  /**
   * The draft held as the comparison baseline. Held in memory only - it is a
   * snapshot of a generated draft, not a persisted record, and holding it
   * writes nothing.
   */
  const baseline = ref<DeterminationDraftResponse | null>(null)
  const comparison = ref<DeterminationComparison | null>(null)
  const comparisonLoading = ref(false)
  const comparisonError = ref<string | null>(null)

  const proposal = computed<DeterminationProposal | null>(() => draft.value?.proposal ?? null)
  const busy = computed(
    () => scanLoading.value || draftLoading.value || reviewLoading.value || comparisonLoading.value,
  )

  async function runScan(): Promise<void> {
    if (scanLoading.value) return
    scanLoading.value = true
    scanError.value = null
    try {
      const response = await api.GET('/api/v1/determination')
      if (response.error || !response.data) {
        scanError.value = errorText(response.error, 'views.DeterminationView.scan_failed')
        return
      }
      scan.value = response.data
    } finally {
      scanLoading.value = false
    }
  }

  async function generateDraft(): Promise<void> {
    if (draftLoading.value) return
    draftLoading.value = true
    draftError.value = null
    try {
      const response = await api.POST('/api/v1/determination/draft')
      if (response.error || !response.data) {
        draftError.value = errorText(response.error, 'views.DeterminationView.proposal_failed')
        return
      }
      draft.value = response.data
      // A new draft carries a new proposal id, so a previously recorded
      // decision no longer describes what is on screen. Clear it rather than
      // showing a result against a proposal the reviewer never decided.
      review.value = null
      reviewError.value = null
      // The draft is also the comparison target, so a rendered comparison no
      // longer describes the pair on screen. The held baseline is kept: the
      // reviewer's whole point of holding it is to compare later drafts.
      comparison.value = null
      comparisonError.value = null
    } finally {
      draftLoading.value = false
    }
  }

  async function submitReview(submission: ReviewSubmission): Promise<DeterminationReviewResponse | null> {
    const current = proposal.value
    if (!current || reviewLoading.value) return null
    reviewLoading.value = true
    reviewError.value = null
    try {
      const body: components['schemas']['ProposalReviewRequest'] = {
        proposal_id: current.proposal_id,
        proposal: current,
        decision: submission.decision,
        reason: submission.reason,
        corrected_derived_elements: submission.correctedDerivedElements,
        removed_derived_element_ids: submission.removedDerivedElementIds,
        acknowledged_assumption_ids: submission.acknowledgedAssumptionIds,
      }
      const response = await api.POST('/api/v1/determination/review', { body })
      if (response.error || !response.data) {
        // A refused decision arrives as a 422 ProblemDetail whose ``detail``
        // names the exact reason (e.g. the blocking assumption ids). Surface it
        // verbatim so the reviewer can act on it; the translation carries the
        // context, never a substitute for the server's own explanation.
        reviewError.value = errorText(response.error, 'views.DeterminationView.review_failed')
        return null
      }
      review.value = response.data
      return response.data
    } finally {
      reviewLoading.value = false
    }
  }

  /** The API's own detail when it sent one, else the translated fallback. */
  function errorText(error: unknown, fallbackKey: string): string {
    if (error) {
      const detail = formatApiError(error)
      if (detail && detail !== 'Unknown error') return detail
    }
    return t(fallbackKey)
  }

  /** One comparison side: the draft's proposal plus the graph it was drafted with. */
  function comparisonSide(draftResponse: DeterminationDraftResponse): DeterminationComparisonSide {
    return {
      proposal: draftResponse.proposal,
      nodes: draftResponse.nodes ?? [],
      edges: draftResponse.edges ?? [],
    }
  }

  function holdBaseline(): void {
    const current = draft.value
    if (!current) return
    baseline.value = current
    // A comparison rendered against the previous baseline (or target) no
    // longer describes the pair the reviewer is about to compare.
    comparison.value = null
    comparisonError.value = null
  }

  function clearBaseline(): void {
    baseline.value = null
    comparison.value = null
    comparisonError.value = null
  }

  async function compareDrafts(): Promise<DeterminationComparison | null> {
    const base = baseline.value
    const target = draft.value
    if (!base || !target || comparisonLoading.value) return null
    comparisonLoading.value = true
    comparisonError.value = null
    // Never leave a previous result on screen while a new comparison is in
    // flight: a failed compare must not read as the outcome of this pair.
    comparison.value = null
    try {
      const body: components['schemas']['ProposalComparisonRequest'] = {
        base: comparisonSide(base),
        target: comparisonSide(target),
      }
      const response = await api.POST('/api/v1/determination/compare', { body })
      if (response.error || !response.data) {
        // A refused comparison arrives as a 422 ProblemDetail; surface its
        // detail verbatim (the translation only supplies the context).
        comparisonError.value = errorText(response.error, 'views.DeterminationView.comparison_failed')
        return null
      }
      comparison.value = response.data
      return response.data
    } finally {
      comparisonLoading.value = false
    }
  }

  return {
    scan,
    scanLoading,
    scanError,
    draft,
    draftLoading,
    draftError,
    review,
    reviewLoading,
    reviewError,
    baseline,
    comparison,
    comparisonLoading,
    comparisonError,
    proposal,
    busy,
    runScan,
    generateDraft,
    submitReview,
    holdBaseline,
    clearBaseline,
    compareDrafts,
  }
}
