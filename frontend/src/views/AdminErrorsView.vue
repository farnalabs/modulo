<template>
  <FeatureGate feature-name="error_tracking" required-tier="team" show-disabled>
  <div class="page-wide">
    <PageTabs :tabs="[
      { label: 'Dashboard', to: '/admin/errors' },
    ]" />
    <PageHeader :title="$t('views.AdminErrorsView.error_dashboard')" :subtitle="$t('views.AdminErrorsView.monitor_and_manage_errors_across_your_organisation')" />

    <!-- Instance scope (FAR-1547): system admins can switch the dashboard to
         the SYSTEM_ORG_ID sentinel partition (instance-level / unattributed
         errors). Hidden entirely for tenant users — they can never read it. -->
    <div
      v-if="isSystemAdmin"
      class="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2"
      role="group"
      :aria-label="$t('views.AdminErrorsView.scope_group_label')"
      data-testid="admin-errors-scope"
    >
      <span class="text-sm font-medium text-muted-foreground">{{ $t('views.AdminErrorsView.scope_label') }}</span>
      <div class="inline-flex rounded-lg border border-input bg-background p-0.5">
        <button
          type="button"
          class="rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="!isInstanceScope ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'"
          :aria-pressed="!isInstanceScope"
          data-testid="admin-errors-scope-organisation"
          @click="setScope('organisation')"
        >
          {{ $t('views.AdminErrorsView.scope_organisation') }}
        </button>
        <button
          type="button"
          class="rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="isInstanceScope ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'"
          :aria-pressed="isInstanceScope"
          data-testid="admin-errors-scope-instance"
          @click="setScope('instance')"
        >
          {{ $t('views.AdminErrorsView.scope_instance') }}
        </button>
      </div>
      <p
        v-if="isInstanceScope"
        class="basis-full text-sm text-muted-foreground sm:basis-auto"
        aria-live="polite"
        data-testid="admin-errors-scope-hint"
      >
        {{ $t('views.AdminErrorsView.scope_instance_hint') }}
      </p>
    </div>

    <div
      v-if="!isInstanceScope && starvationItems.length > 0"
      data-testid="scheduler-starvation"
      class="rounded-lg border border-warning/50 bg-warning/10 p-4 mb-4"
    >
      <div class="font-medium mb-1">{{ $t('views.AdminErrorsView.scheduler_starvation') }}</div>
      <p class="text-sm text-muted-foreground mb-3">
        {{ $t('views.AdminErrorsView.scheduler_starvation_hint', { minutes: starvationThresholdMinutes }) }}
      </p>
      <div class="flex flex-wrap items-center gap-x-8 gap-y-1 text-sm font-medium text-muted-foreground mb-1 px-2">
        <span class="min-w-0 flex-1">{{ $t('views.AdminErrorsView.scheduler_starvation_pipeline') }}</span>
        <span>{{ $t('views.AdminErrorsView.scheduler_starvation_pending_count') }}</span>
        <span>{{ $t('views.AdminErrorsView.scheduler_starvation_oldest_wait') }}</span>
      </div>
      <div
        v-for="item in starvationItems"
        :key="item.pipeline_id"
        class="flex flex-wrap items-center gap-x-8 gap-y-1 text-sm px-2 py-1 rounded hover:bg-warning/10"
      >
        <span class="min-w-0 flex-1 truncate font-medium">{{ item.pipeline_name || shortId(item.pipeline_id) }}</span>
        <span class="font-mono">{{ item.pending_count }}</span>
        <span class="font-mono whitespace-nowrap">{{ formatStarvationAge(item.oldest_age_minutes) }}</span>
      </div>
    </div>

    <FilterBar
      :search="{ placeholder: $t('views.AdminErrorsView.search_error_messages') }"
      :search-value="filterSearch"
      :filters="[
        { key: 'level', label: 'Level', options: [
          { value: 'error', label: 'Error' },
          { value: 'warning', label: 'Warning' },
          { value: 'critical', label: 'Critical' },
        ]},
        { key: 'status', label: 'Status', options: [
          { value: 'new', label: 'New' },
          { value: 'acknowledged', label: 'Acknowledged' },
          { value: 'resolved', label: 'Resolved' },
          { value: 'archived', label: 'Archived' },
        ]},
        { key: 'source', label: 'Source', options: [
          { value: 'backend', label: 'Backend' },
          { value: 'frontend', label: 'Frontend' },
          { value: 'saq', label: 'SAQ' },
          { value: 'celery', label: 'Celery (legacy)' },
        ]},
      ]"
      :filter-values="{ level: filterLevel, status: filterStatus, source: filterSource }"
      @update:search="filterSearch = $event"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <button type="button" class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" data-testid="admin-errors-reset" @click="resetFilters">{{ $t('common.reset') }}</button>
      </template>
    </FilterBar>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadGroups" />

    <EmptyState
      v-else-if="groups.length === 0"
      :title="isInstanceScope ? $t('views.AdminErrorsView.no_instance_error_groups_found') : $t('views.AdminErrorsView.no_error_groups_found')"
      :description="isInstanceScope ? $t('views.AdminErrorsView.no_instance_error_groups_description') : $t('views.AdminErrorsView.no_error_groups_description')"
    />

    <template v-else>
      <div class="table-wrapper">
        <DataTable
          :columns="[
            { key: 'level_peak', label: 'Level' },
            { key: 'sample_message', label: 'Message' },
            { key: 'count', label: 'Count', numeric: true },
            { key: 'first_seen', label: $t('views.AdminErrorDetailView.first_seen') },
            { key: 'last_seen', label: $t('views.AdminErrorDetailView.last_seen') },
            { key: 'status', label: 'Status' },
            { key: 'assignee', label: 'Assignee' },
          ]"
          :rows="groups"
          @row-click="(row: any) => navigateToDetail(row.id)"
        >
          <template #cell-level_peak="{ value }">
            <span :class="levelBadgeClass(value as string)">
              {{ value }}
            </span>
          </template>
          <template #cell-sample_message="{ row, value }">
            <div class="flex items-start gap-1">
              <button
                type="button"
                class="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                :aria-label="isMessageExpanded((row as any).id) ? $t('views.AdminErrorsView.collapse_message') : $t('views.AdminErrorsView.expand_message')"
                :aria-expanded="isMessageExpanded((row as any).id)"
                :data-testid="'admin-errors-expand-' + (row as any).id"
                @click.stop="toggleMessage((row as any).id)"
                @keydown.stop
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  class="transition-transform"
                  :class="isMessageExpanded((row as any).id) ? 'rotate-90' : ''"
                  aria-hidden="true"
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </button>
              <span
                v-if="isMessageExpanded((row as any).id)"
                data-testid="admin-errors-message-full"
                class="min-w-0 max-w-xs whitespace-normal break-words font-medium"
              >{{ value || '(no message)' }}</span>
              <span
                v-else
                data-testid="admin-errors-message-truncated"
                class="min-w-0 max-w-xs truncate font-medium"
              >{{ value || '(no message)' }}</span>
            </div>
          </template>
          <template #cell-first_seen="{ value }">
            <span class="whitespace-nowrap text-muted-foreground">{{ formatDate(value as string) }}</span>
          </template>
          <template #cell-last_seen="{ value }">
            <span class="whitespace-nowrap text-muted-foreground">{{ formatDate(value as string) }}</span>
          </template>
          <template #cell-status="{ value }">
            <span :class="statusBadgeClass(value as string)">
              {{ value }}
            </span>
          </template>
          <template #cell-assignee="{ row }">
            <span class="text-xs text-muted-foreground font-mono">{{ (row as any).assigned_to ? shortId((row as any).assigned_to) : '—' }}</span>
          </template>
        </DataTable>
      </div>

      <div class="flex items-center justify-between">
        <span class="text-sm text-muted-foreground">
          {{ total }} group{{ total === 1 ? '' : 's' }}
        </span>
        <div class="flex items-center gap-2">
          <button type="button"
            :disabled="offset <= 0"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="prevPage"
          >
            Previous
          </button>
          <span class="text-sm text-muted-foreground">
            Page {{ currentPage }}
          </span>
          <button type="button"
            :disabled="offset + limit >= total"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="nextPage"
          >
            Next
          </button>
        </div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed, watch } from 'vue'
