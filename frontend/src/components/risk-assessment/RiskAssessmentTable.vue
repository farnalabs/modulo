<template>
  <div class="overflow-x-auto rounded-lg border bg-card shadow-sm" data-testid="risk-assessment-table">
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="border-b bg-muted/50">
          <th
            v-for="column in sortableColumns"
            :key="column.key"
            scope="col"
            :aria-sort="ariaSort(column.key)"
            class="px-3 py-2 font-medium"
          >
            <button
              type="button"
              class="inline-flex items-center gap-1 hover:underline"
              :data-testid="`risk-assessment-sort-${column.testid}`"
              @click="sortBy(column.key)"
            >
              {{ $t(column.labelKey) }}
              <SortIcon :direction="sortDirection(column.key)" />
            </button>
          </th>
          <th scope="col" class="px-3 py-2 font-medium">
            {{ $t('views.RiskAssessmentView.col_value') }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in sortedRows" :key="row.id" class="border-b last:border-b-0 align-top"
            data-testid="risk-assessment-row">
          <td class="px-3 py-2 text-xs">{{ formatDate(row.created_at) }}</td>
          <td class="px-3 py-2 font-mono text-xs">{{ row.key }}</td>
          <td class="px-3 py-2">
            <ActionBadge :action="getRowAction(row)" data-testid="risk-assessment-action-badge" />
          </td>
          <td class="px-3 py-2">
            <div>{{ row.subject_type }}</div>
            <div class="font-mono text-xs text-muted-foreground">{{ row.subject_id }}</div>
          </td>
          <td class="px-3 py-2">
            <pre class="max-w-xl whitespace-pre-wrap break-words font-mono text-xs">{{ formatValue(row.value) }}</pre>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="rows.length === 0" class="p-8 text-center text-muted-foreground" data-testid="risk-assessment-empty">
      {{ $t('views.RiskAssessmentView.empty_description') }}
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatDateShort } from '../../lib/formatDate'
import type { EvidenceRow } from '../../lib/riskAssessment'
import ActionBadge from './ActionBadge.vue'
import SortIcon from './SortIcon.vue'

type SortKey = 'created_at' | 'key' | 'action' | 'subject'
type SortOrder = 'asc' | 'desc'

interface Props {
  rows: EvidenceRow[]
}

const props = defineProps<Props>()

useI18n()

const sortableColumns: Array<{ key: SortKey; testid: string; labelKey: string }> = [
  { key: 'created_at', testid: 'timestamp', labelKey: 'views.RiskAssessmentView.col_timestamp' },
  { key: 'key', testid: 'key', labelKey: 'views.RiskAssessmentView.col_key' },
  { key: 'action', testid: 'action', labelKey: 'views.RiskAssessmentView.col_action' },
  { key: 'subject', testid: 'subject', labelKey: 'views.RiskAssessmentView.col_subject' },
]

const sortKey = ref<SortKey>('created_at')
const sortOrder = ref<SortOrder>('desc')

function sortBy(key: SortKey): void {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortOrder.value = 'desc'
  }
}

function sortDirection(key: SortKey): 'asc' | 'desc' | 'none' {
  if (sortKey.value !== key) return 'none'
  return sortOrder.value
}

function ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
  if (sortKey.value !== key) return 'none'
  return sortOrder.value === 'asc' ? 'ascending' : 'descending'
}

function sortValue(row: EvidenceRow, key: SortKey): string | number {
  switch (key) {
    case 'created_at':
      return new Date(row.created_at).getTime()
    case 'key':
      return row.key
    case 'action':
      return getRowAction(row)
    case 'subject':
      return row.subject_id
  }
}

const sortedRows = computed(() => {
  const direction = sortOrder.value === 'asc' ? 1 : -1
  return [...props.rows].sort((a, b) => {
    const aVal = sortValue(a, sortKey.value)
    const bVal = sortValue(b, sortKey.value)
    if (aVal < bVal) return -direction
    if (aVal > bVal) return direction
    return 0
  })
})

/**
 * The action is resolved server-side from policy_gate_decisions (the route
 * joins the decision record). Never infer it from the key prefix — that
 * fabricated a value the summary card then contradicted.
 */
function getRowAction(row: EvidenceRow): string {
  return row.action ?? 'unknown'
}

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
