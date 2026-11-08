<template>
  <FeatureGate feature-name="error_tracking" required-tier="team" show-disabled>
  <div class="page-wide">
    <BackLink :to="isInstanceScope ? '/admin/errors?scope=instance' : '/admin/errors'" :label="$t('views.AdminErrorDetailView.back_to_error_dashboard')" />
    <PageHeader
      :title="$t('views.AdminErrorDetailView.error_group_detail')"
      :subtitle="$t('views.AdminErrorDetailView.subtitle')"
    >
      <template #right>
        <button
          type="button"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent"
          data-testid="admin-error-detail-back"
          @click="goBack"
        >
          <ArrowLeft class="mr-1 inline-block h-4 w-4" aria-hidden="true" />
          {{ $t('views.AdminErrorDetailView.back') }}
        </button>
        <!-- FAR-2304: the canonical support reference (group id, fingerprint,
             level, count, source) in one copy action. Available in instance scope
             too — copying is read-only. -->
        <button
          v-if="group"
          type="button"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="admin-error-detail-copy-support"
          @click="copySupportReference"
        >
          {{ supportCopied ? $t('views.AdminErrorDetailView.support_reference_copied') : $t('views.AdminErrorDetailView.copy_support_reference') }}
        </button>
      </template>
    </PageHeader>

    <div v-if="loading" data-testid="admin-error-detail-loading">
      <!-- Live region carries only the status text; the skeleton blocks are
           layout, not phrasing content, so they are siblings. -->
      <output class="sr-only" aria-live="polite">{{ $t('common.loading') }}</output>
      <div class="space-y-4" aria-busy="true">
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div v-for="n in 3" :key="'summary-' + n" class="card p-4">
            <div class="h-3 w-20 animate-pulse rounded bg-muted" />
            <div class="mt-2 h-5 w-16 animate-pulse rounded bg-muted" />
          </div>
        </div>
        <div class="card p-4">
          <SkeletonBlock height-class="h-5 w-32" />
          <SkeletonBlock class="mt-3" height-class="h-9 w-64" />
        </div>
        <div class="card p-4">
          <SkeletonBlock height-class="h-5 w-48" />
          <SkeletonBlock class="mt-2" height-class="h-16 w-full" />
        </div>
      </div>
    </div>
    <ErrorAlert v-else-if="error" :message="error" :on-retry="loadDetail" />
    <template v-else-if="group">
      <div
        v-if="isInstanceScope"
        class="rounded-lg border border-border bg-muted/40 p-3 text-sm text-muted-foreground"
        aria-live="polite"
        data-testid="admin-error-detail-instance-readonly"
      >
        {{ $t('views.AdminErrorDetailView.instance_scope_readonly') }}
      </div>
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
          <p class="mt-0.5 text-sm">{{ formatErrorDate(group.first_seen) }}</p>
        </div>
        <div class="card p-4">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AdminErrorDetailView.last_seen') }}</span>
          <p class="mt-0.5 text-sm">{{ formatErrorDate(group.last_seen) }}</p>
        </div>
      </div>

      <!-- FAR-2374: every error-record write is gated on `errors.manage`
           (admin) by FAR-2373. A viewer keeps the whole READ surface but no
           write control is actionable, and each disabled control is described
           with the reason — via aria-describedby for assistive tech plus the
           shared v-tooltip affordance. The tooltip sits on the wrapping span
           because a disabled control swallows hover events, so a tooltip bound
           directly to it would never open. -->
      <div v-if="!isInstanceScope" class="card p-4">
        <h2 class="mb-3 text-base font-semibold">{{ $t('views.AdminErrorDetailView.actions') }}</h2>
        <p
          v-if="!canManageErrors"
          id="admin-error-status-admin-help"
          class="mb-3 text-xs text-muted-foreground"
          data-testid="admin-error-detail-status-admin-help"
        >
          {{ statusAdminHelp }}
        </p>
        <div class="flex flex-wrap items-center gap-3">
          <span v-tooltip.top="{ value: statusAdminHelp, disabled: canManageErrors }" class="inline-flex">
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
              :disabled="!canManageErrors || group.status === 'acknowledged'"
              :aria-describedby="statusAdminDescribedBy"
              data-testid="admin-error-detail-acknowledge"
              @click="updateStatus('acknowledged')"
            >
              {{ $t('views.AdminErrorDetailView.acknowledge') }}
            </button>
          </span>
          <span v-tooltip.top="{ value: statusAdminHelp, disabled: canManageErrors }" class="inline-flex">
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
              :disabled="!canManageErrors || group.status === 'resolved'"
              :aria-describedby="statusAdminDescribedBy"
              data-testid="admin-error-detail-resolve"
              @click="updateStatus('resolved')"
            >
              {{ $t('views.AdminErrorDetailView.resolve') }}
            </button>
          </span>
          <span v-tooltip.top="{ value: statusAdminHelp, disabled: canManageErrors }" class="inline-flex">
            <button
              type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
              :disabled="!canManageErrors || group.status === 'archived'"
              :aria-describedby="statusAdminDescribedBy"
              data-testid="admin-error-detail-archive"
              @click="updateStatus('archived')"
            >
              {{ $t('views.AdminErrorDetailView.archive') }}
            </button>
          </span>
          <div class="ml-auto flex flex-col items-end gap-1">
            <div class="flex items-center gap-2">
              <label for="assignee-select" class="text-xs text-muted-foreground">{{ $t('views.AdminErrorDetailView.assign_to') }}</label>
              <span
                v-tooltip.top="{ value: assignAdminHelp, disabled: canManageErrors }"
                class="inline-flex"
                :class="{ 'opacity-60': !canManageErrors }"
              >
                <Select
                  v-model="assigneeId"
                  :aria-label="$t('views.AdminErrorDetailView.assign_to')"
                  :disabled="!canManageErrors"
                  :pt="assignSelectPt"
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
              </span>
            </div>
            <p
              v-if="!canManageErrors"
              id="admin-error-assign-admin-help"
              class="text-xs text-muted-foreground"
              data-testid="admin-error-detail-assign-admin-help"
            >
              {{ assignAdminHelp }}
            </p>
          </div>
        </div>
      </div>

      <div v-if="sampleEvent" class="card p-4">
        <h2 class="mb-2 text-base font-semibold">{{ $t('views.AdminErrorDetailView.message') }}</h2>
        <p class="mb-2 text-xs text-muted-foreground">
          {{ $t('views.AdminErrorDetailView.fingerprint') }}: <span class="font-mono">{{ shortId(group.fingerprint) }}</span>
        </p>
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
        <div v-if="eventsLoading" class="py-2" data-testid="admin-error-detail-events-loading">
          <!-- Live region carries only the status text; the skeleton blocks are
               layout, not phrasing content, so they are siblings. -->
          <output class="sr-only" aria-live="polite">{{ $t('common.loading') }}</output>
          <div class="space-y-2" aria-busy="true">
            <SkeletonBlock v-for="n in 3" :key="'event-skeleton-' + n" height-class="h-12 w-full" />
          </div>
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
                <span class="text-xs text-muted-foreground">{{ formatErrorDate(evt.created_at) }}</span>
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
import { fetchErrorGroup, updateErrorGroup, fetchErrorGroupEvents, fetchInstanceErrorGroup, fetchInstanceErrorGroupEvents, type ErrorGroupDetail, type ErrorEventDetail, type ErrorEventListResponse } from '../lib/api/errors'
import { api } from '../lib/api/client'
import { useCurrentUser } from '../composables/useCurrentUser'
import { useDataFetch } from '../composables/useDataFetch'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import BackLink from '../components/BackLink.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { formatApiError } from '../lib/api/formatError'
import { shortId } from '../utils/format'
import { formatErrorDate, levelBadgeClass, statusBadgeClass } from '../utils/errorGroupPresentation'
import Select from '../components/shared/AppSelect.vue'
import { buildErrorGroupSupportReference } from '../lib/supportReference'
import { useErrorAdminActions } from '../composables/useErrorAdminActions'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const errorId = route.params.id as string

