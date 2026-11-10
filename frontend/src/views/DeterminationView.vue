<template>
  <div
    class="page-wide space-y-6"
    :data-loading="busy ? 'true' : 'false'"
    data-testid="determination-view"
  >
    <PageHeader
      :title="$t('views.DeterminationView.title')"
      :subtitle="$t('views.DeterminationView.subtitle')"
    >
      <template #right>
        <button
          type="button"
          class="rounded-md border px-3 py-2 text-sm font-medium disabled:opacity-60"
          :disabled="scanLoading"
          data-testid="determination-run-scan"
          @click="runScan()"
        >
          {{ scanLoading ? $t('views.DeterminationView.running_scan') : $t('views.DeterminationView.run_scan') }}
        </button>
        <button
          type="button"
          class="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60"
          :disabled="draftLoading"
          data-testid="determination-generate-draft"
          @click="generateDraft()"
        >
          {{ draftLoading ? $t('views.DeterminationView.generating_draft') : $t('views.DeterminationView.generate_draft') }}
        </button>
      </template>
    </PageHeader>

    <DeterminationScanPanel
      :scan="scan"
      :loading="scanLoading"
      :error="scanError"
      @retry="runScan()"
    />

    <DeterminationProposalPanel
      :proposal="proposal"
      :review="review"
      :loading="draftLoading"
      :error="draftError"
      @retry="generateDraft()"
    />

    <DeterminationComparisonPanel
      :baseline="baseline"
      :target="draft"
      :comparison="comparison"
      :loading="comparisonLoading"
      :error="comparisonError"
      @hold="holdBaseline()"
      @clear="clearBaseline()"
      @compare="compareDrafts()"
    />

    <DeterminationReviewPanel
      v-if="proposal"
      :proposal="proposal"
      :review="review"
      :loading="reviewLoading"
      :error="reviewError"
      @submit="onReviewSubmit"
      @corrections-change="onCorrectionsChange"
    />
    <p v-else class="text-sm text-muted-foreground" data-testid="determination-review-unavailable">
      {{ $t('views.DeterminationView.review_needs_proposal') }}
    </p>

    <DeterminationRerunPanel
      :proposal="proposal"
      :corrections="reviewCorrections"
      :result="rerun"
      :loading="rerunLoading"
      :error="rerunError"
      @rerun="onRerun"
    />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import PageHeader from '../components/shared/PageHeader.vue'
import DeterminationScanPanel from '../components/determination/DeterminationScanPanel.vue'
import DeterminationProposalPanel from '../components/determination/DeterminationProposalPanel.vue'
import DeterminationComparisonPanel from '../components/determination/DeterminationComparisonPanel.vue'
import DeterminationReviewPanel from '../components/determination/DeterminationReviewPanel.vue'
import DeterminationRerunPanel from '../components/determination/DeterminationRerunPanel.vue'
import {
  useDetermination,
  type ReviewCorrections,
  type ReviewSubmission,
} from '../composables/useDetermination'

const {
  scan,
  scanLoading,
  scanError,
  proposal,
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
  busy,
  runScan,
  generateDraft,
  submitReview,
  rerunDetermination,
  holdBaseline,
  clearBaseline,
  compareDrafts,
} = useDetermination()

/**
 * The review panel's current corrections, kept here so the rerun action can
 * carry exactly what the reviewer changed forward by prior element id.
 */
const reviewCorrections = ref<ReviewCorrections>({
  correctedDerivedElements: [],
  removedDerivedElementIds: [],
})

function onCorrectionsChange(corrections: ReviewCorrections): void {
  reviewCorrections.value = corrections
}

function onRerun(): void {
  void rerunDetermination(reviewCorrections.value)
}

function onReviewSubmit(submission: ReviewSubmission): void {
  void submitReview(submission)
}
</script>
