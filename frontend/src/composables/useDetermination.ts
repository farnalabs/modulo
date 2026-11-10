import { computed, ref, type ComputedRef, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import type { components } from '../lib/api/schema'

/**
 * Determination surface state (FAR-2471, FAR-2473, FAR-2474).
 *
 * Five explicitly user-triggered operations:
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
 * 5. ``rerunDetermination`` writes ``POST /api/v1/determination/rerun`` — a
 *    fresh attempt against the proposal on screen, with the review panel's
 *    corrections carried forward by prior element id. It creates a new attempt
 *    and reports what changed; it persists, activates, publishes and mutates
 *    nothing, including the prior attempt it was given.
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
export type DeterminationRerunResult = components['schemas']['RerunResult']
export type RerunAttemptSummary = components['schemas']['RerunAttemptSummary']
export type ElementChange = components['schemas']['ElementChange']
export type ElementChangeStatus = ElementChange['status']
export type SourceInventoryChanges = components['schemas']['SourceInventoryChanges']
export type CarriedForwardCorrections = components['schemas']['CarriedForwardCorrections']
type DeterminationComparisonSide = components['schemas']['ProposalComparisonSide']

/**
 * The reviewer's corrections, as the review panel holds them and as a rerun
 * carries them forward by prior element id.
 */
export interface ReviewCorrections {
  correctedDerivedElements: DerivedElementEdit[]
  removedDerivedElementIds: string[]
}

/** The reviewer's edits, ready to be merged into a review request. */
export interface ReviewSubmission extends ReviewCorrections {
  decision: ProposalReviewDecision
  reason: string | null
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
  rerun: Ref<DeterminationRerunResult | null>
  rerunLoading: Ref<boolean>
  rerunError: Ref<string | null>
  proposal: ComputedRef<DeterminationProposal | null>
  busy: ComputedRef<boolean>
  runScan: () => Promise<void>
  generateDraft: () => Promise<void>
  submitReview: (submission: ReviewSubmission) => Promise<DeterminationReviewResponse | null>
  /**
   * Rerun determination against the proposal on screen, carrying the given
   * review corrections forward. Nothing is persisted, activated or published.
   */
  rerunDetermination: (corrections: ReviewCorrections) => Promise<DeterminationRerunResult | null>
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

  /**
   * The rerun comparison. Held in memory only, like the draft and the
   * baseline: a rerun creates a new attempt server-side but never persists it
   * as the proposal on screen - it changes nothing.
   */
  const rerun = ref<DeterminationRerunResult | null>(null)
  const rerunLoading = ref(false)
  const rerunError = ref<string | null>(null)

  const proposal = computed<DeterminationProposal | null>(() => draft.value?.proposal ?? null)
  const busy = computed(
    () =>
      scanLoading.value ||
      draftLoading.value ||
      reviewLoading.value ||
      comparisonLoading.value ||
      rerunLoading.value,
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
      // The rerun compared the previous proposal with its own fresh attempt;
      // the proposal on screen has changed, so that comparison no longer
      // describes what a rerun would start from.
      rerun.value = null
      rerunError.value = null
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

  async function rerunDetermination(
    corrections: ReviewCorrections,
  ): Promise<DeterminationRerunResult | null> {
    const current = proposal.value
    if (!current || rerunLoading.value) return null
    rerunLoading.value = true
    rerunError.value = null
    // Never leave a previous rerun result on screen while a new one is in
    // flight: a failed rerun must not read as the outcome of this attempt.
    rerun.value = null
    try {
      const body: components['schemas']['ProposalRerunRequest'] = {
        prior_proposal: current,
        corrected_derived_elements: corrections.correctedDerivedElements,
        removed_derived_element_ids: corrections.removedDerivedElementIds,
      }
      const response = await api.POST('/api/v1/determination/rerun', { body })
      if (response.error || !response.data) {
        // A refused rerun arrives as a 422 ProblemDetail; surface its detail
        // verbatim (the translation only supplies the context).
        rerunError.value = errorText(response.error, 'views.DeterminationView.rerun_failed')
        return null
      }
      rerun.value = response.data
      return response.data
    } finally {
      rerunLoading.value = false
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
    rerun,
    rerunLoading,
    rerunError,
    proposal,
    busy,
    runScan,
    generateDraft,
    submitReview,
    rerunDetermination,
    holdBaseline,
    clearBaseline,
    compareDrafts,
  }
}
