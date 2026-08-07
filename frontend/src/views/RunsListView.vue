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
        <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" @click="resetFilters">{{ $t('common.reset') }}</button>
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
            { key: 'started_at', label: $t('views.RunsListView.start'), sortable: true },
            { key: 'completed_at', label: $t('views.RunsListView.end'), sortable: true },
            { key: 'duration', label: $t('views.RunsListView.duration') },
            { key: 'total_cost_usd', label: $t('views.RunsListView.cost'), numeric: true, sortable: true },
          ]"
          :rows="runs"
          @row-click="(row: any) => navigateToDetail(row.run_id)"
        >
          <template #cell-pipeline_name="{ row, value }">
            <router-link
              :to="`/runs/${row.run_id}`"
              class="font-medium hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 rounded"
              @click.stop
            >
              {{ value || '(deleted pipeline)' }}
            </router-link>
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
          <template #cell-started_at="{ value }">
            <span class="whitespace-nowrap text-muted-foreground">{{ formatRunDate(value as string) || '—' }}</span>
          </template>
          <template #cell-completed_at="{ value }">
            <span class="whitespace-nowrap text-muted-foreground">{{ formatRunDate(value as string) || '—' }}</span>
          </template>
          <template #cell-duration="{ row }">
            <span class="whitespace-nowrap tabular-nums text-muted-foreground">{{ formatDuration(row.started_at as string, row.completed_at as string) }}</span>
          </template>
          <template #cell-total_cost_usd="{ value, row }">
            <span class="tabular-nums">
              <template v-if="aggregateCosts[row.run_id as string] != null">
                <span data-testid="runs-list-aggregate-cost">{{ formatMoney(aggregateCosts[row.run_id as string] as number, currencyCode, 4) }}</span>
                <span v-if="childCounts[row.run_id as string]" class="ml-1 text-xs text-muted-foreground">{{ $t('views.RunsListView.cost_includes_child_runs_count', childCounts[row.run_id as string]) }}</span>
                <span v-else class="ml-1 text-xs text-muted-foreground">{{ $t('views.RunsListView.cost_includes_child_runs') }}</span>
              </template>
              <span v-else>{{ value != null ? formatMoney(Number(value), currencyCode, 4) : '—' }}</span>
            </span>
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
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const router = useRouter()
const route = useRoute()
const { currencyCode, loadCurrency } = useOrgCurrency()

const pageSize = 20
const page = ref(1)

const FILTER_STORAGE_KEY = 'runs-list-filters'

const filterStatus = ref(route.query.status as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.status`) || '')
const filterTriggerType = ref(route.query.trigger_type as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.trigger_type`) || '')
const filterSearch = ref(route.query.search as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.search`) || '')
const filterPipelineId = ref(route.query.pipeline_id as string || '')

function buildParams(): FetchRunsParams {
  const params: FetchRunsParams = { page: page.value, page_size: pageSize }
  if (filterStatus.value) params.status = filterStatus.value
  if (filterTriggerType.value) params.trigger_type = filterTriggerType.value
  if (filterSearch.value) params.search = filterSearch.value
  if (filterPipelineId.value) params.pipeline_id = filterPipelineId.value
  return params
}

const { data: runsData, loading, error, load: loadRuns } = useDataFetch<{ items: RunListItem[]; total: number }>(
  () => fetchRuns(buildParams()).then(
    d => ({ data: d }),
    e => ({ error: { detail: `Failed to load runs: ${formatApiError(e)}` } }),
  ),
  { initialValue: { items: [] as RunListItem[], total: 0 } },
)

const runs = computed(() => runsData.value?.items ?? [])
const total = computed(() => runsData.value?.total ?? 0)

function aggregateCostValue(run: RunListItem): number | null {
  if (run.aggregate_cost_usd == null || run.aggregate_cost_usd === '') return null
  const aggregate = Number(run.aggregate_cost_usd)
  if (!Number.isFinite(aggregate)) return null
  const own = run.total_cost_usd == null ? 0 : Number(run.total_cost_usd)
  const ownSafe = Number.isFinite(own) ? own : 0
  if (Math.abs(aggregate - ownSafe) < 1e-9) return null
  return aggregate
}

const aggregateCosts = computed<Record<string, number>>(() => {
  const byRunId: Record<string, number> = {}
  for (const run of runs.value) {
    const value = aggregateCostValue(run)
    if (value != null) byRunId[run.run_id] = value
  }
  return byRunId
})


const childCounts = computed<Record<string, number>>(() => {
  const byRunId: Record<string, number> = {}
  for (const run of runs.value) {
    const count = run.child_runs_count
    if (Number.isInteger(count) && (count ?? 0) > 0) byRunId[run.run_id] = count as number
  }
  return byRunId
})

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

loadCurrency()

function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined): string {
  if (!startIso || !endIso) return '—'
  const start = new Date(startIso)
  const end = new Date(endIso)
  if (isNaN(start.getTime()) || isNaN(end.getTime())) return '—'
  let totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  totalSeconds -= hours * 3600
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

</script>
