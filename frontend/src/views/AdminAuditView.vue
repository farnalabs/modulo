<template>
  <FeatureGate feature-name="audit_viewer" required-tier="team" show-disabled>

    <div class="page-wide">
    <header class="flex items-center justify-between">
      <PageHeader :title="$t('views.AdminAuditView.audit_log')" :subtitle="$t('views.AdminAuditView.tamper_evident_event_trail')" />
      <div class="flex items-center gap-2">
        <button
          type="button"
          :disabled="verifying"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-verify-chain"
          @click="verifyChain"
        >
          {{ verifying ? $t('views.AdminAuditView.verifying') : $t('views.AdminAuditView.verify_chain') }}
        </button>
        <button
          type="button"
          :disabled="exporting"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-export-csv"
          @click="exportCsv"
        >
          {{ exporting ? $t('views.AdminAuditView.exporting') : $t('views.AdminAuditView.export_csv') }}
        </button>
        <button
          type="button"
          :disabled="exportingJsonl"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-export-jsonl"
          @click="exportJsonl"
        >
          {{ exportingJsonl ? $t('views.AdminAuditView.exporting') : $t('views.AdminAuditView.export_jsonl') }}
        </button>
      </div>
    </header>
    <div v-if="chainResult" class="rounded-lg border px-4 py-3 text-sm" :class="chainResult.valid ? 'border-green-500 bg-green-50 text-green-800' : 'border-red-500 bg-red-50 text-red-800'" data-testid="admin-audit-chain-result">
      <strong>{{ chainResult.valid ? $t('views.AdminAuditView.chain_valid') : $t('views.AdminAuditView.chain_broken') }}</strong>
      <span v-if="chainResult.event_count" class="ml-2">— {{ $t('views.AdminAuditView.events_verified', { count: chainResult.event_count }) }}</span>
      <span v-if="chainResult.error" class="ml-2">— {{ chainResult.error }}</span>
    </div>

    <div class="card p-4">
      <FilterBar
        :filters="[
          { key: 'event_type', label: $t('views.AdminAuditView.event_type'), options: [
            { value: 'pipeline.created', label: $t('views.AdminAuditView.opt_pipeline_created') },
            { value: 'pipeline.updated', label: $t('views.AdminAuditView.opt_pipeline_updated') },
            { value: 'pipeline.deleted', label: $t('views.AdminAuditView.opt_pipeline_deleted') },
            { value: 'run.started', label: $t('views.AdminAuditView.opt_run_started') },
            { value: 'run.completed', label: $t('views.AdminAuditView.opt_run_completed') },
            { value: 'run.failed', label: $t('views.AdminAuditView.opt_run_failed') },
            { value: 'run.cancelled', label: $t('views.AdminAuditView.opt_run_cancelled') },
            { value: 'user.created', label: $t('views.AdminAuditView.opt_user_created') },
            { value: 'user.updated', label: $t('views.AdminAuditView.opt_user_updated') },
            { value: 'user.deactivated', label: $t('views.AdminAuditView.opt_user_deactivated') },
            { value: 'user.activated', label: $t('views.AdminAuditView.opt_user_activated') },
            { value: 'team.created', label: $t('views.AdminAuditView.opt_team_created') },
            { value: 'team.updated', label: $t('views.AdminAuditView.opt_team_updated') },
            { value: 'team.deleted', label: $t('views.AdminAuditView.opt_team_deleted') },
            { value: 'schema.created', label: $t('views.AdminAuditView.opt_schema_created') },
            { value: 'schema.updated', label: $t('views.AdminAuditView.opt_schema_updated') },
            { value: 'schema.deleted', label: $t('views.AdminAuditView.opt_schema_deleted') },
            { value: 'connector.created', label: $t('views.AdminAuditView.opt_connector_created') },
            { value: 'connector.updated', label: $t('views.AdminAuditView.opt_connector_updated') },
            { value: 'connector.deleted', label: $t('views.AdminAuditView.opt_connector_deleted') },
            { value: 'model_backend.created', label: $t('views.AdminAuditView.opt_model_backend_created') },
            { value: 'model_backend.updated', label: $t('views.AdminAuditView.opt_model_backend_updated') },
            { value: 'model_backend.deleted', label: $t('views.AdminAuditView.opt_model_backend_deleted') },
            { value: 'sso_provider.created', label: $t('views.AdminAuditView.opt_sso_provider_created') },
            { value: 'sso_provider.updated', label: $t('views.AdminAuditView.opt_sso_provider_updated') },
            { value: 'sso_provider.deleted', label: $t('views.AdminAuditView.opt_sso_provider_deleted') },
            { value: 'sso_provider.toggled', label: $t('views.AdminAuditView.opt_sso_provider_toggled') },
            { value: 'settings.updated', label: $t('views.AdminAuditView.opt_settings_updated') },
            { value: 'api_key.created', label: $t('views.AdminAuditView.opt_api_key_created') },
            { value: 'api_key.deleted', label: $t('views.AdminAuditView.opt_api_key_deleted') },
            { value: 'export.csv', label: $t('views.AdminAuditView.opt_export_csv') },
          ]},
        ]"
        :filter-values="{ event_type: filterEventType }"
        @update:filter="(key, value) => { if (key === 'event_type') filterEventType = value }"
      >
        <template #after>
          <input
            v-model="filterActor"
            type="text"
            :aria-label="$t('views.AdminAuditView.actor_id')"
            :placeholder="$t('views.AdminAuditView.actor_id')"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-actor"
          />
          <input aria-label="date"
            v-model="filterDateFrom"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-from"
          />
        </template>
        <div>
          <label for="adminauditview-field-2" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.to') }}</label>
          <input id="adminauditview-field-2"
            v-model="filterDateTo"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-to"
          />
        </div>
        <div>
          <label for="adminauditview-field-1" class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.target_type') }}</label>
          <Select
  aria-label="Target Type"
  v-model="filterTargetType"
  :placeholder="$t('views.AdminAuditView.target_type')"
  data-testid="admin-audit-target-type"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.AdminAuditView.all_targets') }, { value: 'pipeline', label: $t('views.AdminAuditView.optgroup_pipeline') }, { value: 'run', label: $t('views.AdminAuditView.optgroup_run') }, { value: 'user', label: $t('views.AdminAuditView.optgroup_user') }, { value: 'team', label: $t('views.AdminAuditView.optgroup_team') }, { value: 'schema', label: $t('views.AdminAuditView.optgroup_schema') }, { value: 'connector', label: $t('views.AdminAuditView.optgroup_connector') }, { value: 'model_backend', label: $t('views.AdminAuditView.optgroup_model_backend') }, { value: 'sso_provider', label: $t('views.AdminAuditView.optgroup_sso_provider') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>
      </FilterBar>
      <div class="mt-3 flex items-center gap-2">
        <Button data-testid="admin-audit-apply-filters" @click="applyFilters">
          {{ $t('views.AdminAuditView.apply_filters') }}
        </Button>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          data-testid="admin-audit-reset"
          @click="resetFilters"
        >
          {{ $t('views.AdminAuditView.reset') }}
        </button>
        <span v-if="total > 0" class="ml-auto text-sm text-muted-foreground">
          {{ $t('views.AdminAuditView.events_count', { count: total }, total) }}
        </span>
      </div>
    </div>

    <div v-if="loading" aria-hidden="true" class="table-wrapper overflow-x-auto">
      <table class="w-full">
        <thead>
          <tr>
            <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.target') }}</th>
            <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
            <th class="w-8 table-header" />
          </tr>
        </thead>
        <tbody class="divide-y">
          <tr v-for="row in 8" :key="row">
            <td class="table-cell whitespace-nowrap"><div class="h-4 w-28 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-24 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-32 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-20 rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="h-4 w-full max-w-sm rounded bg-muted/50" /></td>
            <td class="table-cell"><div class="ml-auto h-4 w-4 rounded bg-muted/50" /></td>
          </tr>
        </tbody>
      </table>
    </div>

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvents" />

    <EmptyState
      v-else-if="events.length === 0"
      :title="$t('views.AdminAuditView.no_audit_events_found')"
      :description="$t('views.AdminAuditView.try_adjusting_filters')"
    />

    <template v-else>
      <div class="table-wrapper overflow-x-auto">
        <table class="w-full">
          <thead>
            <tr>
              <th class="table-header">{{ $t('views.AdminAuditView.timestamp') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.actor') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.target') }}</th>
              <th class="table-header">{{ $t('views.AdminAuditView.summary') }}</th>
              <th class="w-8 table-header" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in events"
              :key="event.id"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              :data-testid="'admin-audit-event-row-' + event.id"
              @click="toggleExpand(event.id)"
            >
              <td class="table-cell whitespace-nowrap">
                {{ formatTimestamp(event.created_at) }}
              </td>
              <td class="table-cell">
                <span :class="badgeClass(event.event_type)">
                  {{ event.event_type }}
                </span>
              </td>
              <td class="table-cell font-mono">
                {{ formatActor(event.actor_user_id) }}
              </td>
              <td class="table-cell">
                <span v-if="event.resource_type" class="text-muted-foreground">
                  {{ event.resource_type }}
                </span>
                <span v-if="event.resource_id" class="ml-1 font-mono text-xs text-muted-foreground/70">
                  / {{ shortId(event.resource_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <td class="table-cell max-w-xs truncate text-muted-foreground" v-tooltip.top="{ value: summarize(event), showDelay: 300 }">{{ summarize(event) }}</td>
              <td class="table-cell text-xs text-muted-foreground">
                <button
                  type="button"
                  class="inline-flex items-center rounded p-1 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :aria-label="'Expand event ' + event.id"
                  :data-testid="'admin-audit-event-expand-' + event.id"
                  @click.stop="toggleExpand(event.id)"
                >
                  <svg
                    class="h-4 w-4 transition-transform"
                    :class="{ 'rotate-180': expandedId === event.id }"
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    aria-hidden="true"
                  >
                    <path d="m6 9 6 6 6-6" />
                  </svg>
                </button>
              </td>
            </tr>
            <tr v-if="expandedId">
              <td colspan="6" class="border-t bg-muted p-4">
                <div class="space-y-3">
                  <div v-if="expandedEvent?.payload_json && Object.keys(expandedEvent.payload_json).length > 0">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.details') }}</h4>
                    <JsonViewer :data="expandedEvent?.payload_json ?? null" :show-toolbar="true" :max-height="'20rem'" />
                  </div>
                  <div v-if="expandedEvent?.previous_hash" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.previous_hash') }}</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono" v-tooltip.top="expandedEvent.previous_hash">{{ shortId(expandedEvent.previous_hash) }}</code>
                    </div>
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.event_id') }}</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono" v-tooltip.top="expandedEvent.id">{{ shortId(expandedEvent.id) }}</code>
                    </div>
                  </div>
                  <div v-if="expandedEvent?.request_id">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.request_id') }}</h4>
                    <code class="rounded bg-background px-2 py-1 text-xs font-mono">{{ shortId(expandedEvent.request_id) }}</code>
                  </div>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex items-center justify-between">
        <button
          type="button"
          :disabled="!prevCursor"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          data-testid="admin-audit-previous"
          @click="() => goToPage(prevCursor)"
        >
          {{ $t('views.AdminAuditView.previous') }}
        </button>
        <span class="text-sm text-muted-foreground">
          {{ $t('views.AdminAuditView.page_of_total', { page: currentPage, count: events.length, total: total }) }}
        </span>
        <button
          type="button"
          :disabled="!nextCursor"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          data-testid="admin-audit-next"
          @click="() => goToPage(nextCursor)"
        >
          {{ $t('views.AdminAuditView.next') }}
        </button>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { formatError } from '../lib/utils'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { formatApiError } from '../lib/api/formatError'
import Button from 'primevue/button'
import { formatDateFilename } from '../lib/formatDate'
import { shortId } from '../utils/format'
import Select from 'primevue/select'

const { t } = useI18n()

const planStore = usePlanStore()

interface AuditEvent {
  id: string
  event_type: string
  actor_user_id: string | null
  created_at: string | null
  resource_type: string | null
  resource_id: string | null
  payload_json: Record<string, unknown> | null
  request_id: string | null
  previous_hash: string | null
}
interface AuditPage {
  items: AuditEvent[]
  total: number
  next_cursor: string | null
  prev_cursor: string | null
}

const cursor = ref<string | null>(null)
const currentPage = ref(1)

const { data: auditData, loading, error, load: loadEvents } = useDataFetch(
  () => api.GET('/api/v1/admin/audit', { params: { query: buildQuery() as any } }),
  { initialValue: { items: [] as AuditEvent[], total: 0, next_cursor: null as string | null, prev_cursor: null as string | null } }
)

const auditPage = computed(() => auditData.value as unknown as AuditPage)
const events = computed(() => auditPage.value.items ?? [])
const total = computed(() => auditPage.value.total ?? 0)
const nextCursor = computed(() => auditPage.value.next_cursor ?? null)
const prevCursor = computed(() => auditPage.value.prev_cursor ?? null)

const filterEventType = ref('')
const filterActor = ref('')
const filterDateFrom = ref('')
const filterDateTo = ref('')
const filterTargetType = ref('__all__')

const expandedId = ref<string | null>(null)
const expandedEvent = ref<AuditEvent | null>(null)

const exporting = ref(false)
const exportingJsonl = ref(false)
const verifying = ref(false)
const chainResult = ref<{ valid: boolean; event_count?: number; error?: string } | null>(null)

function formatActor(actorId: string | null): string {
  if (!actorId) return '—'
  return 'usr_' + shortId(actorId).replace('#', '')
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

function badgeClass(eventType: string): string {
  if (eventType.startsWith('pipeline.')) return 'badge badge-context-blue'
  if (eventType.startsWith('run.completed')) return 'badge badge-status-success'
  if (eventType.startsWith('run.failed')) return 'badge badge-status-destructive'
  if (eventType.startsWith('run.')) return 'badge badge-status-warning'
  if (eventType.startsWith('user.')) return 'badge badge-context-purple'
  if (eventType.startsWith('team.')) return 'badge badge-context-indigo'
  if (eventType.startsWith('schema.')) return 'badge badge-context-cyan'
  if (eventType.startsWith('connector.')) return 'badge badge-context-orange'
  if (eventType.startsWith('model_backend.')) return 'badge badge-context-pink'
  if (eventType.startsWith('sso_provider.')) return 'badge badge-context-slate'
  if (eventType.startsWith('settings.')) return 'badge badge-context-slate'
  if (eventType.startsWith('api_key.')) return 'badge badge-context-rose'
  if (eventType.startsWith('export.')) return 'badge badge-context-blue'
  return 'badge badge-context-slate'
}

function summarize(event: AuditEvent): string {
  const et = event.event_type
  const action = et.includes('.') ? et.split('.')[1] : et
  const resource = event.resource_type ?? 'resource'

  const p = event.payload_json ?? {}
  const name = (p as Record<string, unknown>).name ?? (p as Record<string, unknown>).display_name ?? null

  const parts = [action.charAt(0).toUpperCase() + action.slice(1), resource]
  if (name) parts.push(`"${name}"`)
  return parts.join(' ')
}

function toggleExpand(id: string) {
  if (expandedId.value === id) {
    expandedId.value = null
    expandedEvent.value = null
    return
  }
  expandedId.value = id
  expandedEvent.value = events.value.find(e => e.id === id) ?? null
}

function buildQuery() {
  const q: Record<string, unknown> = { limit: 50 }
  if (cursor.value) q.cursor = cursor.value
  if (filterEventType.value) q.event_type = filterEventType.value
  if (filterActor.value) q.user_id = filterActor.value
  if (filterDateFrom.value) q.from_date = filterDateFrom.value
  if (filterDateTo.value) q.to_date = filterDateTo.value
  if (filterTargetType.value !== '__all__') q.entity_type = filterTargetType.value
  return q
}

function goToPage(c: string | null) {
  if (!c) return
  currentPage.value = prevCursor.value === c
    ? Math.max(1, currentPage.value - 1)
    : currentPage.value + 1
  cursor.value = c
  loadEvents()
}

function applyFilters() {
  currentPage.value = 1
  cursor.value = null
  loadEvents()
}

function resetFilters() {
  filterEventType.value = ''
  filterActor.value = ''
  filterDateFrom.value = ''
  filterDateTo.value = ''
  filterTargetType.value = '__all__'
  currentPage.value = 1
  cursor.value = null
  loadEvents()
}

async function exportCsv() {
  exporting.value = true
  try {
    const allEvents: AuditEvent[] = []
    let page = 1
    const pageSize = 1000
    let totalPages = 1

    while (page <= totalPages) {
      const { data, error: err } = await api.GET('/api/v1/admin/audit/export', {
        params: {
          query: {
            page,
            page_size: pageSize,
            event_type: filterEventType.value || undefined,
            user_id: filterActor.value || undefined,
            entity_type: filterTargetType.value || undefined,
            from_date: filterDateFrom.value || undefined,
            to_date: filterDateTo.value || undefined,
          } as any,
        },
      })
      if (err) {
        error.value = `${t('views.AdminAuditView.export_failed')} ${formatError(err)}`
        return
      }
      if (!data) break
      const exportPage = data as unknown as AuditPage
      allEvents.push(...exportPage.items)
      totalPages = Math.ceil(exportPage.total / pageSize)
      page++
    }

    const headers = ['Timestamp', 'Event Type', 'Actor ID', 'Target Type', 'Target ID', 'Summary', 'Request ID', 'Previous Hash']
    const rows = allEvents.map(e => [
      e.created_at ?? '',
      e.event_type,
      e.actor_user_id ?? '',
      e.resource_type ?? '',
      e.resource_id ?? '',
      summarize(e).replaceAll('"', '""'),
      e.request_id ?? '',
      e.previous_hash ?? '',
    ])
    const csvContent = [
      headers.join(','),
      ...rows.map(r => r.map(v => `"${v}"`).join(',')),
    ].join('\n')

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-log-${formatDateFilename(new Date())}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${formatApiError(e)}`
  } finally {
    exporting.value = false
  }
}

async function verifyChain() {
  verifying.value = true
  chainResult.value = null
  error.value = null
  try {
    const res = await (api as any).GET('/api/v1/admin/audit/verify')
    const data = res.data as any
    const err = res.error
    if (err) {
      chainResult.value = { valid: false, error: String(err) }
    } else if (data) {
      chainResult.value = {
        valid: data.valid !== false,
        event_count: data.event_count,
        error: data.detail || data.error,
      }
    }
  } catch (e: unknown) {
    chainResult.value = { valid: false, error: formatApiError(e) }
  } finally {
    verifying.value = false
  }
}

async function exportJsonl() {
  exportingJsonl.value = true
  try {
    const allEvents: AuditEvent[] = []
    let page = 1
    const pageSize = 1000
    let totalPages = 1

    while (page <= totalPages) {
      const { data, error: err } = await api.GET('/api/v1/admin/audit/export', {
        params: {
          query: {
            page,
            page_size: pageSize,
            event_type: filterEventType.value || undefined,
            user_id: filterActor.value || undefined,
            entity_type: filterTargetType.value || undefined,
            from_date: filterDateFrom.value || undefined,
            to_date: filterDateTo.value || undefined,
          } as any,
        },
      })
      if (err) {
        error.value = `${t('views.AdminAuditView.export_failed')} ${formatError(err)}`
        return
      }
      if (!data) break
      const exportPage = data as unknown as AuditPage
      allEvents.push(...exportPage.items)
      totalPages = Math.ceil(exportPage.total / pageSize)
      page++
    }

    const jsonl = allEvents.map(e => JSON.stringify(e)).join('\n')
    const blob = new Blob([jsonl], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-log-${formatDateFilename(new Date())}.jsonl`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${formatApiError(e)}`
  } finally {
    exportingJsonl.value = false
  }
}

planStore.fetchPlan()
</script>
