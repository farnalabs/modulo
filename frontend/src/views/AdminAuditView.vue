<template>
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold tracking-tight">Audit Log</h1>
        <p class="mt-1 text-muted-foreground">Tamper-evident event trail for your organisation</p>
      </div>
      <button
        :disabled="exporting"
        class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
        @click="exportCsv"
      >
        {{ exporting ? 'Exporting...' : 'Export CSV' }}
      </button>
    </header>

    <div class="rounded-lg border bg-card p-4 shadow-sm">
      <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Event Type</label>
          <select
            v-model="filterEventType"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All types</option>
            <optgroup label="Pipeline">
              <option value="pipeline.created">Created</option>
              <option value="pipeline.updated">Updated</option>
              <option value="pipeline.deleted">Deleted</option>
            </optgroup>
            <optgroup label="Run">
              <option value="run.started">Started</option>
              <option value="run.completed">Completed</option>
              <option value="run.failed">Failed</option>
              <option value="run.cancelled">Cancelled</option>
            </optgroup>
            <optgroup label="User">
              <option value="user.created">Created</option>
              <option value="user.updated">Updated</option>
              <option value="user.deactivated">Deactivated</option>
              <option value="user.activated">Activated</option>
            </optgroup>
            <optgroup label="Team">
              <option value="team.created">Created</option>
              <option value="team.updated">Updated</option>
              <option value="team.deleted">Deleted</option>
            </optgroup>
            <optgroup label="Schema">
              <option value="schema.created">Created</option>
              <option value="schema.updated">Updated</option>
              <option value="schema.deleted">Deleted</option>
            </optgroup>
            <optgroup label="Connector">
              <option value="connector.created">Created</option>
              <option value="connector.updated">Updated</option>
              <option value="connector.deleted">Deleted</option>
            </optgroup>
            <optgroup label="Model Backend">
              <option value="model_backend.created">Created</option>
              <option value="model_backend.updated">Updated</option>
              <option value="model_backend.deleted">Deleted</option>
            </optgroup>
            <optgroup label="SSO Provider">
              <option value="sso_provider.created">Created</option>
              <option value="sso_provider.updated">Updated</option>
              <option value="sso_provider.deleted">Deleted</option>
              <option value="sso_provider.toggled">Toggled</option>
            </optgroup>
            <optgroup label="Settings">
              <option value="settings.updated">Updated</option>
            </optgroup>
            <optgroup label="API Key">
              <option value="api_key.created">Created</option>
              <option value="api_key.deleted">Deleted</option>
            </optgroup>
            <optgroup label="Export">
              <option value="export.csv">CSV Export</option>
            </optgroup>
          </select>
        </div>
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Actor</label>
          <input
            v-model="filterActor"
            type="text"
            placeholder="User ID..."
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          />
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
        <div>
          <label class="mb-1 block text-xs font-medium text-muted-foreground">Target Type</label>
          <select
            v-model="filterTargetType"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          >
            <option value="">All targets</option>
            <option value="pipeline">Pipeline</option>
            <option value="run">Run</option>
            <option value="user">User</option>
            <option value="team">Team</option>
            <option value="schema">Schema</option>
            <option value="connector">Connector</option>
            <option value="model_backend">Model Backend</option>
            <option value="sso_provider">SSO Provider</option>
          </select>
        </div>
      </div>
      <div class="mt-3 flex items-center gap-2">
        <button
          class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
          @click="applyFilters"
        >
          Apply Filters
        </button>
        <button
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
          @click="resetFilters"
        >
          Reset
        </button>
        <span v-if="total > 0" class="ml-auto text-sm text-muted-foreground">
          {{ total }} event{{ total === 1 ? '' : 's' }}
        </span>
      </div>
    </div>

    <div v-if="loading" class="flex items-center justify-center py-16">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="error" class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">
      {{ error }}
      <button class="ml-2 underline" @click="() => loadEvents()">Retry</button>
    </div>

    <div v-else-if="events.length === 0" class="rounded-lg border bg-card p-8 text-center">
      <p class="text-lg font-medium">No audit events found</p>
      <p class="mt-1 text-sm text-muted-foreground">
        Try adjusting your filters or wait for activity to be recorded.
      </p>
    </div>

    <template v-else>
      <div class="overflow-hidden rounded-lg border bg-card shadow-sm">
        <table class="w-full">
          <thead>
            <tr class="border-b bg-muted/50 text-left text-xs font-medium uppercase text-muted-foreground">
              <th class="px-4 py-3">Timestamp</th>
              <th class="px-4 py-3">Event Type</th>
              <th class="px-4 py-3">Actor</th>
              <th class="px-4 py-3">Target</th>
              <th class="px-4 py-3">Summary</th>
              <th class="w-8 px-4 py-3" />
            </tr>
          </thead>
          <tbody class="divide-y">
            <tr
              v-for="event in events"
              :key="event.id"
              class="cursor-pointer transition-colors hover:bg-muted/30"
              @click="toggleExpand(event.id)"
            >
              <td class="whitespace-nowrap px-4 py-3 text-sm">
                {{ formatTimestamp(event.created_at) }}
              </td>
              <td class="px-4 py-3">
                <span
                  class="inline-block rounded-full px-2.5 py-0.5 text-xs font-medium"
                  :class="badgeClass(event.event_type)"
                >
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
                  / {{ truncateId(event.resource_id) }}
                </span>
                <span v-else class="text-muted-foreground/50">&mdash;</span>
              </td>
              <td class="max-w-xs truncate px-4 py-3 text-sm text-muted-foreground">
                {{ summarize(event) }}
              </td>
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
              <td colspan="6" class="border-t bg-muted/20 p-4">
                <div class="space-y-3">
                  <div v-if="expandedEvent?.payload_json && Object.keys(expandedEvent.payload_json).length > 0">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">Details</h4>
                    <pre class="overflow-x-auto rounded bg-background p-3 text-xs leading-relaxed">{{ JSON.stringify(expandedEvent?.payload_json, null, 2) }}</pre>
                  </div>
                  <div v-if="expandedEvent?.previous_hash" class="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">Previous Hash</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedEvent.previous_hash }}</code>
                    </div>
                    <div>
                      <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">Event ID</h4>
                      <code class="block truncate rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedEvent.id }}</code>
                    </div>
                  </div>
                  <div v-if="expandedEvent?.request_id">
                    <h4 class="mb-1 text-xs font-semibold uppercase text-muted-foreground">Request ID</h4>
                    <code class="rounded bg-background px-2 py-1 text-xs font-mono">{{ expandedEvent.request_id }}</code>
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
          @click="() => goToPage(prevCursor)"
        >
          Previous
        </button>
        <span class="text-sm text-muted-foreground">
          Page {{ currentPage }} &middot; {{ events.length }} of {{ total }} events
        </span>
        <button
          :disabled="!nextCursor"
          class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-30 disabled:cursor-not-allowed"
          @click="() => goToPage(nextCursor)"
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

