<template>
  <FeatureGate feature-name="error_tracking" required-tier="team" show-disabled>
  <div class="page-wide">
    <BackLink to="/admin/errors" :label="$t('views.AdminErrorDetailView.back_to_error_dashboard')" />
    <header class="flex items-center justify-between">
      <div class="flex items-center gap-3">
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
          data-testid="admin-error-detail-back"
          @click="goBack"
        >
          <ArrowLeft class="mr-1 inline-block h-4 w-4" aria-hidden="true" />
          {{ $t('views.AdminErrorDetailView.back') }}
        </button>
        <PageHeader :title="$t('views.AdminErrorDetailView.error_group_detail')" :subtitle="group ? shortId(group.fingerprint) : undefined" />
      </div>
    </header>

    <div
      v-if="loading"
      class="space-y-4"
      role="status"
      aria-busy="true"
      :aria-label="$t('common.loading')"
      data-testid="admin-error-detail-loading"
    >
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div v-for="n in 3" :key="'summary-' + n" class="card p-4">
          <div class="h-3 w-20 animate-pulse rounded bg-muted" />
          <div class="mt-2 h-5 w-16 animate-pulse rounded bg-muted" />
        </div>
      </div>
      <div class="card p-4">
        <SkeletonBlock class="h-5 w-32" />
        <SkeletonBlock class="mt-3 h-9 w-64" />
      </div>
      <div class="card p-4">
        <SkeletonBlock class="h-5 w-48" />
        <SkeletonBlock class="mt-2 h-16 w-full" />
      </div>
    </div>
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDetail" />
    <template v-else-if="group">
      <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.level') }}</span>
          <p class="mt-0.5">
            <span :class="levelBadgeClass(group.level_peak)">{{ group.level_peak }}</span>
          </p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground capitalize">{{ $t('views.AdminErrorDetailView.status') }}</span>
          <p class="mt-0.5">
            <span :class="statusBadgeClass(group.status)" class="capitalize">{{ group.status }}</span>
          </p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.occurrences') }}</span>
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
        <h2 class="mb-3 text-base font-semibold">{{ $t('views.AdminErrorDetailView.actions') }}</h2>
        <div class="flex flex-wrap items-center gap-3">
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'acknowledged'"
            data-testid="admin-error-detail-acknowledge"
            @click="updateStatus('acknowledged')"
          >
            {{ $t('views.AdminErrorDetailView.acknowledge') }}
          </button>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'resolved'"
            data-testid="admin-error-detail-resolve"
            @click="updateStatus('resolved')"
          >
            {{ $t('views.AdminErrorDetailView.resolve') }}
          </button>
          <button
            type="button"
            class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
            :disabled="group.status === 'archived'"
            data-testid="admin-error-detail-archive"
            @click="updateStatus('archived')"
          >
            {{ $t('views.AdminErrorDetailView.archive') }}
          </button>
            <div class="ml-auto flex items-center gap-2">
            <label for="assignee-select" class="text-xs text-muted-foreground">{{ $t('views.AdminErrorDetailView.assign_to') }}</label>
            <Select
  v-model="assigneeId"
  :aria-label="$t('views.AdminErrorDetailView.assign_to')"
  @update:model-value="updateAssignee"
  :placeholder="$t('views.AdminErrorDetailView.unassigned')"
  id="assignee-select"
  data-testid="admin-error-detail-assignee"
  :options="users.map(user => ({ value: user.id, label: user.display_name || user.email }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          </div>
        </div>
      </div>

      <div v-if="sampleEvent" class="card p-4">
        <h2 class="mb-2 text-base font-semibold">{{ $t('views.AdminErrorDetailView.message') }}</h2>
        <p class="rounded-lg bg-muted p-3 text-sm font-mono">{{ sampleEvent.message }}</p>

        <div v-if="sampleEvent.stacktrace" class="mt-4">
          <button
            type="button"
            class="flex items-center gap-1 rounded text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="admin-error-detail-toggle-stacktrace"
            :aria-expanded="showStacktrace"
            @click="showStacktrace = !showStacktrace"
          >
            <ChevronRight
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showStacktrace }"
              aria-hidden="true"
            />
            {{ $t('views.AdminErrorDetailView.stacktrace') }}
          </button>
          <pre v-if="showStacktrace" class="mt-2 max-h-96 overflow-auto rounded-lg bg-muted p-3 text-xs leading-relaxed"><code>{{ sampleEvent.stacktrace }}</code></pre>
        </div>

        <div v-if="sampleEvent.context_json && Object.keys(sampleEvent.context_json).length > 0" class="mt-4">
          <button
            type="button"
            class="flex items-center gap-1 rounded text-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="admin-error-detail-toggle-context"
            :aria-expanded="showContext"
            @click="showContext = !showContext"
          >
            <ChevronRight
              class="h-4 w-4 transition-transform"
              :class="{ 'rotate-90': showContext }"
              aria-hidden="true"
            />
            {{ $t('views.AdminErrorDetailView.context_json') }}
          </button>
          <JsonViewer v-if="showContext" :data="sampleEvent.context_json ?? null" :show-toolbar="true" :max-height="'16rem'" />
        </div>

        <div class="mt-4 grid grid-cols-2 gap-4 text-sm">
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.source') }}</span>
            <p class="mt-0.5 capitalize">{{ sampleEvent.source }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.environment') }}</span>
            <p class="mt-0.5 capitalize">{{ sampleEvent.environment || '—' }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.version') }}</span>
            <p class="mt-0.5">{{ sampleEvent.version || '—' }}</p>
          </div>
          <div>
            <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.event_id') }}</span>
            <p class="mt-0.5 font-mono text-xs">{{ shortId(sampleEvent.id) }}</p>
          </div>
        </div>
      </div>

      <div class="card p-4">
        <h2 class="mb-3 text-base font-semibold">{{ $t('views.AdminErrorDetailView.raw_events', { count: eventsTotal }) }}</h2>
        <div v-if="eventsLoading" class="space-y-2 py-2" role="status" :aria-label="$t('common.loading')" data-testid="admin-error-detail-events-loading">
          <SkeletonBlock v-for="n in 3" :key="'event-skeleton-' + n" height-class="h-12 w-full" />
        </div>
        <EmptyState
          v-else-if="events.length === 0"
          :title="$t('views.AdminErrorDetailView.no_raw_events_loaded')"
          :description="$t('views.AdminErrorDetailView.no_raw_events_description')"
        />
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
              {{ $t('views.AdminErrorDetailView.events_of', { count: events.length, total: eventsTotal }) }}
            </span>
            <div class="flex gap-2">
              <button
                type="button"
                :disabled="eventsOffset <= 0"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-30"
                data-testid="admin-error-detail-events-prev"
                @click="loadEvents(eventsOffset - eventsLimit)"
              >
                {{ $t('views.AdminErrorDetailView.previous') }}
              </button>
              <button
                type="button"
                :disabled="eventsOffset + eventsLimit >= eventsTotal"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent disabled:opacity-30"
                data-testid="admin-error-detail-events-next"
                @click="loadEvents(eventsOffset + eventsLimit)"
              >
                {{ $t('views.AdminErrorDetailView.next') }}
              </button>
            </div>
          </div>
        </template>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import JsonViewer from '../components/shared/JsonViewer.vue'
import SkeletonBlock from '../components/shared/SkeletonBlock.vue'
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, ChevronRight } from '@lucide/vue'
import { fetchErrorGroup, updateErrorGroup, fetchErrorGroupEvents, type ErrorGroupDetail, type ErrorEventDetail } from '../lib/api/errors'
import { api } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import BackLink from '../components/BackLink.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatApiError } from '../lib/api/formatError'
import { shortId } from '../utils/format'
import Select from '../components/shared/AppSelect.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const errorId = route.params.id as string

const showStacktrace = ref(false)
const showContext = ref(false)

const assigneeId = ref('')
const users = ref<Array<{ id: string; email: string; display_name: string }>>([])

const events = ref<ErrorEventDetail[]>([])
const eventsTotal = ref(0)
const eventsLoading = ref(false)
const eventsOffset = ref(0)
const eventsLimit = 20

const { data: groupData, loading, error, load: loadDetail } = useDataFetch(
  () => fetchErrorGroup(errorId).then(
    d => ({ data: d }),
    e => ({ error: { detail: `${t('views.AdminErrorDetailView.failed_to_load_error_group')} ${formatApiError(e)}` } }),
  ),
  { initialValue: null as ErrorGroupDetail | null }
)

const group = computed(() => groupData.value ?? null)
const sampleEvent = computed(() => groupData.value?.sample_event ?? null)

watch(() => groupData.value, (g) => {
  if (g) assigneeId.value = g.assigned_to || ''
})

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

async function updateStatus(status: string) {
  try {
    await updateErrorGroup(errorId, { status })
    await loadDetail()
  } catch (e: unknown) {
    error.value = `${t('views.AdminErrorDetailView.failed_to_update_status')} ${formatApiError(e)}`
  }
}

async function updateAssignee() {
  try {
    await updateErrorGroup(errorId, { assigned_to: assigneeId.value || undefined })
  } catch (e: unknown) {
    error.value = `${t('views.AdminErrorDetailView.failed_to_update_assignee')} ${formatApiError(e)}`
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
    error.value = `${t('views.AdminErrorDetailView.failed_to_load_events')} ${formatApiError(e)}`
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
  } catch (e) {
    console.warn('Failed to load users', e)
  }
}

loadEvents(0)
loadUsers()
</script>