import { watchDebounced, useIntervalFn } from '@vueuse/core'
import { useRoute, useRouter } from 'vue-router'
import {
  fetchErrorGroups,
  fetchSchedulerStarvation,
  type ErrorGroupSummary,
  type ErrorListResponse,
  type FetchErrorGroupsParams,
  type SchedulerStarvationResponse,
} from '../lib/api/errors'
import { api } from '../lib/api/client'
import { useCurrentUser } from '../composables/useCurrentUser'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"
import { shortId } from '../utils/format'
import { formatApiError, throwOnError } from "../lib/api/formatError"
import { DataTable } from '../components/ui/data-table'
import EmptyState from '../components/shared/EmptyState.vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const { isSystemAdmin } = useCurrentUser()

// FAR-1547: the instance scope reads the SYSTEM_ORG_ID sentinel partition
// (system admin only). The query param is the single source of truth so the
// scope survives the trip to the detail view and back; a forged ?scope=instance
// from a non-system-admin resolves to the tenant scope here AND is refused 403
// by the backend, so a tenant can never see (or be shown) instance rows.
const scope = computed<'organisation' | 'instance'>(() =>
  isSystemAdmin.value && route.query.scope === 'instance' ? 'instance' : 'organisation',
)
const isInstanceScope = computed(() => scope.value === 'instance')

