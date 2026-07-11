<template>
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
          { value: 'celery', label: 'Celery' },
        ]},
      ]"
      :filter-values="{ level: filterLevel, status: filterStatus, source: filterSource }"
      @update:search="filterSearch = $event"
      @update:filter="handleFilterUpdate"
    >
      <template #after>
        <Button variant="default" @click="applyFilters">Apply Filters</Button>
        <button class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent" @click="resetFilters">Reset</button>
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
        <table class="w-full">
          <thead>
            <tr>
              <th class="table-header">Level</th>
              <th class="table-header">Message</th>
              <th class="table-header table-cell-numeric">Count</th>
              <th class="table-header">{{ $t('views.AdminErrorDetailView.first_seen') }}</th>
              <th class="table-header">{{ $t('views.AdminErrorDetailView.last_seen') }}</th>
              <th class="table-header">Status</th>
              <th class="table-header">Assignee</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="group in groups"
              :key="group.id"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              role="button"
              tabindex="0"
              @click="navigateToDetail(group.id)"
              @keydown.enter="navigateToDetail(group.id)"
              @keydown.space.prevent="navigateToDetail(group.id)"
            >
              <td class="table-cell">
                <span :class="levelBadgeClass(group.level_peak)">
                  {{ group.level_peak }}
                </span>
              </td>
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <td class="table-cell max-w-xs truncate font-medium">{{ group.sample_message || '(no message)' }}</td>
                </TooltipTrigger>
                <TooltipContent side="top" class="max-w-xs">
                  <p>{{ group.sample_message || '(no message)' }}</p>
                </TooltipContent>
              </Tooltip>
              <td class="table-cell-numeric">{{ group.count }}</td>
              <td class="table-cell whitespace-nowrap text-muted-foreground">
                {{ formatDate(group.first_seen) }}
              </td>
              <td class="table-cell whitespace-nowrap text-muted-foreground">
                {{ formatDate(group.last_seen) }}
              </td>
              <td class="table-cell">
                <span :class="statusBadgeClass(group.status)">
                  {{ group.status }}
                </span>
              </td>
              <td class="table-cell text-xs text-muted-foreground font-mono">
                {{ group.assigned_to ? shortId(group.assigned_to) : '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <span class="text-sm text-muted-foreground">
          {{ total }} group{{ total === 1 ? '' : 's' }}
        </span>
        <div class="flex items-center gap-2">
          <button
            :disabled="offset <= 0"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
            @click="prevPage"
          >
            Previous
          </button>
          <span class="text-sm text-muted-foreground">
            Page {{ currentPage }}
          </span>
          <button
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
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { fetchErrorGroups, type ErrorGroupSummary, type FetchErrorGroupsParams } from '../lib/api/errors'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"
import { shortId } from '../utils/format'
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'
import { formatApiError } from "../lib/api/formatError"
import { Button } from '@/components/ui/button'
import EmptyState from '../components/shared/EmptyState.vue'

const router = useRouter()

const limit = ref(20)
const offset = ref(0)
const currentPage = ref(1)

const { data: groupsData, loading, error, load: loadGroups } = useDataFetch(
  () => fetchErrorGroups(buildParams()).then(
    d => ({ data: d }),
    e => ({ error: { detail: `Failed to load error groups: ${formatApiError(e)}` } }),
  ),
  { initialValue: { items: [] as ErrorGroupSummary[], total: 0 } },
)

const groups = computed(() => groupsData.value?.items ?? [])
const total = computed(() => groupsData.value?.total ?? 0)

const filterLevel = ref('')
const filterStatus = ref('')
const filterSource = ref('')
const filterEnvironment = ref('')
const filterSearch = ref('')

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
