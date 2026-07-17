<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.RunsListView.runs')" :subtitle="$t('views.RunsListView.view_all_pipeline_executions')" />

    <FilterBar
      :search="{ placeholder: $t('views.RunsListView.search_by_pipeline_name') }"
      :search-value="filterSearch"
      :filters="[
        { key: 'status', label: 'Status', options: [
          { value: RUN_STATUS.PENDING, label: 'Pending' },
          { value: RUN_STATUS.RUNNING, label: 'Running' },
          { value: RUN_STATUS.AWAITING_HUMAN, label: 'Awaiting Human' },
          { value: RUN_STATUS.COMPLETE, label: 'Complete' },
          { value: RUN_STATUS.FAILED, label: 'Failed' },
          { value: RUN_STATUS.CANCELLED, label: 'Cancelled' },
          { value: RUN_STATUS.EVAL_FAILED, label: 'Eval Failed' },
        ]},
        { key: 'trigger_type', label: 'Trigger Type', options: [
          { value: TRIGGER_TYPE.MANUAL, label: 'Manual' },
          { value: TRIGGER_TYPE.WEBHOOK, label: 'Webhook' },
          { value: TRIGGER_TYPE.CRON, label: 'Cron' },
          { value: TRIGGER_TYPE.CORRECTION, label: 'Correction' },
        ]},
      ]"
      :filter-values="{ status: filterStatus, trigger_type: filterTriggerType }"
      @update:search="filterSearch = $event"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" @click="resetFilters">Reset</button>
      </template>
    </FilterBar>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadRuns" />

    <EmptyState
      v-else-if="runs.length === 0"
      :title="$t('views.RunsListView.no_runs_found')"
      description="Try adjusting your filters or trigger a pipeline run."
    />

    <template v-else>
      <div class="table-wrapper">
        <DataTable
          :columns="[
            { key: 'pipeline_name', label: $t('views.RunsListView.pipeline'), sortable: true },
            { key: 'status', label: $t('views.RunsListView.status'), sortable: true },
            { key: 'trigger_type', label: $t('views.RunsListView.trigger'), sortable: true },
            { key: 'run_number', label: '#', numeric: true, sortable: true },
            { key: 'created_at', label: $t('views.RunsListView.created'), sortable: true },
            { key: 'total_cost_usd', label: $t('views.RunsListView.cost'), numeric: true, sortable: true },
          ]"
          :rows="runs"
          @row-click="(row: any) => navigateToDetail(row.run_id)"
        >
          <template #cell-pipeline_name="{ value }">
            <span class="font-medium">{{ value || '(deleted pipeline)' }}</span>
          </template>
          <template #cell-status="{ value }">
            <span :class="runStatusBadgeClass(value as string)" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium capitalize">
              {{ value }}
            </span>
          </template>
          <template #cell-trigger_type="{ value }">
            <span class="text-xs text-muted-foreground capitalize">{{ value || '—' }}</span>
          </template>
          <template #cell-run_number="{ value }">
            <span class="tabular-nums">{{ value ?? '—' }}</span>
          </template>
          <template #cell-created_at="{ value }">
            <span class="whitespace-nowrap text-muted-foreground">{{ formatRunDate(value as string) }}</span>
          </template>
          <template #cell-total_cost_usd="{ value }">
            <span class="tabular-nums">{{ value != null ? '$' + Number(value).toFixed(4) : '—' }}</span>
          </template>
        </DataTable>
      </div>

      <div class="flex items-center justify-between">
        <span class="text-sm text-muted-foreground">
          {{ total }} run{{ total === 1 ? '' : 's' }}
        </span>
        <div class="flex items-center gap-2">
          <button
            :disabled="page <= 1"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="prevPage"
          >
            Previous
          </button>
          <span class="text-sm text-muted-foreground">
            Page {{ page }}
          </span>
          <button
            :disabled="page * pageSize >= total"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="nextPage"
          >
            Next
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { fetchRuns, type RunListItem, type FetchRunsParams } from '../lib/api/runs'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatApiError } from '../lib/api/formatError'
import { DataTable } from '../components/ui/data-table'
import EmptyState from '../components/shared/EmptyState.vue'
import { runStatusBadgeClass, formatRunDate } from '../utils/runUtils'
import { RUN_STATUS, TRIGGER_TYPE } from '../constants/filters'

const router = useRouter()
const route = useRoute()

const pageSize = 20
const page = ref(1)

const { data: runsData, loading, error, load: loadRuns } = useDataFetch<{ items: RunListItem[]; total: number }>(
  () => fetchRuns(buildParams()).then(
    d => ({ data: d }),
    e => ({ error: { detail: `Failed to load runs: ${formatApiError(e)}` } }),
  ),
  { initialValue: { items: [] as RunListItem[], total: 0 } },
)

const runs = computed(() => runsData.value?.items ?? [])
const total = computed(() => runsData.value?.total ?? 0)

const FILTER_STORAGE_KEY = 'runs-list-filters'

const filterStatus = ref(route.query.status as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.status`) || '')
const filterTriggerType = ref(route.query.trigger_type as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.trigger_type`) || '')
const filterSearch = ref(route.query.search as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.search`) || '')

watch([filterStatus, filterTriggerType, filterSearch], ([status, triggerType, search]) => {
  localStorage.setItem(`${FILTER_STORAGE_KEY}.status`, status)
  localStorage.setItem(`${FILTER_STORAGE_KEY}.trigger_type`, triggerType)
  localStorage.setItem(`${FILTER_STORAGE_KEY}.search`, search)
})

function handleFilterUpdate(key: string, value: string) {
  if (key === 'status') filterStatus.value = value
  else if (key === 'trigger_type') filterTriggerType.value = value
  page.value = 1
  loadRuns()
}

function buildParams(): FetchRunsParams {
  const params: FetchRunsParams = { page: page.value, page_size: pageSize }
  if (filterStatus.value) params.status = filterStatus.value
  if (filterTriggerType.value) params.trigger_type = filterTriggerType.value
  if (filterSearch.value) params.search = filterSearch.value
  return params
}

function resetFilters() {
  filterStatus.value = ''
  filterTriggerType.value = ''
  filterSearch.value = ''
  localStorage.removeItem(`${FILTER_STORAGE_KEY}.status`)
  localStorage.removeItem(`${FILTER_STORAGE_KEY}.trigger_type`)
  localStorage.removeItem(`${FILTER_STORAGE_KEY}.search`)
  page.value = 1
  loadRuns()
}

function nextPage() {
  if (page.value * pageSize >= total.value) return
  page.value++
  loadRuns()
}

function prevPage() {
  if (page.value <= 1) return
  page.value--
  loadRuns()
}

function navigateToDetail(id: string) {
  router.push(`/runs/${id}`)
}

</script>
