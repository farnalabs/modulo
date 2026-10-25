<template>
  <DataTable
    :columns="columns"
    :rows="tableRows"
    :row-clickable="false"
    initial-sort-column="created_at"
    initial-sort-direction="desc"
    new-sort-direction="desc"
    row-testid="risk-assessment-row"
    data-testid="risk-assessment-table"
  >
    <template #cell-created_at="{ row }">
      <span class="text-xs">{{ formatDate(row.created_at as string) }}</span>
    </template>
    <template #cell-key="{ row }">
      <span class="font-mono text-xs">{{ row.key }}</span>
    </template>
    <template #cell-action="{ row }">
      <ActionBadge :action="String(row.action)" data-testid="risk-assessment-action-badge" />
    </template>
    <template #cell-subject="{ row }">
      <div>{{ row.subject_type }}</div>
      <div class="font-mono text-xs text-muted-foreground">{{ row.subject_id }}</div>
    </template>
    <template #cell-value="{ row }">
      <pre class="max-w-xl whitespace-pre-wrap break-words font-mono text-xs">{{ formatValue(row.value) }}</pre>
    </template>
    <template #empty>
      <EmptyState
        :title="t('views.RiskAssessmentView.empty_title')"
        :description="t('views.RiskAssessmentView.empty_description')"
        data-testid="risk-assessment-empty"
      />
    </template>
  </DataTable>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateShort } from '../../lib/formatDate'
import type { EvidenceRow } from '../../lib/riskAssessment'
import EmptyState from '../shared/EmptyState.vue'
import { DataTable, type Column, type DataTableRow } from '../ui/data-table'
import ActionBadge from './ActionBadge.vue'

const props = defineProps<{ rows: EvidenceRow[] }>()
const { t } = useI18n()

const columns: Column[] = [
  { key: 'created_at', label: t('views.RiskAssessmentView.col_timestamp'), sortable: true, testid: 'risk-assessment-sort-timestamp' },
  { key: 'key', label: t('views.RiskAssessmentView.col_key'), sortable: true, testid: 'risk-assessment-sort-key' },
  { key: 'action', label: t('views.RiskAssessmentView.col_action'), sortable: true, testid: 'risk-assessment-sort-action' },
  { key: 'subject', label: t('views.RiskAssessmentView.col_subject'), sortable: true, testid: 'risk-assessment-sort-subject' },
  { key: 'value', label: t('views.RiskAssessmentView.col_value') },
]

const tableRows = computed<DataTableRow[]>(() => props.rows.map((row) => ({
  ...row,
  action: row.action ?? 'unknown',
  subject: row.subject_id,
})))

function formatDate(dateStr: string): string {
  return formatDateShort(dateStr)
}

function formatValue(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}
</script>
