<template>
  <FeatureGate feature-name="audit_viewer" required-tier="team" show-disabled>

    <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AdminAuditView.audit_log') }}</h1>
        <p class="mt-1 text-muted-foreground">{{ $t('views.AdminAuditView.tamper_evident_event_trail') }}</p>
      </div>
      <div class="flex items-center gap-2">
        <button
          :disabled="verifying"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-verify-chain"
          @click="verifyChain"
        >
          {{ verifying ? $t('views.AdminAuditView.verifying') : $t('views.AdminAuditView.verify_chain') }}
        </button>
        <button
          :disabled="exporting"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
          data-testid="admin-audit-export-csv"
          @click="exportCsv"
        >
          {{ exporting ? $t('views.AdminAuditView.exporting') : $t('views.AdminAuditView.export_csv') }}
        </button>
        <button
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
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.event_type') }}</label>
          <select
            v-model="filterEventType"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-event-type"
          >
            <option value="">{{ $t('views.AdminAuditView.all_types') }}</option>
            <optgroup :label="$t('views.AdminAuditView.optgroup_pipeline')">
              <option value="pipeline.created">{{ $t('views.AdminAuditView.opt_pipeline_created') }}</option>
              <option value="pipeline.updated">{{ $t('views.AdminAuditView.opt_pipeline_updated') }}</option>
              <option value="pipeline.deleted">{{ $t('views.AdminAuditView.opt_pipeline_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_run')">
              <option value="run.started">{{ $t('views.AdminAuditView.opt_run_started') }}</option>
              <option value="run.completed">{{ $t('views.AdminAuditView.opt_run_completed') }}</option>
              <option value="run.failed">{{ $t('views.AdminAuditView.opt_run_failed') }}</option>
              <option value="run.cancelled">{{ $t('views.AdminAuditView.opt_run_cancelled') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_user')">
              <option value="user.created">{{ $t('views.AdminAuditView.opt_user_created') }}</option>
              <option value="user.updated">{{ $t('views.AdminAuditView.opt_user_updated') }}</option>
              <option value="user.deactivated">{{ $t('views.AdminAuditView.opt_user_deactivated') }}</option>
              <option value="user.activated">{{ $t('views.AdminAuditView.opt_user_activated') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_team')">
              <option value="team.created">{{ $t('views.AdminAuditView.opt_team_created') }}</option>
              <option value="team.updated">{{ $t('views.AdminAuditView.opt_team_updated') }}</option>
              <option value="team.deleted">{{ $t('views.AdminAuditView.opt_team_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_schema')">
              <option value="schema.created">{{ $t('views.AdminAuditView.opt_schema_created') }}</option>
              <option value="schema.updated">{{ $t('views.AdminAuditView.opt_schema_updated') }}</option>
              <option value="schema.deleted">{{ $t('views.AdminAuditView.opt_schema_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_connector')">
              <option value="connector.created">{{ $t('views.AdminAuditView.opt_connector_created') }}</option>
              <option value="connector.updated">{{ $t('views.AdminAuditView.opt_connector_updated') }}</option>
              <option value="connector.deleted">{{ $t('views.AdminAuditView.opt_connector_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_model_backend')">
              <option value="model_backend.created">{{ $t('views.AdminAuditView.opt_model_backend_created') }}</option>
              <option value="model_backend.updated">{{ $t('views.AdminAuditView.opt_model_backend_updated') }}</option>
              <option value="model_backend.deleted">{{ $t('views.AdminAuditView.opt_model_backend_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_sso_provider')">
              <option value="sso_provider.created">{{ $t('views.AdminAuditView.opt_sso_provider_created') }}</option>
              <option value="sso_provider.updated">{{ $t('views.AdminAuditView.opt_sso_provider_updated') }}</option>
              <option value="sso_provider.deleted">{{ $t('views.AdminAuditView.opt_sso_provider_deleted') }}</option>
              <option value="sso_provider.toggled">{{ $t('views.AdminAuditView.opt_sso_provider_toggled') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_settings')">
              <option value="settings.updated">{{ $t('views.AdminAuditView.opt_settings_updated') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_api_key')">
              <option value="api_key.created">{{ $t('views.AdminAuditView.opt_api_key_created') }}</option>
              <option value="api_key.deleted">{{ $t('views.AdminAuditView.opt_api_key_deleted') }}</option>
            </optgroup>
            <optgroup :label="$t('views.AdminAuditView.optgroup_export')">
              <option value="export.csv">{{ $t('views.AdminAuditView.opt_export_csv') }}</option>
            </optgroup>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.actor') }}</label>
          <input
            v-model="filterActor"
            type="text"
            :placeholder="$t('views.AdminAuditView.actor_id')"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-actor"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.from') }}</label>
          <input
            v-model="filterDateFrom"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-from"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.to') }}</label>
          <input
            v-model="filterDateTo"
            type="date"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-date-to"
          />
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.AdminAuditView.target_type') }}</label>
          <select
            v-model="filterTargetType"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            data-testid="admin-audit-target-type"
          >
            <option value="">{{ $t('views.AdminAuditView.all_targets') }}</option>
            <option value="pipeline">{{ $t('views.AdminAuditView.optgroup_pipeline') }}</option>
            <option value="run">{{ $t('views.AdminAuditView.optgroup_run') }}</option>
            <option value="user">{{ $t('views.AdminAuditView.optgroup_user') }}</option>
            <option value="team">{{ $t('views.AdminAuditView.optgroup_team') }}</option>
            <option value="schema">{{ $t('views.AdminAuditView.optgroup_schema') }}</option>
            <option value="connector">{{ $t('views.AdminAuditView.optgroup_connector') }}</option>
            <option value="model_backend">{{ $t('views.AdminAuditView.optgroup_model_backend') }}</option>
            <option value="sso_provider">{{ $t('views.AdminAuditView.optgroup_sso_provider') }}</option>
          </select>
        </div>
      </div>
      <div class="mt-3 flex items-center gap-2">
        <button
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          data-testid="admin-audit-apply-filters"
          @click="applyFilters"
        >
          {{ $t('views.AdminAuditView.apply_filters') }}
        </button>
        <button
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

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadEvents" />

    <div v-else-if="events.length === 0" class="card p-8 text-center">
      <p class="text-lg font-medium">{{ $t('views.AdminAuditView.no_audit_events_found') }}</p>
      <p class="mt-1 text-sm text-muted-foreground">
        {{ $t('views.AdminAuditView.try_adjusting_filters') }}
      </p>
    </div>

    <template v-else>
      <div class="card overflow-hidden">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/30 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.timestamp') }}</th>
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.event_type') }}</th>
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.actor') }}</th>
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.target') }}</th>
              <th class="px-4 py-3">{{ $t('views.AdminAuditView.summary') }}</th>
              <th class="w-8 px-4 py-3" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in events"
              :key="event.id"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              role="button"
              tabindex="0"
              :data-testid="'admin-audit-event-row-' + event.id"
              :aria-label="'Expand event ' + event.id"
              @click="toggleExpand(event.id)"
              @keydown.enter="toggleExpand(event.id)"
              @keydown.space.prevent="toggleExpand(event.id)"
            >
              <td class="whitespace-nowrap px-4 py-3 text-sm">
                {{ formatTimestamp(event.created_at) }}
              </td>
              <td class="px-4 py-3">
                <span :class="badgeClass(event.event_type)">
                  {{ event.event_type }}
                </span>
              </td>
              <td class="px-4 py-3 text-sm font-mono">
                {{ formatActor(event.actor_user_id) }}
              </td>
              <td class="px-4 py-3 text-sm">
                <span v-if="event.resource_type" class="text-muted-foreground">
                  {{ event.resource_type }}
                </span>
                <span v-if="event.resource_id" class="ml-1 font-mono text-xs text-muted-foreground/70">
                  / {{ shortId(event.resource_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <Tooltip :delay-duration="300">
                <TooltipTrigger as-child>
                  <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground">{{ summarize(event) }}</td>
                </TooltipTrigger>
                <TooltipContent side="top" class="max-w-xs">
                  <p>{{ summarize(event) }}</p>
                </TooltipContent>
              </Tooltip>
              <td class="px-4 py-3 text-xs text-muted-foreground">
                <svg
                  class="h-4 w-4 transition-transform"
                  :class="{ 'rotate-180': expandedId === event.id }"
                  xmlns="http://www.w3.org/2000/svg"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                >
                  <path d="m6 9 6 6 6-6" />
                </svg>
              </td>
            </tr>
            <tr v-if="expandedId">
              <td colspan="6" class="border-t bg-muted p-4">
                <div class="space-y-3">
                  <div v-if="expandedEvent?.payload_json && Object.keys(expandedEvent.payload_json).length > 0">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.details') }}</h4>
                    <pre class="overflow-x-auto rounded bg-background p-3 text-xs leading-relaxed">{{ JSON.stringify(expandedEvent?.payload_json, null, 2) }}</pre>
                  </div>
                  <div v-if="expandedEvent?.previous_hash" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.previous_hash') }}</h4>
                      <Tooltip :delay-duration="300">
                        <TooltipTrigger as-child>
                          <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono">{{ shortId(expandedEvent.previous_hash) }}</code>
                        </TooltipTrigger>
                        <TooltipContent side="top" class="max-w-xs">
                          <p class="font-mono text-xs break-all">{{ expandedEvent.previous_hash }}</p>
                        </TooltipContent>
                      </Tooltip>
                    </div>
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">{{ $t('views.AdminAuditView.event_id') }}</h4>
                      <Tooltip :delay-duration="300">
                        <TooltipTrigger as-child>
                          <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono">{{ shortId(expandedEvent.id) }}</code>
                        </TooltipTrigger>
                        <TooltipContent side="top" class="max-w-xs">
                          <p class="font-mono text-xs break-all">{{ shortId(expandedEvent.id) }}</p>
                        </TooltipContent>
                      </Tooltip>
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
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { formatError } from '../lib/utils'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import { shortId } from '../utils/format'

const { t } = useI18n()
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '../components/ui/tooltip'

const planStore = usePlanStore()

type AuditEvent = components['schemas']['AuditEventResponse']

const events = ref<AuditEvent[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const total = ref(0)
const nextCursor = ref<string | null>(null)
const prevCursor = ref<string | null>(null)
const currentPage = ref(1)

const filterEventType = ref('')
const filterActor = ref('')
const filterDateFrom = ref('')
const filterDateTo = ref('')
const filterTargetType = ref('')

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

  let parts = [action.charAt(0).toUpperCase() + action.slice(1), resource]
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

function buildQuery(cursor?: string | null) {
  const q: Record<string, unknown> = { limit: 50 }
  if (cursor) q.cursor = cursor
  if (filterEventType.value) q.event_type = filterEventType.value
  if (filterActor.value) q.user_id = filterActor.value
  if (filterDateFrom.value) q.from_date = filterDateFrom.value
  if (filterDateTo.value) q.to_date = filterDateTo.value
  if (filterTargetType.value) q.entity_type = filterTargetType.value
  return q
}

async function loadEvents(cursor?: string | null) {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/audit', {
      params: { query: buildQuery(cursor) as any },
    })
    if (err) {
      error.value = `${t('views.AdminAuditView.failed_to_load_audit_events')} ${formatError(err)}`
    } else if (data) {
      events.value = data.items
      total.value = data.total
      nextCursor.value = data.next_cursor
      prevCursor.value = data.prev_cursor
      expandedId.value = null
      expandedEvent.value = null
    }
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.failed_to_load_audit_events')} ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

function goToPage(cursor: string | null) {
  if (!cursor) return
  currentPage.value = prevCursor.value === cursor
    ? Math.max(1, currentPage.value - 1)
    : currentPage.value + 1
  loadEvents(cursor)
}

function applyFilters() {
  currentPage.value = 1
  loadEvents(null)
}

function resetFilters() {
  filterEventType.value = ''
  filterActor.value = ''
  filterDateFrom.value = ''
  filterDateTo.value = ''
  filterTargetType.value = ''
  currentPage.value = 1
  loadEvents(null)
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
      allEvents.push(...data.items)
      totalPages = Math.ceil(data.total / pageSize)
      page++
    }

    const headers = ['Timestamp', 'Event Type', 'Actor ID', 'Target Type', 'Target ID', 'Summary', 'Request ID', 'Previous Hash']
    const rows = allEvents.map(e => [
      e.created_at ?? '',
      e.event_type,
      e.actor_user_id ?? '',
      e.resource_type ?? '',
      e.resource_id ?? '',
      summarize(e).replace(/"/g, '""'),
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
    a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${e instanceof Error ? e.message : String(e)}`
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
        error: data.error,
      }
    }
  } catch (e: unknown) {
    chainResult.value = { valid: false, error: e instanceof Error ? e.message : String(e) }
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
      allEvents.push(...data.items)
      totalPages = Math.ceil(data.total / pageSize)
      page++
    }

    const jsonl = allEvents.map(e => JSON.stringify(e)).join('\n')
    const blob = new Blob([jsonl], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.jsonl`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e: unknown) {
    error.value = `${t('views.AdminAuditView.export_failed')} ${e instanceof Error ? e.message : String(e)}`
  } finally {
    exportingJsonl.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadEvents(null) })
</script>
