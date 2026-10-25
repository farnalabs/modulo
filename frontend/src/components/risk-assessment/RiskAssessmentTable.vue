<template>
  <div class="overflow-x-auto rounded-lg border bg-card shadow-sm" data-testid="risk-assessment-table">
    <table class="w-full text-left text-sm">
      <thead>
        <tr class="border-b bg-muted/50">
          <th scope="col" class="px-3 py-2 font-medium cursor-pointer hover:bg-muted"
              @click="sortBy('created_at')"
              data-testid="risk-assessment-sort-timestamp">
            {{ $t('views.RiskAssessmentView.col_timestamp') }}
            <SortIcon :direction="sortDirection('created_at')" />
          </th>
          <th scope="col" class="px-3 py-2 font-medium cursor-pointer hover:bg-muted"
              @click="sortBy('key')"
              data-testid="risk-assessment-sort-key">
            {{ $t('views.RiskAssessmentView.col_key') }}
            <SortIcon :direction="sortDirection('key')" />
          </th>
          <th scope="col" class="px-3 py-2 font-medium cursor-pointer hover:bg-muted"
              @click="sortBy('action')"
              data-testid="risk-assessment-sort-action">
            {{ $t('views.RiskAssessmentView.col_action') }}
            <SortIcon :direction="sortDirection('action')" />
          </th>
          <th scope="col" class="px-3 py-2 font-medium cursor-pointer hover:bg-muted"
              @click="sortBy('subject')"
              data-testid="risk-assessment-sort-subject">
            {{ $t('views.RiskAssessmentView.col_subject') }}
            <SortIcon :direction="sortDirection('subject')" />
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

interface Props {
  rows: Array<{
    id: string
    key: string
    subject_type: string
    subject_id: string
    value: unknown
    created_at: string
    producer_type: string
    producer_id: string | null
  }>
}

const props = defineProps<Props>()

useI18n()

const sortKey = ref<'created_at' | 'key' | 'action' | 'subject'>('created_at')
const sortOrder = ref<'asc' | 'desc'>('desc')

function sortBy(key: 'created_at' | 'key' | 'action' | 'subject'): void {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortOrder.value = 'desc'
  }
}

function sortDirection(key: 'created_at' | 'key' | 'action' | 'subject'): 'asc' | 'desc' | 'none' {
  if (sortKey.value !== key) return 'none'
  return sortOrder.value
}

const sortedRows = computed(() => {
  return [...props.rows].sort((a, b) => {
    let aVal: string | number
    let bVal: string | number

    switch (sortKey.value) {
      case 'created_at':
        aVal = new Date(a.created_at).getTime()
        bVal = new Date(b.created_at).getTime()
        break
      case 'key':
        aVal = a.key
        bVal = b.key
        break
      case 'action':
        aVal = getRowAction(a)
        bVal = getRowAction(b)
        break
      case 'subject':
        aVal = a.subject_id
        bVal = b.subject_id
        break
      default:
        return 0
    }

    if (aVal < bVal) return sortOrder.value === 'asc' ? -1 : 1
    if (aVal > bVal) return sortOrder.value === 'asc' ? 1 : -1
    return 0
  })
})

function getRowAction(row: Props['rows'][0]): string {
  // Try to extract action from subject_id for node_execution rows
  // Format: "{run_id}:{node_id}" - we'd need to join to policy_gate_decisions
  // For now, check if the key contains action hints or return 'unknown'
  if (row.key.startsWith('risk.')) return 'risk'
  if (row.key.startsWith('assessment.')) return 'assessment'
  return 'unknown'
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

<style scoped>
/* Sort icon styles */
</style>
