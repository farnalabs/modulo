<template>
  <FeatureGate feature-name="webhook_notification_log" required-tier="community" show-disabled>
    <div class="page-wide">
    <PageHeader :title="$t('views.AdminNotificationDeliveryLogView.notification_delivery_log')" :subtitle="$t('views.AdminNotificationDeliveryLogView.admin_view_of_all_webhook_notification_deliveries')" data-test-id="admin-notification-log-title" />

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label for="adminnotificationdeliverylogview-field-4" class="mb-1 block text-xs font-medium text-muted-foreground capitalize">{{ $t('views.AdminNotificationDeliveryLogView.status') }}</label>
          <Select
  :aria-label="$t('views.AdminNotificationDeliveryLogView.status')"
  v-model="filterStatus"
  :placeholder="$t('views.AdminNotificationDeliveryLogView.status')"
  data-testid="admin-notification-log-status"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminErrorsView.all_statuses') }, { value: 'delivered', label: $t('views.AdminNotificationDeliveryLogView.delivered') }, { value: 'failed', label: $t('views.AdminNotificationDeliveryLogView.failed') }, { value: 'dead_lettered', label: $t('views.AdminNotificationDeliveryLogView.dead_lettered') }, { value: 'pending', label: $t('views.AdminNotificationDeliveryLogView.pending') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div>
          <label for="adminnotificationdeliverylogview-field-3" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.event_type') }}</label>
          <Select
  :aria-label="$t('views.AdminAuditView.event_type')"
  v-model="filterEventType"
  :placeholder="$t('views.AdminAuditView.event_type')"
  data-testid="admin-notification-log-event-type"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminNotificationDeliveryLogView.all_types') }, { value: 'hitl_awaiting', label: $t('views.AdminNotificationDeliveryLogView.hitl_awaiting') }, { value: 'run_failed', label: $t('views.AdminNotificationDeliveryLogView.run_failed') }, { value: 'claim_expired', label: $t('views.AdminNotificationDeliveryLogView.claim_expired') }, { value: 'hitl_overdue', label: $t('views.AdminNotificationDeliveryLogView.hitl_overdue') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
        <div>
          <label for="adminnotificationdeliverylogview-field-2" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.from') }}</label>
          <input id="adminnotificationdeliverylogview-field-2"
            v-model="filterDateFrom"
            type="date"
            data-testid="admin-notification-log-date-from"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div>
          <label for="adminnotificationdeliverylogview-field-1" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.to') }}</label>
          <input id="adminnotificationdeliverylogview-field-1"
            v-model="filterDateTo"
            type="date"
            data-testid="admin-notification-log-date-to"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
        <div class="flex items-end gap-2">
          <Button data-testid="admin-notification-log-apply" @click="applyFilters">
            {{ $t('views.AdminNotificationDeliveryLogView.apply') }}
          </Button>
          <button
            type="button"
            data-testid="admin-notification-log-reset"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="resetFilters"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.reset') }}
          </button>
          <button
            v-if="hasRetryableItems && !retryAllConfirm"
            type="button"
            :disabled="retryingAll"
            data-testid="admin-notification-log-retry-all"
            class="rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-800 hover:bg-amber-100 disabled:opacity-40 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-300"
            @click="retryAllConfirm = true"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.retry_all_failed') }}
          </button>
          <div v-if="retryAllConfirm" class="flex items-center gap-2">
            <span class="text-sm text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.retry_all_confirm') }}</span>
            <button
              type="button"
              :disabled="retryingAll"
              data-testid="admin-notification-log-retry-all-confirm"
              class="rounded-lg bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 disabled:opacity-50"
              @click="retryAllFailed"
            >
              {{ retryingAll ? $t('views.AdminNotificationDeliveryLogView.retrying_all') : $t('views.AdminNotificationDeliveryLogView.confirm') }}
            </button>
            <button
              type="button"
              data-testid="admin-notification-log-retry-all-cancel"
              class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
              @click="retryAllConfirm = false"
            >
              {{ $t('views.AdminNotificationDeliveryLogView.cancel') }}
            </button>
          </div>
        </div>
      </div>
      <div v-if="total > 0" class="mt-3 text-sm text-muted-foreground">
        {{ $t('views.AdminNotificationDeliveryLogView.deliveries_count', { count: total }, total) }}
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

    <EmptyState
      v-else-if="items.length === 0"
      data-testid="admin-notification-log-empty"
      :title="$t('views.AdminNotificationDeliveryLogView.no_delivery_logs_found')"
      :description="$t('views.AdminNotificationDeliveryLogView.try_adjusting_filters')"
    />

    <template v-else>
      <div class="table-wrapper">
        <table class="w-full">
          <thead>
            <tr>
              <th class="w-8 table-header"></th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.timestamp') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.destination') }}</th>
              <th class="table-header capitalize">{{ $t('views.AdminNotificationDeliveryLogView.status') }}</th>
              <th class="table-header table-cell-numeric">{{ $t('views.AdminNotificationDeliveryLogView.attempts') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.error') }}</th>
              <th class="table-header">{{ $t('views.AdminNotificationDeliveryLogView.actions') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="entry in items"
              :key="entry.id"
              class="transition-colors hover:bg-muted/30 cursor-pointer"
              @click="toggleRow(entry.id)"
            >
              <td class="table-cell text-muted-foreground">
                <button
                  type="button"
                  class="inline-flex items-center rounded p-1 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :aria-label="$t('views.AdminNotificationDeliveryLogView.expand_delivery', { id: entry.id })"
                  :data-testid="'admin-notification-log-expand-' + entry.id"
                  @click.stop="toggleRow(entry.id)"
                >
                  <ChevronRight
                    :class="expandedId === entry.id ? 'rotate-90' : ''"
                    class="h-3.5 w-3.5 transition-transform"
                    aria-hidden="true"
                  />
                </button>
              </td>
              <td class="table-cell whitespace-nowrap text-muted-foreground">
                {{ formatTimestamp(entry.created_at) }}
              </td>
              <td class="table-cell font-medium">{{ entry.event_type }}</td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" :title="entry.endpoint_url ?? undefined">
                {{ entry.endpoint_url || '—' }}
              </td>
              <td class="table-cell">
                <span :class="statusBadge(entry.status)" class="capitalize">
                  {{ entry.status }}
                </span>
              </td>
              <td class="table-cell table-cell-numeric text-muted-foreground">{{ entry.attempt_count }}</td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" :title="entry.last_error ?? undefined">
                {{ entry.last_error || '—' }}
              </td>
              <td class="table-cell">
                <div v-if="retryMessages[entry.id]" class="text-xs" :class="retryMessages[entry.id].type === 'error' ? 'text-destructive' : 'text-success'">
                  {{ retryMessages[entry.id].text }}
                </div>
                <button
                  v-else-if="entry.status === 'failed' || entry.status === 'dead_lettered'"
                  type="button"
                  :disabled="retryingId === entry.id"
                  data-testid="admin-notification-log-retry"
                  class="rounded-md bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/20 disabled:opacity-40"
                  @click.stop="retryDelivery(entry)"
                >
                  {{ retryingId === entry.id ? $t('views.AdminNotificationDeliveryLogView.retrying') : $t('views.AdminNotificationDeliveryLogView.retry') }}
                </button>
              </td>
            </tr>
            <template v-if="expandedId">
              <tr v-for="entry in expandedEntries" :key="`exp-${entry.id}`">
              <td colspan="8" class="bg-muted/20 px-4 py-3">
                <div class="space-y-2 text-sm">
                  <div v-if="entry.response_body" class="rounded border bg-card p-3">
                    <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminNotificationDeliveryLogView.response_body') }}</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs">{{ entry.response_body }}</pre>
                  </div>
                  <div v-if="entry.last_error" class="rounded border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-950/30">
                    <span class="text-xs font-medium text-red-600 dark:text-red-400">{{ $t('views.AdminNotificationDeliveryLogView.error_details') }}</span>
                    <pre class="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all font-mono text-xs text-red-700 dark:text-red-300">{{ entry.last_error }}</pre>
                  </div>
                  <div v-if="entry.response_code" class="text-muted-foreground">
                    <span class="text-xs font-medium">{{ $t('views.AdminNotificationDeliveryLogView.http_response_code') }}</span>
                    <code class="ml-1 font-mono text-xs">{{ entry.response_code }}</code>
                  </div>
                  <div v-if="!entry.response_body && !entry.last_error && !entry.response_code" class="text-xs text-muted-foreground italic">
                    {{ $t('views.AdminNotificationDeliveryLogView.no_additional_details') }}
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
          type="button"
          :disabled="!prevCursor"
          data-testid="admin-notification-log-previous"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(prevCursor)"
        >
          {{ $t('views.AdminNotificationDeliveryLogView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.AdminNotificationDeliveryLogView.of_deliveries', { count: items.length, total }) }}
        </span>
        <button
          type="button"
          :disabled="!nextCursor"
          data-testid="admin-notification-log-next"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="goToPage(nextCursor)"
        >
          {{ $t('views.AdminNotificationDeliveryLogView.next') }}
        </button>
      </div>

      <div v-if="deadLetteredCount > 0" data-testid="admin-notification-log-dlq" class="rounded-lg border bg-card p-4 shadow-sm">
        <div class="flex items-center justify-between">
          <div>
            <h3 class="text-base font-semibold">{{ $t('views.AdminNotificationDeliveryLogView.dead_letter_queue') }}</h3>
            <p class="text-sm text-muted-foreground">
              {{ $t('views.AdminNotificationDeliveryLogView.dead_letter_queue_description', { count: deadLetteredCount }, deadLetteredCount) }}
            </p>
          </div>
          <button
            type="button"
            data-testid="admin-notification-log-dlq-filter"
            class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent"
            @click="showDeadLettered"
          >
            {{ $t('views.AdminNotificationDeliveryLogView.view_dead_lettered') }}
          </button>
        </div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { ref, computed } from 'vue'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import { formatApiError } from '../lib/api/formatError'
import type { components, paths } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import { ChevronRight } from '@lucide/vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

type DeliveryLogEntry = components['schemas']['DeliveryLogEntry']
interface DeliveryLogPage {
  items: DeliveryLogEntry[]
  total: number
  next_cursor: string | null
  prev_cursor?: string | null
}
type DeliveryLogQuery = NonNullable<paths['/api/v1/admin/notifications/deliveries']['get']>['parameters']['query']

const cursor = ref<string | null>(null)
const filterStatus = ref('__all__')
const filterEventType = ref('__all__')
const filterDateFrom = ref('')
const filterDateTo = ref('')

const { data: deliveriesData, loading, error, load: loadDeliveries } = useDataFetch<DeliveryLogPage>(
  async () => {
    const params: DeliveryLogQuery = { limit: 50 }
    if (cursor.value) params.cursor = cursor.value
    if (filterStatus.value !== '__all__') params.status = filterStatus.value
    if (filterEventType.value !== '__all__') params.event_type = filterEventType.value
    if (filterDateFrom.value) params.from = filterDateFrom.value
    if (filterDateTo.value) params.to = filterDateTo.value
    const response = await api.GET('/api/v1/admin/notifications/deliveries', { params: { query: params } })
    return { data: response.data as unknown as DeliveryLogPage | undefined, error: response.error }
  },
  { initialValue: { items: [] as DeliveryLogEntry[], total: 0, next_cursor: null as string | null, prev_cursor: null as string | null } }
)

const items = computed(() => deliveriesData.value?.items ?? [])
const total = computed(() => deliveriesData.value?.total ?? 0)
const nextCursor = computed(() => deliveriesData.value?.next_cursor ?? null)
const prevCursor = computed(() => {
  const dc = deliveriesData.value
  if (!dc?.prev_cursor) return null
  return dc.prev_cursor
})

const expandedId = ref<string | null>(null)
const retryingId = ref<string | null>(null)
const retryingAll = ref(false)
const retryAllConfirm = ref(false)
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

function goToPage(c: string | null) {
  if (!c) return
  cursor.value = c
  retrySuccessMessage.value = null
  loadDeliveries()
}

function applyFilters() {
  cursor.value = null
  retrySuccessMessage.value = null
  loadDeliveries()
}

function resetFilters() {
  filterStatus.value = '__all__'
  filterEventType.value = '__all__'
  filterDateFrom.value = ''
  filterDateTo.value = ''
  cursor.value = null
  retrySuccessMessage.value = null
  loadDeliveries()
}

function showDeadLettered() {
  filterStatus.value = 'dead_lettered'
  filterEventType.value = '__all__'
  filterDateFrom.value = ''
  filterDateTo.value = ''
  cursor.value = null
  retrySuccessMessage.value = null
  loadDeliveries()
}

async function retryDelivery(entry: DeliveryLogEntry) {
  if (!entry.endpoint_id) {
    retryMessages.value[entry.id] = { type: 'error', text: t('views.AdminNotificationDeliveryLogView.cannot_retry_missing_endpoint_id') }
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
      retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_failed')} ${formatApiError(err)}` }
    } else if (data) {
      if (data.success) {
        await loadDeliveries()
        retryMessages.value[entry.id] = { type: 'success', text: t('views.AdminNotificationDeliveryLogView.retry_succeeded') }
      } else {
        await loadDeliveries()
        retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_failed')} ${data.error || `HTTP ${data.status_code}`}` }
      }
    }
  } catch (e: unknown) {
    retryMessages.value[entry.id] = { type: 'error', text: `${t('views.AdminNotificationDeliveryLogView.retry_request_failed')} ${formatApiError(e)}` }
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
      error.value = `${t('views.AdminNotificationDeliveryLogView.retry_all_failed_1')} ${formatApiError(err)}`
    } else if (data) {
      await loadDeliveries()
      const result = data as unknown as { retried: number; success: boolean; errors?: unknown[] }
      const retried = t('views.AdminNotificationDeliveryLogView.retried_deliveries_count', { count: result.retried }, result.retried)
      retrySuccessMessage.value = result.success
        ? retried
        : t('views.AdminNotificationDeliveryLogView.retried_with_errors', {
            retried,
            errors: t('views.AdminNotificationDeliveryLogView.errors_count', { count: result.errors?.length || 0 }, result.errors?.length || 0),
          })
    }
  } catch (e: unknown) {
    error.value = `${t('views.AdminNotificationDeliveryLogView.retry_all_request_failed')} ${formatApiError(e)}`
  } finally {
    retryingAll.value = false
    retryAllConfirm.value = false
  }
}

/* onMounted handled by useDataFetch */
</script>
