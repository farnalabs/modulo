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

    <DeterminationReviewPanel
      v-if="proposal"
      :proposal="proposal"
      :review="review"
      :loading="reviewLoading"
      :error="reviewError"
      @submit="onReviewSubmit"
    />
    <p v-else class="text-sm text-muted-foreground" data-testid="determination-review-unavailable">
      {{ $t('views.DeterminationView.review_needs_proposal') }}
    </p>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import DeterminationScanPanel from '../components/determination/DeterminationScanPanel.vue'
import DeterminationProposalPanel from '../components/determination/DeterminationProposalPanel.vue'
import DeterminationReviewPanel from '../components/determination/DeterminationReviewPanel.vue'
import { useDetermination, type ReviewSubmission } from '../composables/useDetermination'

const {
  scan,
  scanLoading,
  scanError,
  proposal,
  draftLoading,
  draftError,
  review,
  reviewLoading,
  reviewError,
  busy,
  runScan,
  generateDraft,
  submitReview,
} = useDetermination()

function onReviewSubmit(submission: ReviewSubmission): void {
  void submitReview(submission)
}
</script>
