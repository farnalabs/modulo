import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/schema'

/**
 * Determination surface state (FAR-2471).
 *
 * Three sequential, explicitly user-triggered operations:
 *
 * 1. ``runScan`` reads ``GET /api/v1/determination`` — the sampled connector
 *    sources and the inferred SDLC findings.
 * 2. ``generateDraft`` reads ``POST /api/v1/determination/draft`` — the same
 *    scan plus the versioned ``ProposalContract`` the reviewer edits.
 * 3. ``submitReview`` writes ``POST /api/v1/determination/review`` — the
 *    reviewer's decision. The response records ``activation_status`` as
 *    ``not_activated``: reviewing a proposal never creates a pipeline, enables a
 *    trigger or writes to a connected tool.
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
  proposal: ComputedRef<DeterminationProposal | null>
  busy: ComputedRef<boolean>
  runScan: () => Promise<void>
  generateDraft: () => Promise<void>
  submitReview: (submission: ReviewSubmission) => Promise<DeterminationReviewResponse | null>
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

  const proposal = computed<DeterminationProposal | null>(() => draft.value?.proposal ?? null)
  const busy = computed(() => scanLoading.value || draftLoading.value || reviewLoading.value)

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
    proposal,
    busy,
    runScan,
    generateDraft,
    submitReview,
  }
}
