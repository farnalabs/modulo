<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.RunsListView.runs')" :subtitle="$t('views.RunsListView.view_all_pipeline_executions')">
      <template #right>
        <FilterBar
          :search="{ placeholder: $t('views.RunsListView.search_by_pipeline_name') }"
          :search-value="filterSearch"
          :filters="[
            { key: 'status', label: $t('views.RunsListView.status_filter'), options: [
              { value: RUN_STATUS.PENDING, label: $t('views.RunsListView.status_pending') },
              { value: RUN_STATUS.RUNNING, label: $t('views.RunsListView.status_running') },
              { value: RUN_STATUS.AWAITING_HUMAN, label: $t('views.RunsListView.status_awaiting_human') },
              { value: RUN_STATUS.COMPLETE, label: $t('views.RunsListView.status_complete') },
              { value: RUN_STATUS.FAILED, label: $t('views.RunsListView.status_failed') },
              { value: RUN_STATUS.CANCELLED, label: $t('views.RunsListView.status_cancelled') },
              { value: RUN_STATUS.EVAL_FAILED, label: $t('views.RunsListView.status_eval_failed') },
              { value: RUN_STATUS.STALLED, label: $t('views.RunsListView.status_stalled') },
            ]},
            { key: 'trigger_type', label: $t('views.RunsListView.trigger_type_filter'), options: [
              { value: TRIGGER_TYPE.MANUAL, label: $t('views.RunsListView.trigger_manual') },
              { value: TRIGGER_TYPE.WEBHOOK, label: $t('views.RunsListView.trigger_webhook') },
              { value: TRIGGER_TYPE.CRON, label: $t('views.RunsListView.trigger_cron') },
              { value: TRIGGER_TYPE.POLLING, label: $t('views.RunsListView.trigger_polling') },
              { value: TRIGGER_TYPE.AGENT_SIGNAL, label: $t('views.RunsListView.trigger_agent_signal') },
              { value: TRIGGER_TYPE.ONGOING, label: $t('views.RunsListView.trigger_ongoing') },
              { value: TRIGGER_TYPE.CORRECTION, label: $t('views.RunsListView.trigger_correction') },
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

    <template v-else>
      <!-- An emptied cursor page still renders the footer when the user can
           go back, so they are never stranded without Prev. -->
      <EmptyState
        v-if="runs.length === 0"
        :title="$t('views.RunsListView.no_runs_found')"
        :description="$t('views.RunsListView.empty_state_description')"
      />

      <div v-else class="table-wrapper">
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
              {{ row.pipeline_name || $t('views.RunsListView.deleted_pipeline') }}
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
                <LoaderCircle v-if="cancellingIds.has(row.run_id as string)" aria-hidden="true" class="h-3.5 w-3.5 animate-spin" />
                <Square v-else aria-hidden="true" class="h-3.5 w-3.5" />
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

      <div
        v-if="runs.length > 0 || cursorStack.length > 0"
        class="flex items-center justify-between"
      >
        <span class="text-sm text-muted-foreground">
          {{ $t('views.RunsListView.run_count', total) }}
        </span>
        <div class="flex items-center gap-2">
          <button
            type="button"
            :disabled="!canGoPrev"
            data-testid="runs-list-prev-page"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="prevPage"
          >
            {{ $t('views.RunsListView.previous') }}
          </button>
          <span v-if="positionKnown" class="text-sm text-muted-foreground">
            {{ $t('views.RunsListView.page_label', { page: pagePosition }) }}
          </span>
          <button
            type="button"
            :disabled="!canGoNext"
            data-testid="runs-list-next-page"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="nextPage"
          >
            {{ $t('views.RunsListView.next') }}
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
import { ref, computed, watch, onUnmounted, onMounted } from 'vue'
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
import { LoaderCircle, Square } from '@lucide/vue'

const route = useRoute()
const router = useRouter()
const { currencyCode, loadCurrency } = useOrgCurrency()
const { t } = useI18n()

const confirmingIds = ref(new Set<string>())
const cancellingIds = ref(new Set<string>())
const cancelErrors = ref<Record<string, string>>({})

const pageSize = 20
const FILTER_STORAGE_KEY = 'runs-list-filters'

// Cursor-based pagination: the list walks pages via the opaque `next_cursor`
// the backend returns (keyset pagination), never via deep-page OFFSET. "Prev"
// pops a stack of previously visited cursors; there is no absolute page jump.
const cursor = ref(parseCursorParam(route.query.cursor))
const cursorStack = ref<Array<string | null>>([])
const pagePosition = ref(1)
// The visited-page count is only meaningful for in-session navigation — a
// deep link straight onto a cursor has no known position, so the label hides;
// a bare mount is page 1 by definition.
const positionKnown = ref(!cursor.value)

const filterStatus = ref(route.query.status as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.status`) || '')
const filterTriggerType = ref(route.query.trigger_type as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.trigger_type`) || '')
const filterSearch = ref(route.query.search as string || localStorage.getItem(`${FILTER_STORAGE_KEY}.search`) || '')
const filterPipelineId = ref(route.query.pipeline_id as string || '')

function parseCursorParam(raw: unknown): string | null {
  return typeof raw === 'string' && raw.length > 0 ? raw : null
}

function syncQuery() {
  const query: LocationQuery = { ...route.query }
  // `page` is the retired offset param — scrub it so stale deep links clean up.
  delete query.page
  if (cursor.value) query.cursor = cursor.value
  else delete query.cursor
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
  const params: FetchRunsParams = { page_size: pageSize }
  if (cursor.value) params.cursor = cursor.value
  if (filterStatus.value) params.status = filterStatus.value
  if (filterTriggerType.value) params.trigger_type = filterTriggerType.value
  if (filterSearch.value) params.search = filterSearch.value
  if (filterPipelineId.value) params.pipeline_id = filterPipelineId.value
  return params
}

const { data: runsData, loading, error, load: loadRuns } = useDataFetch<{ items: RunListItem[]; total: number; next_cursor: string | null; has_more: boolean }>(
  () => fetchRuns(buildParams()).then(
    d => ({ data: d }),
    e => ({ error: { detail: t('views.RunsListView.failed_to_load_runs', { detail: formatApiError(e) }) } }),
  ),
  { initialValue: { items: [] as RunListItem[], total: 0, next_cursor: null, has_more: false } },
)

const runs = computed(() => runsData.value?.items ?? [])
const total = computed(() => runsData.value?.total ?? 0)
const nextCursor = computed(() => runsData.value?.next_cursor ?? null)
const hasMore = computed(() => runsData.value?.has_more ?? false)
const canGoNext = computed(() => hasMore.value && !!nextCursor.value)
const canGoPrev = computed(() => cursorStack.value.length > 0)

function resetPagination() {
  cursor.value = null
  cursorStack.value = []
  pagePosition.value = 1
  positionKnown.value = true
}

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

onMounted(() => {
  if (route.query.page !== undefined) {
    const query: LocationQuery = { ...route.query }
    delete query.page
    router.replace({ query })
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
    resetPagination()
    loadRuns()
    syncQuery()
  }, SEARCH_DEBOUNCE_MS)
})

function handleFilterUpdate(key: string, value: string) {
  if (key === 'status') filterStatus.value = value
  else if (key === 'trigger_type') filterTriggerType.value = value
  resetPagination()
  loadRuns()
  syncQuery()
}

function nextPage() {
  if (loading.value) return
  if (!canGoNext.value) return
  cursorStack.value = [...cursorStack.value, cursor.value]
  cursor.value = nextCursor.value
  pagePosition.value += 1
  positionKnown.value = true
  loadRuns()
  syncQuery()
}

function prevPage() {
  if (loading.value) return
  if (cursorStack.value.length === 0) return
  const stack = [...cursorStack.value]
  cursor.value = stack.pop() ?? null
  cursorStack.value = stack
  pagePosition.value = Math.max(1, pagePosition.value - 1)
  positionKnown.value = true
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
