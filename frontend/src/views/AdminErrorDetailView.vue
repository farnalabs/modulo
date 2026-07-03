<template>
  <BackLink to="/admin/errors" label="Back to Error Dashboard" />
  <div class="mx-auto max-w-6xl space-y-6 p-6">
    <header class="flex items-center justify-between">
      <div class="flex items-center gap-3">
        <button
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
          @click="goBack"
        >
          &larr; Back
        </button>
        <div>
          <h1 class="text-2xl font-bold tracking-tight">{{ $t('views.AdminErrorDetailView.error_group_detail') }}</h1>
          <p v-if="group" class="mt-0.5 text-sm text-muted-foreground font-mono">
            {{ shortId(group.fingerprint) }}
          </p>
        </div>
      </div>
    </header>

    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDetail" />
    <template v-else-if="group">
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">Level</span>
          <p class="mt-0.5">
            <span :class="levelBadgeClass(group.level_peak)">{{ group.level_peak }}</span>
          </p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">Status</span>
          <p class="mt-0.5">
            <span :class="statusBadgeClass(group.status)">{{ group.status }}</span>
          </p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">Occurrences</span>
          <p class="mt-0.5 text-lg font-semibold">{{ group.count }}</p>
        </div>
      </div>

      <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.first_seen') }}</span>
          <p class="mt-0.5 text-sm">{{ formatDate(group.first_seen) }}</p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.last_seen') }}</span>
          <p class="mt-0.5 text-sm">{{ formatDate(group.last_seen) }}</p>
        </div>
      </div>

      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">Actions</h2>
        <div class="flex flex-wrap items-center gap-3">
          <button
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'acknowledged'"
            @click="updateStatus('acknowledged')"
          >
            Acknowledge
          </button>
          <button
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'resolved'"
            @click="updateStatus('resolved')"
          >
            Resolve
          </button>
          <button
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'archived'"
            @click="updateStatus('archived')"
          >
            Archive
          </button>
          <div class="ml-auto flex items-center gap-2">
            <span class="text-xs text-muted-foreground">{{ $t('views.AdminErrorDetailView.assign_to') }}</span>
            <select
              v-model="assigneeId"
              class="rounded-lg border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              @change="updateAssignee"
            >
              <option value="">Unassigned</option>
              <option v-for="user in users" :key="user.id" :value="user.id">
                {{ user.display_name || user.email }}
              </option>
            </select>
          </div>
        </div>
      </div>

      <div v-if="sampleEvent" class="card p-4">
        <h2 class="mb-2 text-lg font-semibold">Message</h2>
        <p class="rounded-lg bg-muted p-3 text-sm font-mono">{{ sampleEvent.message }}</p>

        <div v-if="sampleEvent.stacktrace" class="mt-4">
          <button
            class="flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground"
            @click="showStacktrace = !showStacktrace"
          >
            <svg
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showStacktrace }"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
            Stacktrace
          </button>
          <pre v-if="showStacktrace" class="mt-2 max-h-96 overflow-auto rounded-lg bg-muted p-3 text-xs leading-relaxed"><code>{{ sampleEvent.stacktrace }}</code></pre>
        </div>

        <div v-if="sampleEvent.context_json && Object.keys(sampleEvent.context_json).length > 0" class="mt-4">
          <button
            class="flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground"
            @click="showContext = !showContext"
          >
            <svg
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showContext }"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="m9 18 6-6-6-6" />
            </svg>
            Context JSON
          </button>
          <pre v-if="showContext" class="mt-2 max-h-64 overflow-auto rounded-lg bg-muted p-3 text-xs"><code>{{ JSON.stringify(sampleEvent.context_json, null, 2) }}</code></pre>
        </div>

        <div class="mt-4 grid grid-cols-2 gap-4 text-sm">
          <div>
            <span class="text-xs font-medium text-muted-foreground">Source</span>
            <p class="mt-0.5 capitalize">{{ sampleEvent.source }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Environment</span>
            <p class="mt-0.5 capitalize">{{ sampleEvent.environment || '—' }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">Version</span>
            <p class="mt-0.5">{{ sampleEvent.version || '—' }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.event_id') }}</span>
            <p class="mt-0.5 font-mono text-xs">{{ shortId(sampleEvent.id) }}</p>
          </div>
        </div>
      </div>

      <div class="card p-4">
        <h2 class="mb-3 text-lg font-semibold">Raw Events ({{ eventsTotal }})</h2>
        <LoadingSpinner v-if="eventsLoading" />
        <div v-else-if="events.length === 0" class="py-4 text-center text-sm text-muted-foreground">
          No raw events loaded.
        </div>
        <template v-else>
          <div class="divide-y">
            <div
              v-for="evt in events"
              :key="evt.id"
              class="py-3 first:pt-0 last:pb-0"
            >
              <div class="flex items-center justify-between">
                <span :class="levelBadgeClass(evt.level)">{{ evt.level }}</span>
                <span class="text-xs text-muted-foreground">{{ formatDate(evt.created_at) }}</span>
              </div>
              <p class="mt-1 text-sm font-mono">{{ evt.message }}</p>
              <div class="mt-1 flex gap-3 text-xs text-muted-foreground">
                <span class="capitalize">{{ evt.source }}</span>
                <span v-if="evt.environment" class="capitalize">{{ evt.environment }}</span>
                <span v-if="evt.version">v{{ evt.version }}</span>
              </div>
            </div>
          </div>
          <div class="mt-3 flex items-center justify-between border-t pt-3">
            <span class="text-sm text-muted-foreground">
              {{ events.length }} of {{ eventsTotal }} events
            </span>
            <div class="flex gap-2">
              <button
                :disabled="eventsOffset <= 0"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-30"
                @click="loadEvents(eventsOffset - eventsLimit)"
              >
                Previous
              </button>
              <button
                :disabled="eventsOffset + eventsLimit >= eventsTotal"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-30"
                @click="loadEvents(eventsOffset + eventsLimit)"
              >
                Next
              </button>
            </div>
          </div>
        </template>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchErrorGroup, updateErrorGroup, fetchErrorGroupEvents, type ErrorGroupDetail, type ErrorEventDetail } from '../lib/api/errors'
import { api } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import BackLink from '../components/BackLink.vue'
import { shortId } from '../utils/format'

const route = useRoute()
const router = useRouter()
const errorId = route.params.id as string

const group = ref<ErrorGroupDetail | null>(null)
const sampleEvent = ref<ErrorEventDetail | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

const showStacktrace = ref(false)
const showContext = ref(false)

const assigneeId = ref('')
const users = ref<Array<{ id: string; email: string; display_name: string }>>([])

const events = ref<ErrorEventDetail[]>([])
const eventsTotal = ref(0)
const eventsLoading = ref(false)
const eventsOffset = ref(0)
const eventsLimit = 20

function goBack() {
  router.push('/admin/errors')
}

function levelBadgeClass(level: string): string {
  if (level === 'critical') return 'badge badge-status-destructive'
  if (level === 'warning') return 'badge badge-status-warning'
  return 'badge badge-context-blue'
}

function statusBadgeClass(status: string): string {
  if (status === 'new') return 'badge badge-status-destructive'
  if (status === 'acknowledged') return 'badge badge-status-warning'
  if (status === 'resolved') return 'badge badge-status-success'
  if (status === 'archived') return 'badge badge-status-muted'
  return 'badge'
}

function formatDate(dateStr: string): string {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function loadDetail() {
  loading.value = true
  error.value = null
  try {
    const data = await fetchErrorGroup(errorId)
    group.value = data
    sampleEvent.value = data.sample_event
    assigneeId.value = data.assigned_to || ''
  } catch (e: unknown) {
    error.value = `Failed to load error group: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function updateStatus(status: string) {
  try {
    await updateErrorGroup(errorId, { status })
    await loadDetail()
  } catch (e: unknown) {
    error.value = `Failed to update status: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function updateAssignee() {
  try {
    await updateErrorGroup(errorId, { assigned_to: assigneeId.value || undefined })
  } catch (e: unknown) {
    error.value = `Failed to update assignee: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadEvents(offset?: number) {
  eventsLoading.value = true
  if (offset !== undefined) eventsOffset.value = offset
  try {
    const data = await fetchErrorGroupEvents(errorId, { limit: eventsLimit, offset: eventsOffset.value })
    events.value = data.items
    eventsTotal.value = data.total
  } catch (e: unknown) {
    error.value = `Failed to load events: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    eventsLoading.value = false
  }
}

async function loadUsers() {
  try {
    const { data } = await api.GET('/api/v1/admin/users')
    if (data) {
      users.value = data.items
    }
  } catch {
    // Users list is non-critical
  }
}

onMounted(() => {
  loadDetail()
  loadEvents(0)
  loadUsers()
})
</script>