// FAR-1547: ?scope=instance renders this group from the SYSTEM_ORG_ID sentinel
// partition (read-only — the instance surface has no PATCH). The scope travels
// in the query so the back link returns to the same tab it came from. Mirrors
// the list view: without the is_system_admin claim the param resolves to the
// tenant scope, and the backend independently refuses any instance read with
// 403, so a forged param can never surface sentinel rows.
const { isSystemAdmin } = useCurrentUser()
const isInstanceScope = isSystemAdmin.value && route.query?.scope === 'instance'

// FAR-2374: the same admin-only permission that gates the PATCH endpoint. The
// detail view already refuses to render the actions card in instance scope, so
// this only distinguishes an admin from a viewer WITHIN the tenant scope.
const { canManageErrors } = useErrorAdminActions()

const STATUS_ADMIN_HELP_ID = 'admin-error-status-admin-help'
const ASSIGN_ADMIN_HELP_ID = 'admin-error-assign-admin-help'

const statusAdminHelp = computed(() => t('views.AdminErrorDetailView.admin_only_status_help'))
const assignAdminHelp = computed(() => t('views.AdminErrorDetailView.admin_only_assign_help'))

// Only referenced while the matching help text is rendered, so an admin never
// carries a dangling aria-describedby reference.
const statusAdminDescribedBy = computed(() =>
  canManageErrors.value ? undefined : STATUS_ADMIN_HELP_ID,
)

