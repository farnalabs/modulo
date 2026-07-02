<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.SettingsTriggerEventLogView.trigger_event_log') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.SettingsTriggerEventLogView.event_history_for_all_triggers_across_the_organisation') }}</p>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.SettingsTriggerEventLogView.trigger_type') }}</label>
          <select
            v-model="filterTriggerType"
            data-testid="settings-trigger-event-log-trigger-type"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">{{ $t('views.AdminNotificationDeliveryLogView.all_types') }}</option>
            <option value="manual">Manual</option>
            <option value="webhook">Webhook</option>
            <option value="cron">Cron</option>
            <option value="polling">Polling</option>
            <option value="agent_signal">{{ $t('views.SettingsTriggerEventLogView.agent_signal') }}</option>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Result</label>
          <select
            v-model="filterResult"
            data-testid="settings-trigger-event-log-result"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">{{ $t('views.SettingsTriggerEventLogView.all_results') }}</option>
            <option value="accepted">Accepted</option>
            <option value="passed">Passed</option>
            <option value="condition_met">{{ $t('views.SettingsTriggerEventLogView.condition_met') }}</option>
            <option value="signal_fired">{{ $t('views.SettingsTriggerEventLogView.signal_fired') }}</option>
            <option value="no_match">{{ $t('views.SettingsTriggerEventLogView.no_match') }}</option>
            <option value="hmac_failed">{{ $t('views.SettingsTriggerEventLogView.hmac_failed') }}</option>
            <option value="schema_validation_failed">{{ $t('views.SettingsTriggerEventLogView.schema_validation_failed') }}</option>
            <option value="deduplicated">Deduplicated</option>
            <option value="concurrency_limit_reached">{{ $t('views.SettingsTriggerEventLogView.concurrency_limit_reached') }}</option>
            <option value="flood_rejected">{{ $t('views.SettingsTriggerEventLogView.flood_rejected') }}</option>
            <option value="timestamp_expired">{{ $t('views.SettingsTriggerEventLogView.timestamp_expired') }}</option>
            <option value="validation_failed">{{ $t('views.SettingsTriggerEventLogView.validation_failed') }}</option>
            <option value="rate_limited">{{ $t('views.SettingsTriggerEventLogView.rate_limited') }}</option>
            <option value="poll_error">{{ $t('views.SettingsTriggerEventLogView.poll_error') }}</option>
          </select>
        </div>
        <div class="flex items-end gap-2">
          <button
            data-testid="settings-trigger-event-log-apply"
            class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="applyFilters"
          >
            Apply
          </button>
          <button
            data-testid="settings-trigger-event-log-reset"
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

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvents" />

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.SettingsTriggerEventLogView.no_trigger_events_found') }}</p>
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
              <th class="px-4 py-3">{{ $t('views.SettingsNotificationLogView.error_detail') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggerEventLogView.trigger_id') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in items"
              :key="event.id"
              class="transition-colors hover:bg-muted/30"
            >
              <td class="px-4 py-3">
                <span :class="typeBadge(event.trigger_type)">
                  {{ event.trigger_type }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span :class="resultBadge(event.validation_result)">
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
          data-testid="settings-trigger-event-log-previous"
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
          data-testid="settings-trigger-event-log-next"
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
  if (type === 'manual') return 'badge badge-context-blue'
  if (type === 'webhook') return 'badge badge-context-purple'
  if (type === 'cron') return 'badge badge-context-amber'
  if (type === 'polling') return 'badge badge-context-cyan'
  if (type === 'agent_signal') return 'badge badge-context-indigo'
  return 'badge badge-context-slate'
}

function resultBadge(result: string): string {
  if (result === 'accepted' || result === 'passed' || result === 'condition_met' || result === 'signal_fired') return 'badge badge-status-success'
  if (result === 'no_match') return 'badge badge-context-slate'
  if (result === 'hmac_failed' || result === 'schema_validation_failed' || result === 'validation_failed') return 'badge badge-status-destructive'
  if (result === 'deduplicated' || result === 'concurrency_limit_reached' || result === 'flood_rejected' || result === 'rate_limited') return 'badge badge-context-orange'
  if (result === 'timestamp_expired') return 'badge badge-context-slate'
  if (result === 'poll_error') return 'badge badge-context-rose'
  return 'badge badge-context-slate'
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
