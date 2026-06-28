<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Notification Delivery Log</h1>
      <p class="mt-1 text-muted-foreground">Delivery history for all webhook notifications</p>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Status</label>
          <select
            v-model="filterStatus"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All statuses</option>
            <option value="delivered">Delivered</option>
            <option value="failed">Failed</option>
            <option value="dead_lettered">Dead Lettered</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">From</label>
          <input
            v-model="filterDateFrom"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">To</label>
          <input
            v-model="filterDateTo"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div class="flex items-end gap-2">
          <button
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="applyFilters"
          >
            Apply
          </button>
          <button
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

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
      <button class="ml-2 underline" @click="loadDeliveries()">Retry</button>
    </div>

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
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
              <th class="px-4 py-3">Event Type</th>
              <th class="px-4 py-3">Destination</th>
              <th class="px-4 py-3">Status</th>
              <th class="px-4 py-3">Attempts</th>
              <th class="px-4 py-3">Last Attempt</th>
              <th class="px-4 py-3">Error Detail</th>
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
                <span
                  class="inline-block rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="statusBadge(entry.status)"
                >
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
  if (status === 'delivered' || status === 'success') return 'bg-green-100 text-green-700'
  if (status === 'failed') return 'bg-red-100 text-red-700'
  if (status === 'dead_lettered') return 'bg-gray-100 text-gray-700'
  if (status === 'pending') return 'bg-amber-100 text-amber-700'
  return 'bg-gray-100 text-gray-700'
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
