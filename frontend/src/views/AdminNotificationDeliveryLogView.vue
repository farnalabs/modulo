<template>
  <div class="mx-auto max-w-7xl space-y-6 p-6">
    <header>
      <h1 data-testid="admin-notification-log-title" class="text-3xl font-bold tracking-tight">Notification Delivery Log</h1>
      <p class="mt-1 text-muted-foreground">Admin view of all webhook notification deliveries</p>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Status</label>
          <select
            v-model="filterStatus"
            data-testid="admin-notification-log-status"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All statuses</option>
            <option value="delivered">Delivered</option>
            <option value="failed">Failed</option>
            <option value="dead_lettered">Dead Lettered</option>
            <option value="pending">Pending</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Event Type</label>
          <select
            v-model="filterEventType"
            data-testid="admin-notification-log-event-type"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All types</option>
            <option value="hitl_awaiting">HITL Awaiting</option>
            <option value="run_failed">Run Failed</option>
            <option value="claim_expired">Claim Expired</option>
            <option value="hitl_overdue">HITL Overdue</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">From</label>
          <input
            v-model="filterDateFrom"
            type="date"
            data-testid="admin-notification-log-date-from"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">To</label>
          <input
            v-model="filterDateTo"
            type="date"
            data-testid="admin-notification-log-date-to"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div class="flex items-end gap-2">
          <button
            data-testid="admin-notification-log-apply"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="applyFilters"
          >
            Apply
          </button>
          <button
            data-testid="admin-notification-log-reset"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetFilters"
          >
            Reset
          </button>
          <button
            v-if="hasRetryableItems"
            :disabled="retryingAll"
            data-testid="admin-notification-log-retry-all"
            class="rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-40 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300"
            @click="retryAllFailed"
          >
            {{ retryingAll ? 'Retrying All…' : 'Retry All Failed' }}
          </button>
        </div>
      </div>
      <div v-if="total > 0" class="mt-3 text-sm text-muted-foreground">
        {{ total }} delivery{{ total === 1 ? '' : 'ies' }}
      </div>
      <div
        v-if="retrySuccessMessage"
        data-testid="admin-notification-log-retry-success"
        class="mt-3 rounded-lg border border-success/50 bg-success/10 px-4 py-3 text-sm text-success"
      >
        {{ retrySuccessMessage }}
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDeliveries" />

    <div v-else-if="items.length === 0" data-testid="admin-notification-log-empty" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No delivery logs found</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Try adjusting your filters or wait for notifications to be sent.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="w-8 px-4 py-3"></th>
              <th class="px-4 py-3">Timestamp</th>
              <th class="px-4 py-3">Event Type</th>
              <th class="px-4 py-3">Destination</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Attempts</th>
              <th class="px-4 py-3">Error</th>
              <th class="px-4 py-3">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="entry in items"
              :key="entry.id"
              role="button"
              tabindex="0"
              class="transition-colors hover:bg-muted/30 cursor-pointer"
              @click="toggleRow(entry.id)"
              @keydown.enter="toggleRow(entry.id)"
              @keydown.space.prevent="toggleRow(entry.id)"
            >
              <td class="px-4 py-3 text-sm text-muted-foreground">
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
                  :class="expandedId === entry.id ? 'rotate-90' : ''"
                  class="transition-transform"
                >
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </td>
              <td class="whitespace-nowrap px-4 py-3 text-sm text-muted-foreground">
                {{ formatTimestamp(entry.created_at) }}
              </td>
              <td class="px-4 py-3 text-sm font-medium">{{ entry.event_type }}</td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="entry.endpoint_url ?? undefined">
                {{ entry.endpoint_url || '—' }}
              </td>
              <td class="px-4 py-3">
                <span :class="statusBadge(entry.status)" class="capitalize">
                  {{ entry.status }}
                </span>
              </td>
              <td class="px-4 py-3 text-sm text-muted-foreground">{{ entry.attempt_count }}</td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="entry.last_error ?? undefined">
                {{ entry.last_error || '—' }}
              </td>
              <td class="px-4 py-3">
                <div v-if="retryMessages[entry.id]" class="text-xs" :class="retryMessages[entry.id].type === 'error' ? 'text-destructive' : 'text-success'">
                  {{ retryMessages[entry.id].text }}
                </div>
                <button
                  v-else-if="entry.status === 'failed' || entry.status === 'dead_lettered'"
                  :disabled="retryingId === entry.id"
                  data-testid="admin-notification-log-retry"
                  class="rounded-md bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-40"
                  @click.stop="retryDelivery(entry)"
                >
                  {{ retryingId === entry.id ? 'Retrying…' : 'Retry' }}
                </button>
              </td>
            </tr>
            <template v-if="expandedId">
              <tr v-for="entry in expandedEntries" :key="`exp-${entry.id}`">
              <td colspan="8" class="bg-muted/20 px-4 py-3">
                <div class="space-y-2 text-sm">
                  <div v-if="entry.response_body" class="rounded border bg-card p-3">
                    <span class="text-xs font-medium text-muted-foreground">Response Body</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">{{ entry.response_body }}</pre>
                  </div>
                  <div v-if="entry.last_error" class="rounded border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-950/30">
                    <span class="text-xs font-medium text-red-600 dark:text-red-400">Error Details</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-red-700 dark:text-red-300">{{ entry.last_error }}</pre>
                  </div>
                  <div v-if="entry.response_code" class="text-muted-foreground">
                    <span class="text-xs font-medium">HTTP Response Code:</span>
                    <code class="ml-1 font-mono text-xs">{{ entry.response_code }}</code>
                  </div>
                  <div v-if="!entry.response_body && !entry.last_error && !entry.response_code" class="text-xs text-muted-foreground italic">
                    No additional details available.
                  </div>
                </div>
              </td>
            </tr>
            </template>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <button
          :disabled="!prevCursor"
          data-testid="admin-notification-log-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(prevCursor)"
        >
          Previous
        </button>
        <span class="text-sm text-muted-foreground">
          {{ items.length }} of {{ total }} deliveries
        </span>
        <button
          :disabled="!nextCursor"
          data-testid="admin-notification-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(nextCursor)"
        >
          Next
        </button>
      </div>

      <div v-if="deadLetteredCount > 0" data-testid="admin-notification-log-dlq" class="rounded-lg border bg-card p-4 shadow-sm">
        <div class="flex items-center justify-between">
          <div>
            <h3 class="text-lg font-semibold">Dead Letter Queue</h3>
            <p class="text-sm text-muted-foreground">
              {{ deadLetteredCount }} undeliverable notification{{ deadLetteredCount === 1 ? '' : 's' }} across all endpoints
            </p>
          </div>
          <button
            data-testid="admin-notification-log-dlq-filter"
            class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
            @click="showDeadLettered"
          >
            View Dead Lettered
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

