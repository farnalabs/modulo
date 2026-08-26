<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.SettingsTriggerEventLogView.trigger_event_log')" :subtitle="$t('views.SettingsTriggerEventLogView.event_history_for_all_triggers_across_the_organisation')" />

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label for="settingstriggereventlogview-trigger-type" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.SettingsTriggerEventLogView.trigger_type') }}</label>
          <Select
  aria-label="Trigger type"
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
  aria-label="Result"
  v-model="filterResult"
  :placeholder="$t('views.SettingsTriggerEventLogView.result')"
  data-testid="settings-trigger-event-log-result"
  id="settingstriggereventlogview-result"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.SettingsTriggerEventLogView.all_results') }, { value: 'accepted', label: $t('views.SettingsTriggerEventLogView.accepted') }, { value: 'passed', label: $t('views.SettingsTriggerEventLogView.passed') }, { value: 'condition_met', label: $t('views.SettingsTriggerEventLogView.condition_met') }, { value: 'signal_fired', label: $t('views.SettingsTriggerEventLogView.signal_fired') }, { value: 'no_match', label: $t('views.SettingsTriggerEventLogView.no_match') }, { value: 'hmac_failed', label: $t('views.SettingsTriggerEventLogView.hmac_failed') }, { value: 'schema_validation_failed', label: $t('views.SettingsTriggerEventLogView.schema_validation_failed') }, { value: 'deduplicated', label: $t('views.SettingsTriggerEventLogView.deduplicated') }, { value: 'concurrency_limit_reached', label: $t('views.SettingsTriggerEventLogView.concurrency_limit_reached') }, { value: 'flood_rejected', label: $t('views.SettingsTriggerEventLogView.flood_rejected') }, { value: 'timestamp_expired', label: $t('views.SettingsTriggerEventLogView.timestamp_expired') }, { value: 'validation_failed', label: $t('views.SettingsTriggerEventLogView.validation_failed') }, { value: 'rate_limited', label: $t('views.SettingsTriggerEventLogView.rate_limited') }, { value: 'poll_error', label: $t('views.SettingsTriggerEventLogView.poll_error') }]"
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
            Apply
          </Button>
          <button type="button"
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
          Previous
        </button>
        <span class="text-sm text-muted-foreground">
          {{ items.length }} of {{ total }} events
        </span>
        <button type="button"
          :disabled="!nextCursor"
          data-testid="settings-trigger-event-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToNextPage()"
        >
          Next
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { shortId } from '../utils/format'
import Button from 'primevue/button'
import Select from 'primevue/select'

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
  if (result === 'deduplicated' || result === 'concurrency_limit_reached' || result === 'flood_rejected' || result === 'rate_limited') return 'badge badge-context-orange'
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
