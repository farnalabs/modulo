<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.RunsListView.runs')" :subtitle="$t('views.RunsListView.view_all_pipeline_executions')">
      <template #right>
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
              { value: RUN_STATUS.STALLED, label: 'Stalled' },
            ]},
            { key: 'trigger_type', label: 'Trigger Type', options: [
              { value: TRIGGER_TYPE.MANUAL, label: 'Manual' },
              { value: TRIGGER_TYPE.WEBHOOK, label: 'Webhook' },
              { value: TRIGGER_TYPE.CRON, label: 'Cron' },
              { value: TRIGGER_TYPE.POLLING, label: 'Polling' },
              { value: TRIGGER_TYPE.AGENT_SIGNAL, label: 'Agent Signal' },
              { value: TRIGGER_TYPE.ONGOING, label: 'Ongoing' },
              { value: TRIGGER_TYPE.CORRECTION, label: 'Correction' },
            ]},
          ]"
          :filter-values="{ status: filterStatus, trigger_type: filterTriggerType }"
          @update:search="filterSearch = $event"
          @update:filter="handleFilterUpdate"
        ></FilterBar>
      </template>
    </PageHeader>

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
            { key: 'heartbeat', label: $t('views.RunsListView.heartbeat'), sortable: false },
            { key: 'run_number', label: '#', numeric: true, sortable: true },
            { key: 'started_at', label: $t('views.RunsListView.start'), sortable: true },
            { key: 'completed_at', label: $t('views.RunsListView.end'), sortable: true },
            { key: 'duration', label: $t('views.RunsListView.duration') },
            { key: 'total_cost_usd', label: $t('views.RunsListView.cost'), numeric: true, sortable: true },
            { key: 'actions', label: '', sortable: false },
          ]"
          :rows="runs"
          :row-clickable="false"
        >
          <template #cell-pipeline_name="{ row }">
            <router-link
              :to="`/runs/${row.run_id}`"
              class="font-medium hover:underline"
              :data-testid="`runs-list-view-${row.run_id}`"
            >
              {{ row.pipeline_name || '(deleted pipeline)' }}
            </router-link>
          </template>
          <template #cell-status="{ value, row }">
            <div class="flex flex-wrap items-center gap-1">
              <span :class="runStatusBadgeClass(value as string)" class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium capitalize">
                {{ value }}
              </span>
              <span
                v-if="(row as RunListItem).capacity?.waiting"
                :data-testid="`runs-list-queued-${row.run_id}`"
                class="inline-flex items-center rounded-full bg-warning/10 px-2 py-0.5 text-xs font-medium text-warning capitalize"
              >{{ $t('views.RunsListView.queued') }}</span>
              <RunErrorTag
                v-if="(row as RunListItem).error_code"
                :code="(row as RunListItem).error_code"
                :detail="((row as RunListItem).error_detail as string | null | undefined)?.slice(0, 200)"
                :data-testid="`runs-list-error-${row.run_id}`"
              />
            </div>
          </template>
          <template #cell-trigger_type="{ value }">
            <span class="text-xs text-muted-foreground">{{ triggerTypeLabel(value as string | null | undefined, t) }}</span>
          </template>
          <template #cell-heartbeat="{ row }">
            <span
              :data-testid="`runs-list-heartbeat-${row.run_id}`"
              :class="isListHeartbeatStale(row as RunListItem) ? 'text-warning font-medium' : 'text-muted-foreground'"
              class="text-xs whitespace-nowrap"
            >{{ formatHeartbeat(heartbeatAgeFor(row as RunListItem, now)) }}</span>
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
            <span
              class="whitespace-nowrap tabular-nums text-muted-foreground"
              :data-testid="`runs-list-duration-${row.run_id}`"
            >
              <span
                v-if="isNonTerminalStatus(row.status as string) && row.started_at"
                role="status"
                aria-live="polite"
                class="tabular-nums"
              >{{ formatElapsed(row.started_at as string) }}</span>
              <span v-else>{{ formatDuration(row.started_at as string, row.completed_at as string) }}</span>
            </span>
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
          <template #cell-actions="{ row }">
            <div class="text-right">
              <button
                type="button"
                v-if="isNonTerminalStatus(row.status as string)"
                :disabled="cancellingIds.has(row.run_id as string)"
                :data-testid="`runs-list-cancel-${row.run_id}`"
                class="inline-flex items-center gap-1 rounded-lg border border-destructive/50 bg-destructive/10 px-2 py-1 text-xs font-medium text-destructive hover:bg-destructive/20 disabled:opacity-50"
                @click.stop="cancelRun(row as RunListItem)"
                @keydown.stop
              >
                <svg v-if="cancellingIds.has(row.run_id as string)" class="h-3.5 w-3.5 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
                <svg v-else xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
                {{ cancelLabel(row.run_id as string) }}
              </button>
              <span
                v-if="cancelErrors[row.run_id as string]"
                :data-testid="`runs-list-cancel-error-${row.run_id}`"
                role="alert"
                class="ml-2 text-xs text-destructive"
              >{{ cancelErrors[row.run_id as string] }}</span>
            </div>
          </template>
        </DataTable>
      </div>

      <div class="flex items-center justify-between">
        <span class="text-sm text-muted-foreground">
          {{ total }} run{{ total === 1 ? '' : 's' }}
        </span>
        <div class="flex items-center gap-2">
          <button
            type="button"
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
            type="button"
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
import RunErrorTag from '../components/shared/RunErrorTag.vue'
import { ref, computed, watch, onUnmounted } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'
import { fetchRuns, requestRunCancellation, type RunListItem, type FetchRunsParams } from '../lib/api/runs'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatApiError } from '../lib/api/formatError'
import { DataTable } from '../components/ui/data-table'
import EmptyState from '../components/shared/EmptyState.vue'
import { runStatusBadgeClass, formatRunDate, heartbeatAgeSeconds, isHeartbeatStale, formatHeartbeatAge, triggerTypeLabel } from '../utils/runUtils'
import { RUN_STATUS, TRIGGER_TYPE } from '../constants/filters'
import { isNonTerminalStatus } from '../constants/runStatuses'
import { formatMoney } from '../lib/money'
import { useOrgCurrency } from '../composables/useOrgCurrency'