function setScope(next: 'organisation' | 'instance') {
  if (next === scope.value) return
  router.push({
    path: '/admin/errors',
    query: next === 'instance' ? { scope: 'instance' } : {},
  })
}

const limit = ref(20)
const offset = ref(0)
const currentPage = ref(1)

const filterLevel = ref('')
const filterStatus = ref('')
const filterSource = ref('')
const filterEnvironment = ref('')
const filterSearch = ref('')

watchDebounced(filterSearch, () => {
  currentPage.value = 1
  offset.value = 0
  loadGroups()
}, { debounce: 300 })

// Auto-apply on dropdown filter changes (immediate)
watch([filterLevel, filterStatus, filterSource], () => {
  currentPage.value = 1
  offset.value = 0
  loadGroups()
})

// FAR-1547: organisation scope reads the tenant partition (existing helper);
// instance scope reads the system-admin-only sentinel partition. Both share
// the same filters, pagination and rendering.
async function fetchGroups(): Promise<ErrorListResponse> {
  const params = buildParams()
  if (!isInstanceScope.value) return fetchErrorGroups(params)
  return throwOnError(
    await api.GET('/api/v1/errors/instance', {
      params: { query: params as unknown as Record<string, unknown> },
    }),
  ) as ErrorListResponse
}

const { data: groupsData, loading, error, load: loadGroups } = useDataFetch<{ items: ErrorGroupSummary[]; total: number }>(
  () => fetchGroups().then(
    d => ({ data: d }),
    e => ({ error: { detail: `Failed to load error groups: ${formatApiError(e)}` } }),
  ),
  { initialValue: { items: [] as ErrorGroupSummary[], total: 0 } },
)

const groups = computed(() => groupsData.value?.items ?? [])
const total = computed(() => groupsData.value?.total ?? 0)

// Scheduler-starvation banner (FAR-604): capacity-blocked pending runs never
// produce error events, so without this surface a pipeline stuck at its
// concurrency cap is invisible here. Fail-open: a starvation fetch failure
// renders nothing — never blocks the error-group dashboard. The banner must
// track a LIVE incident, not a mount-time snapshot (an operator staring at
// this page mid-wedge is exactly when fresh data matters), so the fetch is
// re-polled every 60s — useDataFetch's staleTime would otherwise freeze the
// data at the mount fetch (refetchOnWindowFocus is disabled app-wide).
const { data: starvationData, load: loadStarvation } = useDataFetch<SchedulerStarvationResponse>(
  // Instance scope reads the sentinel partition, which has no pipelines of its
  // own: the starvation query is tenant-scoped, so skip it rather than polling
  // the caller's org from the instance view.
  () => (isInstanceScope.value
    ? Promise.resolve({ data: { items: [], total: 0, threshold_minutes: 10 } })
    : fetchSchedulerStarvation().then(
        d => ({ data: d }),
        e => ({ error: { detail: `Failed to load scheduler starvation: ${formatApiError(e)}` } }),
      )),
  { initialValue: { items: [], total: 0, threshold_minutes: 10 } },
)
useIntervalFn(loadStarvation, 60_000)

