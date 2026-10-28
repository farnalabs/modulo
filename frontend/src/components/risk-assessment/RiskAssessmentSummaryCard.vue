<template>
  <SummaryCard
    :title="$t('views.RiskAssessmentView.summary_title')"
    :items="summaryItems"
    :footnote="timeWindow
      ? $t('views.RiskAssessmentView.time_window_note', { days: timeWindow })
      : undefined"
    data-testid="risk-assessment-summary-card"
    grid-test-id="risk-assessment-summary-grid"
    footnote-test-id="risk-assessment-time-window"
  >
    <template #headerActions>
      <span
        v-if="facetDegraded"
        class="rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning-text"
        data-testid="risk-assessment-facet-degraded"
      >
        {{ $t('views.RiskAssessmentView.facet_degraded_label') }}
      </span>
    </template>
  </SummaryCard>
</template>

<script setup lang="ts">
import SummaryCard from '../ui/SummaryCard.vue'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

interface Props {
  totalRows: number
  actionCounts: Record<string, number>
  facetDegraded: boolean
  timeWindow: number
  dataTestId?: string
}

const props = defineProps<Props>()

const { t } = useI18n()

const summaryItems = computed(() => [
  {
    label: t('views.RiskAssessmentView.total_rows_label'),
    value: props.totalRows,
    color: 'foreground' as const,
    testId: 'risk-assessment-total',
  },
  {
    label: t('views.RiskAssessmentView.block_label'),
    value: props.actionCounts?.block ?? 0,
    color: 'destructive' as const,
    testId: 'risk-assessment-block-count',
  },
  {
    label: t('views.RiskAssessmentView.warn_label'),
    value: props.actionCounts?.warn ?? 0,
    color: 'warning' as const,
    testId: 'risk-assessment-warn-count',
  },
  {
    label: t('views.RiskAssessmentView.continue_label'),
    value: props.actionCounts?.continue ?? 0,
    color: 'success' as const,
    testId: 'risk-assessment-continue-count',
  },
  {
    label: t('views.RiskAssessmentView.escalate_label'),
    value: props.actionCounts?.escalate ?? 0,
    color: 'muted' as const,
    testId: 'risk-assessment-escalate-count',
  },
])
</script>
