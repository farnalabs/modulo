<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminNotificationDeliveryLogView.notification_delivery_log') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SettingsNotificationLogView.delivery_history_for_all_webhook_notifications') }}</p>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Status</label>
          <select
            v-model="filterStatus"
            data-testid="settings-notification-log-status"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">{{ $t('views.AdminErrorsView.all_statuses') }}</option>
            <option value="delivered">Delivered</option>
            <option value="failed">Failed</option>
            <option value="dead_lettered">{{ $t('views.AdminNotificationDeliveryLogView.dead_lettered') }}</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">From</label>
          <input
            v-model="filterDateFrom"
            type="date"
            data-testid="settings-notification-log-date-from"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">To</label>
          <input
            v-model="filterDateTo"
            type="date"
            data-testid="settings-notification-log-date-to"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div class="flex items-end gap-2">
          <button
            data-testid="settings-notification-log-apply"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="applyFilters"
          >
            Apply
          </button>
          <button
            data-testid="settings-notification-log-reset"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetFilters"
          >
            Reset
          </button>
        </div>
      </div>
      <div v-if="total > 0" class="mt-3 text-sm text-muted-foreground">
        {{ total }} delivery{{ total === 1 ? '' : 'ies' }}
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDeliveries" />

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.AdminNotificationDeliveryLogView.no_delivery_logs_found') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Try adjusting your filters or wait for notifications to be sent.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="px-4 py-3">Destination</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Attempts</th>
              <th class="px-4 py-3">{{ $t('views.SettingsNotificationLogView.last_attempt') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsNotificationLogView.error_detail') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="entry in items"
              :key="entry.id"
              class="transition-colors hover:bg-muted/30"
            >
              <td class="px-4 py-3 text-sm font-medium">{{ entry.event_type }}</td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="entry.endpoint_url ?? undefined">
                {{ entry.endpoint_url || '—' }}
              </td>
              <td class="px-4 py-3">
                <span :class="statusBadge(entry.status)">
                  {{ entry.status }}
                </span>
              </td>
              <td class="px-4 py-3 text-sm text-muted-foreground">{{ entry.attempt_count }}</td>
              <td class="whitespace-nowrap px-4 py-3 text-sm text-muted-foreground">
                {{ formatTimestamp(entry.created_at) }}
              </td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="entry.last_error ?? undefined">
                {{ entry.last_error || '—' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <button
          :disabled="!prevCursor"
          data-testid="settings-notification-log-previous"
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
          data-testid="settings-notification-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(nextCursor)"
        >
          Next
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
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

const filterStatus = ref('')
const filterDateFrom = ref('')
const filterDateTo = ref('')

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

async function loadDeliveries(cursor?: string | null) {
  loading.value = true
  error.value = null
  try {
    const params: Record<string, unknown> = { limit: 50 }
    if (cursor) params.cursor = cursor
    if (filterStatus.value) params.status = filterStatus.value
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
  filterDateFrom.value = ''
  filterDateTo.value = ''
  loadDeliveries(null)
}

onMounted(() => loadDeliveries(null))
</script>