function truncateId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) + '...' : id
}

function formatActor(actorId: string | null): string {
  if (!actorId) return '—'
  return 'usr_' + actorId.slice(0, 8)
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
  if (eventType.startsWith('pipeline.')) return 'bg-blue-100 text-blue-700'
  if (eventType.startsWith('run.completed')) return 'bg-green-100 text-green-700'
  if (eventType.startsWith('run.failed')) return 'bg-red-100 text-red-700'
  if (eventType.startsWith('run.')) return 'bg-amber-100 text-amber-700'
  if (eventType.startsWith('user.')) return 'bg-purple-100 text-purple-700'
  if (eventType.startsWith('team.')) return 'bg-indigo-100 text-indigo-700'
  if (eventType.startsWith('schema.')) return 'bg-cyan-100 text-cyan-700'
  if (eventType.startsWith('connector.')) return 'bg-orange-100 text-orange-700'
  if (eventType.startsWith('model_backend.')) return 'bg-pink-100 text-pink-700'
  if (eventType.startsWith('sso_provider.')) return 'bg-slate-100 text-slate-700'
  if (eventType.startsWith('settings.')) return 'bg-gray-100 text-gray-700'
  if (eventType.startsWith('api_key.')) return 'bg-rose-100 text-rose-700'
  if (eventType.startsWith('export.')) return 'bg-teal-100 text-teal-700'
  return 'bg-gray-100 text-gray-700'
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
      error.value = `Failed to load audit events: ${err}`
    } else if (data) {
      events.value = data.items
      total.value = data.total
      nextCursor.value = data.next_cursor
      prevCursor.value = data.prev_cursor
      expandedId.value = null
      expandedEvent.value = null
    }
  } catch (e: unknown) {
    error.value = `Failed to load audit events: ${e instanceof Error ? e.message : String(e)}`
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
        error.value = `Export failed: ${err}`
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
    error.value = `Export failed: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    exporting.value = false
  }
}

onMounted(() => loadEvents(null))
</script>