const route = useRoute()
const router = useRouter()
const { currencyCode, loadCurrency } = useOrgCurrency()
const { t } = useI18n()

const confirmingIds = ref(new Set<string>())
const cancellingIds = ref(new Set<string>())
const cancelErrors = ref<Record<string, string>>({})

const pageSize = 20
const page = ref(parsePageParam(route.query.page))

const FILTER_STORAGE_KEY = 'runs-list-filters'

const filterStatus = ref(route.query.status as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.status`) || '')
const filterTriggerType = ref(route.query.trigger_type as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.trigger_type`) || '')
const filterSearch = ref(route.query.search as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.search`) || '')
const filterPipelineId = ref(route.query.pipeline_id as string || '')

function parsePageParam(raw: unknown): number {
  const n = Number(raw)
  if (!Number.isInteger(n) || n < 1) return 1
  return n
}

function syncQuery() {
  const query: LocationQuery = { ...route.query }
  if (page.value > 1) query.page = String(page.value)
  else delete query.page
  if (filterStatus.value) query.status = filterStatus.value
  else delete query.status
  if (filterTriggerType.value) query.trigger_type = filterTriggerType.value
  else delete query.trigger_type
  if (filterSearch.value) query.search = filterSearch.value
  else delete query.search
  if (filterPipelineId.value) query.pipeline_id = filterPipelineId.value
  else delete query.pipeline_id
  router.replace({ query })
}

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

// Live elapsed runtime for executing runs: a 1-second tick re-renders the
// duration cell only while non-terminal runs with a started_at are visible.
const now = ref(Date.now())
let elapsedTimer: ReturnType<typeof setInterval> | null = null

const hasElapsedRuns = computed(() =>
  runs.value.some((r) =>
    isNonTerminalStatus(r.status) && (!!r.started_at || !!r.heartbeat_at),
  ),
)

watch(hasElapsedRuns, (active) => {
  if (active && !elapsedTimer) {
    now.value = Date.now()
    elapsedTimer = setInterval(() => { now.value = Date.now() }, 1000)
  } else if (!active && elapsedTimer) {
    clearInterval(elapsedTimer)
    elapsedTimer = null
  }
}, { immediate: true })

onUnmounted(() => {
  if (elapsedTimer) {
    clearInterval(elapsedTimer)
    elapsedTimer = null
  }
  if (searchDebounceTimer) {
    clearTimeout(searchDebounceTimer)
    searchDebounceTimer = null
  }
})

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

const SEARCH_DEBOUNCE_MS = 300
let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null

watch(filterSearch, () => {
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer)
  searchDebounceTimer = setTimeout(() => {
    page.value = 1
    loadRuns()
    syncQuery()
  }, SEARCH_DEBOUNCE_MS)
})

function handleFilterUpdate(key: string, value: string) {
  if (key === 'status') filterStatus.value = value
  else if (key === 'trigger_type') filterTriggerType.value = value
  page.value = 1
  loadRuns()
  syncQuery()
}

function nextPage() {
  if (page.value * pageSize >= total.value) return
  page.value++
  loadRuns()
  syncQuery()
}

function prevPage() {
  if (page.value <= 1) return
  page.value--
  loadRuns()
  syncQuery()
}

function cancelLabel(runId: string): string {
  if (cancellingIds.value.has(runId)) return t('views.RunsListView.stopping')
  if (confirmingIds.value.has(runId)) return t('views.RunsListView.stop_confirm')
  return t('views.RunsListView.stop')
}

async function cancelRun(run: RunListItem) {
  const runId = run.run_id
  if (!isNonTerminalStatus(run.status)) return
  if (cancellingIds.value.has(runId)) return
  if (!confirmingIds.value.has(runId)) {
    confirmingIds.value = new Set([...confirmingIds.value, runId])
    return
  }
  confirmingIds.value = new Set([...confirmingIds.value].filter((id) => id !== runId))
  cancellingIds.value = new Set([...cancellingIds.value, runId])
  cancelErrors.value = { ...cancelErrors.value, [runId]: '' }
  try {
    const { error } = await requestRunCancellation(runId, t('views.RunsListView.cancel_failed'))
    if (error) {
      cancelErrors.value = { ...cancelErrors.value, [runId]: error }
      return
    }
    if (runsData.value) {
      runsData.value = {
        ...runsData.value,
        items: runsData.value.items.map((r) => (r.run_id === runId ? { ...r, status: 'cancelled' } : r)),
      }
    }
    await loadRuns()
  } finally {
    cancellingIds.value = new Set([...cancellingIds.value].filter((id) => id !== runId))
  }
}

loadCurrency()

function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined): string {
  if (!startIso || !endIso) return '—'
  const start = new Date(startIso)
  const end = new Date(endIso)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return '—'
  let totalSeconds = Math.max(0, Math.round((end.getTime() - start.getTime()) / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  totalSeconds -= hours * 3600
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

function formatElapsed(startIso: string): string {
  return `${formatDuration(startIso, new Date(now.value).toISOString())} ${t('views.RunsListView.elapsed')}`
}

function heartbeatAgeFor(run: RunListItem, nowMs: number): number | null {
  return heartbeatAgeSeconds(run.heartbeat_at, run.status, nowMs)
}

function isListHeartbeatStale(run: RunListItem): boolean {
  return isHeartbeatStale(heartbeatAgeFor(run, now.value))
}

function formatHeartbeat(age: number | null): string {
  return formatHeartbeatAge(age, t, 'views.RunsListView.ago')
}

</script>
