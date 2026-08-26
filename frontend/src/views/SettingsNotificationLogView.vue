<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.AdminNotificationDeliveryLogView.notification_delivery_log')" :subtitle="$t('views.SettingsNotificationLogView.delivery_history_for_all_webhook_notifications')" />

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label for="settingsnotificationlogview-status" class="mb-1 block text-xs font-medium text-muted-foreground capitalize">{{ $t('views.SettingsNotificationLogView.status') }}</label>
          <Select
  aria-label="Status"
  v-model="filterStatus"
  :placeholder="$t('views.SettingsNotificationLogView.status')"
  data-testid="settings-notification-log-status"
  id="settingsnotificationlogview-status"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminErrorsView.all_statuses') }, { value: 'delivered', label: $t('views.SettingsNotificationLogView.delivered') }, { value: 'failed', label: $t('views.SettingsNotificationLogView.failed') }, { value: 'dead_lettered', label: $t('views.AdminNotificationDeliveryLogView.dead_lettered') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div>
          <label for="settingsnotificationlogview-field-2" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.SettingsNotificationLogView.from') }}</label>
          <input id="settingsnotificationlogview-field-2"
            v-model="filterDateFrom"
            type="date"
            data-testid="settings-notification-log-date-from"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label for="settingsnotificationlogview-field-1" class="mb-1 block text-xs font-medium text-muted-foreground">To</label>
          <input id="settingsnotificationlogview-field-1"
            v-model="filterDateTo"
            type="date"
            data-testid="settings-notification-log-date-to"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div class="flex items-end gap-2">
          <Button data-testid="settings-notification-log-apply" @click="applyFilters">
            Apply
          </Button>
          <button type="button"
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
              <th class="px-4 py-3">{{ $t('views.SettingsNotificationLogView.destination') }}</th>
              <th class="px-4 py-3 capitalize">{{ $t('views.SettingsNotificationLogView.status') }}</th>
              <th class="px-4 py-3">{{ $t('views.SettingsNotificationLogView.attempts') }}</th>
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
                <span :class="statusBadge(entry.status)" class="capitalize">
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
        <button type="button"
          :disabled="cursorStack.length === 0"
          data-testid="settings-notification-log-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPreviousPage()"
        >
          Previous
        </button>
        <span class="text-sm text-muted-foreground">
          {{ items.length }} of {{ total }} deliveries
        </span>
        <button type="button"
          :disabled="!nextCursor"
          data-testid="settings-notification-log-next"
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
import Button from 'primevue/button'
import Select from 'primevue/select'

const cursorStack = ref<(string | null)[]>([])
const currentCursor = ref<string | null>(null)
const nextCursor = ref<string | null>(null)

const { loading, error, data: responseData, load: loadDeliveries } = useDataFetch(
  async () => {
    const params: Record<string, unknown> = { limit: 50 }
    if (currentCursor.value) params.cursor = currentCursor.value
    if (filterStatus.value !== '__all__') params.status = filterStatus.value
    if (filterDateFrom.value) params.from = filterDateFrom.value
    if (filterDateTo.value) params.to = filterDateTo.value
    const res = await api.GET('/api/v1/admin/notifications/deliveries', {
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

const filterStatus = ref('__all__')
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

function goToPreviousPage() {
  const prev = cursorStack.value.pop()
  if (prev === undefined) return
  currentCursor.value = prev
  loadDeliveries()
}

function goToNextPage() {
  if (!nextCursor.value) return
  cursorStack.value.push(currentCursor.value)
  currentCursor.value = nextCursor.value
  loadDeliveries()
}

function applyFilters() {
  currentCursor.value = null
  cursorStack.value = []
  loadDeliveries()
}

function resetFilters() {
  filterStatus.value = '__all__'
  filterDateFrom.value = ''
  filterDateTo.value = ''
  currentCursor.value = null
  cursorStack.value = []
  loadDeliveries()
}
</script>