type DeliveryLogEntry = components['schemas']['DeliveryLogEntry']

const items = ref<DeliveryLogEntry[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const total = ref(0)
const nextCursor = ref<string | null>(null)
const prevCursor = ref<string | null>(null)
const currentCursor = ref<string | null>(null)

const filterStatus = ref('')
const filterEventType = ref('')
const filterDateFrom = ref('')
const filterDateTo = ref('')

const expandedId = ref<string | null>(null)
const retryingId = ref<string | null>(null)
const retryingAll = ref(false)
const retrySuccessMessage = ref<string | null>(null)
const retryMessages = ref<Record<string, { type: string; text: string }>>({})

const hasRetryableItems = computed(() => items.value.some(e => e.status === 'failed' || e.status === 'dead_lettered'))

const deadLetteredCount = computed(() => items.value.filter(e => e.status === 'dead_lettered').length)

const expandedEntries = computed(() => {
  if (!expandedId.value) return []
  return items.value.filter(e => e.id === expandedId.value)
})

function formatTimestamp(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function statusBadge(status: string): string {
  if (status === 'delivered' || status === 'success') return 'badge badge-status-success'
  if (status === 'failed') return 'badge badge-status-destructive'
  if (status === 'dead_lettered') return 'badge badge-context-slate'
  if (status === 'pending') return 'badge badge-status-warning'
  return 'badge badge-context-slate'
}

function toggleRow(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

async function loadDeliveries(cursor?: string | null) {
  loading.value = true
  error.value = null
  retrySuccessMessage.value = null
  try {
    const params: Record<string, unknown> = { limit: 50 }
    if (cursor) params.cursor = cursor
    if (filterStatus.value) params.status = filterStatus.value
    if (filterEventType.value) params.event_type = filterEventType.value
    if (filterDateFrom.value) params.from = filterDateFrom.value
    if (filterDateTo.value) params.to = filterDateTo.value
    const { data, error: err } = await api.GET('/api/v1/admin/notifications/deliveries', {
      params: { query: params as any },
    })
    if (err) {
      error.value = `Failed to load delivery logs: ${err}`
    } else if (data) {
      items.value = data.items
      total.value = data.total
      nextCursor.value = data.next_cursor
      prevCursor.value = cursor ?? null
      currentCursor.value = cursor ?? null
      expandedId.value = null
    }
  } catch (e: unknown) {
    error.value = `Failed to load delivery logs: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function goToPage(cursor: string | null) {
  if (!cursor) return
  loadDeliveries(cursor)
}

function applyFilters() {
  loadDeliveries(null)
}

function resetFilters() {
  filterStatus.value = ''
  filterEventType.value = ''
  filterDateFrom.value = ''
  filterDateTo.value = ''
  loadDeliveries(null)
}

function showDeadLettered() {
  filterStatus.value = 'dead_lettered'
  filterEventType.value = ''
  filterDateFrom.value = ''
  filterDateTo.value = ''
  loadDeliveries(null)
}

async function retryDelivery(entry: DeliveryLogEntry) {
  if (!entry.endpoint_id) {
    retryMessages.value[entry.id] = { type: 'error', text: 'Cannot retry: missing endpoint ID' }
    return
  }
  retryingId.value = entry.id
  error.value = null
  delete retryMessages.value[entry.id]
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/admin/notifications/{webhook_id}/deliveries/{delivery_id}/retry',
      {
        params: {
          path: {
            webhook_id: entry.endpoint_id,
            delivery_id: entry.id,
          },
        },
      },
    )
    if (err) {
      retryMessages.value[entry.id] = { type: 'error', text: `Retry failed: ${err}` }
    } else if (data) {
      if (data.success) {
        await loadDeliveries(currentCursor.value)
        retryMessages.value[entry.id] = { type: 'success', text: 'Retry succeeded' }
      } else {
        await loadDeliveries(currentCursor.value)
        retryMessages.value[entry.id] = { type: 'error', text: `Retry failed: ${data.error || `HTTP ${data.status_code}`}` }
      }
    }
  } catch (e: unknown) {
    retryMessages.value[entry.id] = { type: 'error', text: `Retry request failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    retryingId.value = null
  }
}

async function retryAllFailed() {
  retryingAll.value = true
  retrySuccessMessage.value = null
  error.value = null
  retryMessages.value = {}
  try {
    const { data, error: err } = await api.POST('/api/v1/admin/notifications/deliveries/retry-all-failed', {})
    if (err) {
      error.value = `Retry all failed: ${err}`
    } else if (data) {
      await loadDeliveries(currentCursor.value)
      const msg = `Retried ${data.retried} deliver${data.retried === 1 ? 'y' : 'ies'}`
      retrySuccessMessage.value = data.success ? msg : `${msg} with ${data.errors?.length || 0} error(s)`
    }
  } catch (e: unknown) {
    error.value = `Retry all request failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    retryingAll.value = false
  }
}

onMounted(() => loadDeliveries(null))
</script>