const starvationItems = computed(() => starvationData.value?.items ?? [])
const starvationThresholdMinutes = computed(() => starvationData.value?.threshold_minutes ?? 10)

// Per-row message expand state (FAR-655): client-side only — a row's full
// sample message is already in the list payload, so expanding wraps it inside
// the bounded column instead of navigating or refetching.
const expandedMessageIds = ref<Set<string>>(new Set())

function isMessageExpanded(id: string): boolean {
  return expandedMessageIds.value.has(id)
}

function toggleMessage(id: string) {
  const next = new Set(expandedMessageIds.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  expandedMessageIds.value = next
}

// A scope switch re-queries against a different partition: reset pagination
// and the per-row expand state, then reload through the new scope's endpoint.
watch(scope, () => {
  currentPage.value = 1
  offset.value = 0
  expandedMessageIds.value = new Set()
  loadGroups()
})

function formatStarvationAge(minutes: number): string {
  if (minutes < 120) return t('views.AdminErrorsView.scheduler_starvation_age_minutes', { minutes: Math.round(minutes) })
  return t('views.AdminErrorsView.scheduler_starvation_age_hours', { hours: Math.round((minutes / 60) * 10) / 10 })
}

function handleFilterUpdate(key: string, value: string) {
  if (key === 'level') filterLevel.value = value
  else if (key === 'status') filterStatus.value = value
  else if (key === 'source') filterSource.value = value
}

function buildParams(): FetchErrorGroupsParams {
  const params: FetchErrorGroupsParams = { limit: limit.value, offset: offset.value }
  if (filterLevel.value) params.level = filterLevel.value
  if (filterStatus.value) params.status = filterStatus.value
  if (filterSource.value) params.source = filterSource.value
  if (filterEnvironment.value) params.environment = filterEnvironment.value
  if (filterSearch.value) params.search = filterSearch.value
  return params
}

function resetFilters() {
  filterLevel.value = ''
  filterStatus.value = ''
  filterSource.value = ''
  filterEnvironment.value = ''
  filterSearch.value = ''
  currentPage.value = 1
  offset.value = 0
  loadGroups()
}

function nextPage() {
  const newOffset = offset.value + limit.value
  if (newOffset >= total.value) return
  currentPage.value++
  offset.value = newOffset
  loadGroups()
}

function prevPage() {
  const newOffset = Math.max(0, offset.value - limit.value)
  if (newOffset === offset.value) return
  currentPage.value--
  offset.value = newOffset
  loadGroups()
}

function navigateToDetail(id: string) {
  if (isInstanceScope.value) {
    // Carry the scope so the detail view reads (and renders read-only) the
    // same sentinel partition the row came from.
    router.push({ path: `/admin/errors/${id}`, query: { scope: 'instance' } })
    return
  }
  router.push(`/admin/errors/${id}`)
}

function levelBadgeClass(level: string): string {
  if (level === 'critical') return 'badge badge-status-destructive'
  if (level === 'warning') return 'badge badge-status-warning'
  return 'badge badge-context-blue'
}

function statusBadgeClass(status: string): string {
  if (status === 'new') return 'badge badge-status-destructive'
  if (status === 'acknowledged') return 'badge badge-status-warning'
  if (status === 'resolved') return 'badge badge-status-success'
  if (status === 'archived') return 'badge badge-status-muted'
  return 'badge'
}

function formatDate(dateStr: string): string {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/* onMounted handled by useDataFetch */
</script>
