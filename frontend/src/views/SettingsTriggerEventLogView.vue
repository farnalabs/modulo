<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.SettingsTriggerEventLogView.trigger_event_log')" :subtitle="$t('views.SettingsTriggerEventLogView.event_history_for_all_triggers_across_the_organisation')" />

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label for="settingstriggereventlogview-trigger-type" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.SettingsTriggerEventLogView.trigger_type') }}</label>
          <Select
  :aria-label="$t('views.SettingsTriggerEventLogView.trigger_type')"
  v-model="filterTriggerType"
  :placeholder="$t('views.SettingsTriggerEventLogView.trigger_type')"
  data-testid="settings-trigger-event-log-trigger-type"
  id="settingstriggereventlogview-trigger-type"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminNotificationDeliveryLogView.all_types') }, { value: 'manual', label: $t('views.SettingsTriggerEventLogView.manual') }, { value: 'webhook', label: $t('views.SettingsTriggerEventLogView.webhook') }, { value: 'cron', label: $t('views.SettingsTriggerEventLogView.cron') }, { value: 'polling', label: $t('views.SettingsTriggerEventLogView.polling') }, { value: 'agent_signal', label: $t('views.SettingsTriggerEventLogView.agent_signal') }, { value: 'ongoing', label: $t('views.SettingsTriggerEventLogView.ongoing') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div>
          <label for="settingstriggereventlogview-result" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.SettingsTriggerEventLogView.result') }}</label>
          <Select
  :aria-label="$t('views.SettingsTriggerEventLogView.result')"
  v-model="filterResult"
  :placeholder="$t('views.SettingsTriggerEventLogView.result')"
  data-testid="settings-trigger-event-log-result"
  id="settingstriggereventlogview-result"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.SettingsTriggerEventLogView.all_results') }, { value: 'accepted', label: $t('views.SettingsTriggerEventLogView.accepted') }, { value: 'passed', label: $t('views.SettingsTriggerEventLogView.passed') }, { value: 'condition_met', label: $t('views.SettingsTriggerEventLogView.condition_met') }, { value: 'signal_fired', label: $t('views.SettingsTriggerEventLogView.signal_fired') }, { value: 'no_match', label: $t('views.SettingsTriggerEventLogView.no_match') }, { value: 'hmac_failed', label: $t('views.SettingsTriggerEventLogView.hmac_failed') }, { value: 'schema_validation_failed', label: $t('views.SettingsTriggerEventLogView.schema_validation_failed') }, { value: 'deduplicated', label: $t('views.SettingsTriggerEventLogView.deduplicated') }, { value: 'concurrency_limit_reached', label: $t('views.SettingsTriggerEventLogView.concurrency_limit_reached') }, { value: 'backpressure_skipped', label: $t('views.SettingsTriggerEventLogView.backpressure_skipped') }, { value: 'coalesced', label: $t('views.SettingsTriggerEventLogView.coalesced') }, { value: 'spend_limit_reached', label: $t('views.SettingsTriggerEventLogView.spend_limit_reached') }, { value: 'flood_rejected', label: $t('views.SettingsTriggerEventLogView.flood_rejected') }, { value: 'timestamp_expired', label: $t('views.SettingsTriggerEventLogView.timestamp_expired') }, { value: 'validation_failed', label: $t('views.SettingsTriggerEventLogView.validation_failed') }, { value: 'rate_limited', label: $t('views.SettingsTriggerEventLogView.rate_limited') }, { value: 'poll_error', label: $t('views.SettingsTriggerEventLogView.poll_error') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div class="flex items-end gap-2">
          <Button data-testid="settings-trigger-event-log-apply" @click="applyFilters">
            {{ $t('views.SettingsTriggerEventLogView.apply') }}
          </Button>
          <button type="button"
            data-testid="settings-trigger-event-log-reset"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetFilters"
          >
            {{ $t('views.SettingsTriggerEventLogView.reset') }}
          </button>
        </div>
      </div>
      <div v-if="total > 0" class="mt-3 text-sm text-muted-foreground">
        {{ $t('views.SettingsTriggerEventLogView.events_count', { count: total }, total) }}
      </div>
    </div>

    <!-- FAR-1344 — capacity outcomes at a glance. A count + trend for the
         deliveries capacity refused/coalesced in the last 24h, so an operator
         sees pressure building without drilling into an individual event. -->
    <div
      v-if="resultCounts"
      data-testid="capacity-outcomes-summary"
      class="rounded-lg border bg-card p-4 shadow-sm"
    >
      <div class="flex flex-wrap items-center gap-3">
        <span class="text-sm font-medium text-foreground">
          {{ $t('views.SettingsTriggerEventLogView.capacity_outcomes') }}
        </span>
        <span class="text-xs text-muted-foreground">
          {{ $t('views.SettingsTriggerEventLogView.capacity_outcomes_window', { hours: resultCounts.window_hours }) }}
        </span>
        <span
          data-testid="capacity-outcomes-total"
          :class="capacityTotalBadgeClass"
        >
          {{ capacityTotalLabel }}
        </span>
        <span
          v-if="resultCounts.total_recent > 0 || resultCounts.total_previous > 0"
          data-testid="capacity-outcomes-trend"
          class="text-xs text-muted-foreground"
        >
          {{ capacityTrendLabel }}
        </span>
      </div>
      <div v-if="capacityOutcomeRows.length > 0" class="mt-3 flex flex-wrap gap-2">
        <span
          v-for="row in capacityOutcomeRows"
          :key="row.key"
          :data-testid="`capacity-outcome-${row.key}`"
          :class="resultBadge(row.key)"
        >
          {{ $t(`views.SettingsTriggerEventLogView.${row.key}`) }}: {{ row.value }}
        </span>
      </div>
      <p v-else class="mt-3 text-xs text-muted-foreground">
        {{ $t('views.SettingsTriggerEventLogView.capacity_outcome_none', { hours: resultCounts.window_hours }) }}
      </p>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvents" />

    <div v-else-if="items.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.SettingsTriggerEventLogView.no_trigger_events_found') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">
        {{ $t('views.SettingsTriggerEventLogView.try_adjusting_filters') }}
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">{{ $t('views.SettingsTriggerEventLogView.type') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggerEventLogView.result') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggerEventLogView.timestamp') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsTriggerEventLogView.run') }}</th>
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
                  {{ shortId(event.run_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground" :title="event.error_detail ?? undefined">
                {{ event.error_detail || '—' }}
              </td>
              <td class="px-4 py-3 font-mono text-xs text-muted-foreground/70">
                {{ shortId(event.trigger_id) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <button type="button"
          :disabled="cursorStack.length === 0"
          data-testid="settings-trigger-event-log-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPreviousPage()"
        >
          {{ $t('views.SettingsTriggerEventLogView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.SettingsTriggerEventLogView.showing_of_total_events', { count: items.length, total: total }) }}
        </span>
        <button type="button"
          :disabled="!nextCursor"
          data-testid="settings-trigger-event-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToNextPage()"
        >
          {{ $t('views.SettingsTriggerEventLogView.next') }}
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { shortId } from '../utils/format'
import Button from 'primevue/button'
import Select from '../components/shared/AppSelect.vue'

const cursorStack = ref<(string | null)[]>([])
const currentCursor = ref<string | null>(null)
const nextCursor = ref<string | null>(null)

const { loading, error, data: responseData, load: loadEvents } = useDataFetch(
  async () => {
    const params: Record<string, unknown> = { limit: 50 }
    if (currentCursor.value) params.cursor = currentCursor.value
    if (filterTriggerType.value !== '__all__') params.trigger_type = filterTriggerType.value
    if (filterResult.value !== '__all__') params.validation_result = filterResult.value
    const res = await api.GET('/api/v1/admin/trigger-events', {
      params: { query: params as any },
    })
    if (res.data) {
      nextCursor.value = res.data.next_cursor ?? null
    }
    return res
  },
)

const items = computed(() => responseData.value?.items ?? [])
const total = computed(() => responseData.value?.total ?? 0)

// FAR-1344 — the capacity-outcome summary. `result_counts` is optional on the
// response (older backends), so the whole block is hidden when absent.
type ResultCounts = {
  window_hours: number
  recent: Record<string, number>
  previous: Record<string, number>
  total_recent: number
  total_previous: number
  direction: string
}
const resultCounts = computed<ResultCounts | null>(
  () => (responseData.value?.result_counts as ResultCounts | undefined) ?? null,
)

const CAPACITY_OUTCOME_KEYS = ['backpressure_skipped', 'concurrency_limit_reached', 'coalesced'] as const

const { t } = useI18n()

const capacityOutcomeRows = computed(() => {
  const counts = resultCounts.value?.recent ?? {}
  return CAPACITY_OUTCOME_KEYS.filter((key) => (counts[key] ?? 0) > 0).map((key) => ({
    key,
    value: counts[key] ?? 0,
  }))
})

const capacityTotalLabel = computed(() =>
  t('views.SettingsTriggerEventLogView.capacity_outcome_total', { count: resultCounts.value?.total_recent ?? 0 }),
)

const capacityTotalBadgeClass = computed(() => {
  const totalRecent = resultCounts.value?.total_recent ?? 0
  if (totalRecent === 0) return 'badge badge-context-slate'
  return 'badge badge-context-orange'
})

const capacityTrendLabel = computed(() => {
  const hours = resultCounts.value?.window_hours ?? 24
  const direction = resultCounts.value?.direction ?? 'flat'
  if (direction === 'up') return t('views.SettingsTriggerEventLogView.capacity_outcome_trend_up', { hours })
  if (direction === 'down') return t('views.SettingsTriggerEventLogView.capacity_outcome_trend_down', { hours })
  return t('views.SettingsTriggerEventLogView.capacity_outcome_trend_flat', { hours })
})

const filterTriggerType = ref('__all__')
const filterResult = ref('__all__')

function formatTimestamp(ts: string | null | undefined): string {
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
  if (type === 'ongoing') return 'badge badge-context-emerald'
  return 'badge badge-context-slate'
}

function resultBadge(result: string): string {
  if (result === 'accepted' || result === 'passed' || result === 'condition_met' || result === 'signal_fired') return 'badge badge-status-success'
  if (result === 'no_match') return 'badge badge-context-slate'
  if (result === 'hmac_failed' || result === 'schema_validation_failed' || result === 'validation_failed') return 'badge badge-status-destructive'
  // FAR-1344 — distinct badges for the capacity outcomes, so a refusal is
  // legible at a glance rather than sharing the generic throttle colour:
  // backpressure is an overload refusal (rose), the concurrency limit is a cap
  // (orange), and coalescing is benign (cyan).
  if (result === 'backpressure_skipped') return 'badge badge-context-rose'
  if (result === 'concurrency_limit_reached') return 'badge badge-context-orange'
  if (result === 'coalesced') return 'badge badge-context-cyan'
  if (result === 'spend_limit_reached') return 'badge badge-context-purple'
  if (result === 'deduplicated' || result === 'flood_rejected' || result === 'rate_limited') return 'badge badge-context-orange'
  if (result === 'paused') return 'badge badge-context-amber'
  if (result === 'timestamp_expired') return 'badge badge-context-slate'
  if (result === 'poll_error') return 'badge badge-context-rose'
  return 'badge badge-context-slate'
}

function goToPreviousPage() {
  const prev = cursorStack.value.pop()
  if (prev === undefined) return
  currentCursor.value = prev
  loadEvents()
}

function goToNextPage() {
  if (!nextCursor.value) return
  cursorStack.value.push(currentCursor.value)
  currentCursor.value = nextCursor.value
  loadEvents()
}

function applyFilters() {
  currentCursor.value = null
  cursorStack.value = []
  loadEvents()
}

function resetFilters() {
  filterTriggerType.value = '__all__'
  filterResult.value = '__all__'
  currentCursor.value = null
  cursorStack.value = []
  loadEvents()
}
</script>