// PrimeVue forwards fallthrough attrs to the Select's wrapper, not to the
// focusable combobox, so the assignee hint is routed through `pt.label` — the
// same pattern the environment-profile form uses.
const assignSelectPt = computed(() =>
  canManageErrors.value
    ? {}
    : { label: { 'aria-describedby': ASSIGN_ADMIN_HELP_ID } },
)

const showStacktrace = ref(false)
const showContext = ref(false)

const assigneeId = ref('')
const users = ref<Array<{ id: string; email: string; display_name: string }>>([])

const events = ref<ErrorEventDetail[]>([])
const eventsTotal = ref(0)
const eventsLoading = ref(false)
const eventsOffset = ref(0)
const eventsLimit = 20

async function fetchDetail(): Promise<ErrorGroupDetail> {
  if (!isInstanceScope) return fetchErrorGroup(errorId)
  return fetchInstanceErrorGroup(errorId)
}

const { data: groupData, loading, error, load: loadDetail } = useDataFetch(
  () => fetchDetail().then(
    d => ({ data: d }),
    e => ({ error: { detail: `${t('views.AdminErrorDetailView.failed_to_load_error_group')} ${formatApiError(e)}` } }),
  ),
  { initialValue: null as ErrorGroupDetail | null }
)

const group = computed(() => groupData.value ?? null)
const sampleEvent = computed(() => groupData.value?.sample_event ?? null)

// FAR-2304: one copy action for the canonical support reference. Built by the
// shared helper so it can never drift from the error list's reference.
const supportCopied = ref(false)

const supportReference = computed(() => {
  const g = groupData.value
  if (!g) return ''
  return buildErrorGroupSupportReference({
    groupId: g.id,
    fingerprint: g.fingerprint,
    level: g.level_peak,
    count: g.count,
    source: g.sample_event?.source ?? null,
  })
})

async function copySupportReference() {
  const text = supportReference.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    supportCopied.value = true
    setTimeout(() => { supportCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy support reference', e)
  }
}

watch(() => groupData.value, (g) => {
  if (g) assigneeId.value = g.assigned_to || ''
})

function goBack() {
  router.push(isInstanceScope ? { path: '/admin/errors', query: { scope: 'instance' } } : '/admin/errors')
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
    const data: ErrorEventListResponse = isInstanceScope
      ? await fetchInstanceErrorGroupEvents(errorId, { limit: eventsLimit, offset: eventsOffset.value })
      : await fetchErrorGroupEvents(errorId, { limit: eventsLimit, offset: eventsOffset.value })
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

void loadEvents(0)
// The assignee picker lives in the tenant-only actions card; the instance
// view is read-only, so skip the admin user lookup there entirely.
//
// FAR-2374: `GET /api/v1/admin/users` is admin-only. Firing it for a viewer
// only buys a 403 and an empty dropdown, so the lookup waits for the
// caller-scoped permission read to grant `errors.manage` — and never happens
// at all when it does not (a viewer's request is skipped, not attempted and
// swallowed).
watch(
  canManageErrors,
  (allowed) => {
    if (allowed && !isInstanceScope) void loadUsers()
  },
  { immediate: true },
)
</script>
