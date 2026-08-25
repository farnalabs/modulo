<template>
  <FeatureGate feature-name="error_tracking" required-tier="team" show-disabled>
  <PageTabs :tabs="[
    { label: 'Dashboard', to: '/admin/errors' },
  ]" />
  <div class="page-wide">
    <PageHeader :title="$t('views.AdminErrorsView.error_dashboard')" :subtitle="$t('views.AdminErrorsView.monitor_and_manage_errors_across_your_organisation')" />

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
        <Button @click="applyFilters">{{ $t('views.AdminErrorsView.apply_filters') }}</Button>
        <button type="button" class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" @click="resetFilters">{{ $t('common.reset') }}</button>
      </template>
    </FilterBar>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadGroups" />

    <EmptyState
      v-else-if="groups.length === 0"
      :title="$t('views.AdminErrorsView.no_error_groups_found')"
      description="Try adjusting your filters or wait for errors to be ingested."
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
          <template #cell-sample_message="{ value }">
            <span class="max-w-xs truncate font-medium">{{ value || '(no message)' }}</span>
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
import { ref, computed } from 'vue'
import { watchDebounced } from '@vueuse/core'
import { useRouter } from 'vue-router'
import { fetchErrorGroups, type ErrorGroupSummary, type FetchErrorGroupsParams } from '../lib/api/errors'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"
import { shortId } from '../utils/format'
import { formatApiError } from "../lib/api/formatError"
import Button from 'primevue/button'
import { DataTable } from '../components/ui/data-table'
import EmptyState from '../components/shared/EmptyState.vue'

const router = useRouter()

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

const { data: groupsData, loading, error, load: loadGroups } = useDataFetch<{ items: ErrorGroupSummary[]; total: number }>(
  () => fetchErrorGroups(buildParams()).then(
    d => ({ data: d }),
    e => ({ error: { detail: `Failed to load error groups: ${formatApiError(e)}` } }),
  ),
  { initialValue: { items: [] as ErrorGroupSummary[], total: 0 } },
)

const groups = computed(() => groupsData.value?.items ?? [])
const total = computed(() => groupsData.value?.total ?? 0)

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

function applyFilters() {
  currentPage.value = 1
  offset.value = 0
  loadGroups()
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
