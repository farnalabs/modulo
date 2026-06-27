<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Trigger Event Log</h1>
      <p class="mt-1 text-muted-foreground">Event history for all triggers across the organisation</p>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Trigger Type</label>
          <select
            v-model="filterTriggerType"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All types</option>
            <option value="manual">Manual</option>
            <option value="webhook">Webhook</option>
            <option value="cron">Cron</option>
            <option value="polling">Polling</option>
            <option value="agent_signal">Agent Signal</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Result</label>
          <select
            v-model="filterResult"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All results</option>
            <option value="accepted">Accepted</option>
            <option value="passed">Passed</option>
            <option value="condition_met">Condition Met</option>
            <option value="signal_fired">Signal Fired</option>
            <option value="no_match">No Match</option>
            <option value="hmac_failed">HMAC Failed</option>
            <option value="schema_validation_failed">Schema Validation Failed</option>
            <option value="deduplicated">Deduplicated</option>
            <option value="concurrency_limit_reached">Concurrency Limit Reached</option>
            <option value="flood_rejected">Flood Rejected</option>
            <option value="timestamp_expired">Timestamp Expired</option>
            <option value="validation_failed">Validation Failed</option>
            <option value="rate_limited">Rate Limited</option>
            <option value="poll_error">Poll Error</option>
          </select>
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
        {{ total }} event{{ total === 1 ? '' : 's' }}
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
      <button class="ml-2 underline" @click="loadEvents()">Retry</button>
    </div>

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No trigger events found</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Try adjusting your filters or wait for trigger activity to be recorded.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">Type</th>
              <th class="px-4 py-3">Result</th>
              <th class="px-4 py-3">Timestamp</th>
              <th class="px-4 py-3">Run</th>
              <th class="px-4 py-3">Error Detail</th>
              <th class="px-4 py-3">Trigger ID</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in items"
              :key="event.id"
              class="transition-colors hover:bg-muted/30"
            >
              <td class="px-4 py-3">
                <span
                  class="inline-block rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="typeBadge(event.trigger_type)"
                >
                  {{ event.trigger_type }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span
                  class="inline-block rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="resultBadge(event.validation_result)"
                >
                  {{ event.validation_result }}
                </span>
              </td>
              <td class="whitespace-nowrap px-4 py-3 text-sm text-muted-foreground">
                {{ formatTimestamp(event.received_at) }}
              </td>
              <td class="px-4 py-3 text-sm">
                <span v-if="event.run_id" class="font-mono text-xs text-muted-foreground/70">
                  {{ truncateId(event.run_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="event.error_detail ?? undefined">
                {{ event.error_detail || '—' }}
              </td>
              <td class="px-4 py-3 font-mono text-xs text-muted-foreground/70">
                {{ truncateId(event.trigger_id) }}
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
          {{ items.length }} of {{ total }} events
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

type TriggerEventItem = components['schemas']['TriggerEventItem']

const items = ref<TriggerEventItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const total = ref(0)
const nextCursor = ref<string | null>(null)
const prevCursor = ref<string | null>(null)

const filterTriggerType = ref('')
const filterResult = ref('')

function truncateId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) + '...' : id
}

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

function typeBadge(type: string): string {
  if (type === 'manual') return 'bg-blue-100 text-blue-700'
  if (type === 'webhook') return 'bg-purple-100 text-purple-700'
  if (type === 'cron') return 'bg-amber-100 text-amber-700'
  if (type === 'polling') return 'bg-cyan-100 text-cyan-700'
  if (type === 'agent_signal') return 'bg-indigo-100 text-indigo-700'
  return 'bg-gray-100 text-gray-700'
}

function resultBadge(result: string): string {
  if (result === 'accepted' || result === 'passed' || result === 'condition_met' || result === 'signal_fired') return 'bg-green-100 text-green-700'
  if (result === 'no_match') return 'bg-slate-100 text-slate-700'
  if (result === 'hmac_failed' || result === 'schema_validation_failed' || result === 'validation_failed') return 'bg-red-100 text-red-700'
  if (result === 'deduplicated' || result === 'concurrency_limit_reached' || result === 'flood_rejected' || result === 'rate_limited') return 'bg-orange-100 text-orange-700'
  if (result === 'timestamp_expired') return 'bg-gray-100 text-gray-700'
  if (result === 'poll_error') return 'bg-rose-100 text-rose-700'
  return 'bg-gray-100 text-gray-700'
}

async function loadEvents(cursor?: string | null) {
  loading.value = true
  error.value = null
  try {
    const params: Record<string, unknown> = { limit: 50 }
    if (cursor) params.cursor = cursor
    if (filterTriggerType.value) params.trigger_type = filterTriggerType.value
    if (filterResult.value) params.validation_result = filterResult.value
    const { data, error: err } = await api.GET('/api/v1/admin/trigger-events', {
      params: { query: params as any },
    })
    if (err) {
      error.value = `Failed to load trigger events: ${err}`
    } else if (data) {
      items.value = data.items
      total.value = data.total
      nextCursor.value = data.next_cursor
      prevCursor.value = cursor ?? null
    }
  } catch (e: unknown) {
    error.value = `Failed to load trigger events: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function goToPage(cursor: string | null) {
  if (!cursor) return
  loadEvents(cursor)
}

function applyFilters() {
  loadEvents(null)
}

function resetFilters() {
  filterTriggerType.value = ''
  filterResult.value = ''
  loadEvents(null)
}

onMounted(() => loadEvents(null))
</script>
