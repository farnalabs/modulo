<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Error Dashboard</h1>
      <p class="mt-1 text-muted-foreground">Monitor and manage errors across your organisation</p>
    </header>

    <div class="card p-4">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Level</label>
          <select
            v-model="filterLevel"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All levels</option>
            <option value="error">Error</option>
            <option value="warning">Warning</option>
            <option value="critical">Critical</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Status</label>
          <select
            v-model="filterStatus"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All statuses</option>
            <option value="new">New</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="resolved">Resolved</option>
            <option value="archived">Archived</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Source</label>
          <select
            v-model="filterSource"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All sources</option>
            <option value="backend">Backend</option>
            <option value="frontend">Frontend</option>
            <option value="celery">Celery</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Environment</label>
          <input
            v-model="filterEnvironment"
            type="text"
            placeholder="e.g. production"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </div>
      <div class="mt-3 flex items-center gap-2">
        <div class="flex-1">
          <input
            v-model="filterSearch"
            type="text"
            placeholder="Search error messages..."
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <button
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          @click="applyFilters"
        >
          Apply Filters
        </button>
        <button
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          @click="resetFilters"
        >
          Reset
        </button>
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadGroups" />

    <div v-else-if="groups.length === 0" class="card p-8 text-center">
      <p class="text-lg font-medium">No error groups found</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Try adjusting your filters or wait for errors to be ingested.
      </p>
    </div>

    <template v-else>
      <div class="card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/30 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">Level</th>
              <th class="px-4 py-3">Message</th>
              <th class="px-4 py-3">Count</th>
              <th class="px-4 py-3">First Seen</th>
              <th class="px-4 py-3">Last Seen</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Assignee</th>
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
              <td class="px-4 py-3">
                <span :class="levelBadgeClass(group.level_peak)">
                  {{ group.level_peak }}
                </span>
              </td>
              <td class="max-w-xs truncate px-4 py-3 text-sm font-medium">
                {{ group.sample_message || '(no message)' }}
              </td>
              <td class="px-4 py-3 text-sm">{{ group.count }}</td>
              <td class="whitespace-nowrap px-4 py-3 text-sm text-muted-foreground">
                {{ formatDate(group.first_seen) }}
              </td>
              <td class="whitespace-nowrap px-4 py-3 text-sm text-muted-foreground">
                {{ formatDate(group.last_seen) }}
              </td>
              <td class="px-4 py-3">
                <span :class="statusBadgeClass(group.status)">
                  {{ group.status }}
                </span>
              </td>
              <td class="px-4 py-3 text-xs text-muted-foreground">
                {{ group.assigned_to ? group.assigned_to.slice(0, 8) + '...' : '—' }}
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
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { fetchErrorGroups, type ErrorGroupSummary, type FetchErrorGroupsParams } from '../lib/api/errors'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

const router = useRouter()

const groups = ref<ErrorGroupSummary[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const total = ref(0)
const limit = ref(20)
const offset = ref(0)
const currentPage = ref(1)

const filterLevel = ref('')
const filterStatus = ref('')
const filterSource = ref('')
const filterEnvironment = ref('')
const filterSearch = ref('')

function buildParams(offs?: number): FetchErrorGroupsParams {
  const params: FetchErrorGroupsParams = { limit: limit.value }
  if (offs !== undefined) params.offset = offs
  else params.offset = offset.value
  if (filterLevel.value) params.level = filterLevel.value
  if (filterStatus.value) params.status = filterStatus.value
  if (filterSource.value) params.source = filterSource.value
  if (filterEnvironment.value) params.environment = filterEnvironment.value
  if (filterSearch.value) params.search = filterSearch.value
  return params
}

async function loadGroups(offs?: number) {
  loading.value = true
  error.value = null
  try {
    const data = await fetchErrorGroups(buildParams(offs))
    groups.value = data.items
    total.value = data.total
  } catch (e: unknown) {
    error.value = `Failed to load error groups: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  currentPage.value = 1
  offset.value = 0
  loadGroups(0)
}

function resetFilters() {
  filterLevel.value = ''
  filterStatus.value = ''
  filterSource.value = ''
  filterEnvironment.value = ''
  filterSearch.value = ''
  currentPage.value = 1
  offset.value = 0
  loadGroups(0)
}

function nextPage() {
  const newOffset = offset.value + limit.value
  if (newOffset >= total.value) return
  currentPage.value++
  offset.value = newOffset
  loadGroups(newOffset)
}

function prevPage() {
  const newOffset = Math.max(0, offset.value - limit.value)
  if (newOffset === offset.value) return
  currentPage.value--
  offset.value = newOffset
  loadGroups(newOffset)
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

onMounted(() => loadGroups(0))
</script>
