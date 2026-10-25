<template>
  <div class="card p-6" data-testid="risk-assessment-summary-card">
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-lg font-semibold">{{ $t('views.RiskAssessmentView.summary_title') }}</h2>
      <span v-if="facetDegraded" class="rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning-text" data-testid="risk-assessment-facet-degraded">
        {{ $t('views.RiskAssessmentView.facet_degraded_label') }}
      </span>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4" data-testid="risk-assessment-summary-grid">
      <div class="rounded-lg border bg-card p-4">
        <div class="text-3xl font-bold text-foreground" data-testid="risk-assessment-total">{{ totalRows }}</div>
        <div class="text-sm text-muted-foreground">{{ $t('views.RiskAssessmentView.total_rows_label') }}</div>
      </div>

      <div class="rounded-lg border bg-card p-4">
        <div class="text-3xl font-bold text-destructive-text" data-testid="risk-assessment-block-count">{{ actionCounts?.block ?? 0 }}</div>
        <div class="text-sm text-muted-foreground">{{ $t('views.RiskAssessmentView.block_label') }}</div>
      </div>

      <div class="rounded-lg border bg-card p-4">
        <div class="text-3xl font-bold text-warning-text" data-testid="risk-assessment-warn-count">{{ actionCounts?.warn ?? 0 }}</div>
        <div class="text-sm text-muted-foreground">{{ $t('views.RiskAssessmentView.warn_label') }}</div>
      </div>

      <div class="rounded-lg border bg-card p-4">
        <div class="text-3xl font-bold text-success-text" data-testid="risk-assessment-continue-count">{{ actionCounts?.continue ?? 0 }}</div>
        <div class="text-sm text-muted-foreground">{{ $t('views.RiskAssessmentView.continue_label') }}</div>
      </div>

      <div class="rounded-lg border bg-card p-4">
        <div class="text-3xl font-bold text-muted-foreground" data-testid="risk-assessment-escalate-count">{{ actionCounts?.escalate ?? 0 }}</div>
        <div class="text-sm text-muted-foreground">{{ $t('views.RiskAssessmentView.escalate_label') }}</div>
      </div>
    </div>

    <p v-if="timeWindow" class="mt-4 text-sm text-muted-foreground" data-testid="risk-assessment-time-window">
      {{ $t('views.RiskAssessmentView.time_window_note', { days: timeWindow }) }}
    </p>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'

interface Props {
  totalRows: number
  actionCounts: Record<string, number>
  facetDegraded: boolean
  timeWindow: number
}

const props = defineProps<Props>()

useI18n()
</script>
