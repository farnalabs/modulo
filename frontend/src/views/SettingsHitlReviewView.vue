<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.SettingsHitlReviewView.title')" :subtitle="$t('views.SettingsHitlReviewView.subtitle')" />
    <FilterBar
      :search="{ placeholder: $t('views.SettingsHitlReviewView.search_placeholder') }"
      :search-value="searchQuery"
      :filters="[
        { key: 'status', label: $t('views.SettingsHitlReviewView.status_label'), options: [
          { value: 'pending', label: $t('views.SettingsHitlReviewView.status_pending') },
          { value: 'claimed', label: $t('views.SettingsHitlReviewView.status_claimed') },
          { value: 'approved', label: $t('views.SettingsHitlReviewView.status_approved') },
          { value: 'rejected', label: $t('views.SettingsHitlReviewView.status_rejected') },
        ]},
      ]"
      :filter-values="{ status: statusFilter }"
      @update:search="searchQuery = $event"
      @update:filter="(key, value) => { if (key === 'status') { statusFilter = value; page = 1; loadGates() } }"
    >
      <template #after>
        <div class="flex flex-wrap items-center gap-2">
          <Select
  class="w-full sm:w-auto"
  :aria-label="$t('views.SettingsHitlReviewView.pipeline_label')"
  v-model="pipelineFilter"
  @update:model-value="loadGates"
  :placeholder="$t('views.SettingsHitlReviewView.all_pipelines')"
  data-testid="hitl-review-pipeline-select"
  :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          <input :aria-label="$t('views.SettingsHitlReviewView.date_label')"
            v-model="dateFrom"
            type="date"
            data-testid="hitl-review-date-from"
            class="w-full sm:w-auto rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadGates"
          />
          <input :aria-label="$t('views.SettingsHitlReviewView.date_label')"
            v-model="dateTo"
            type="date"
            data-testid="hitl-review-date-to"
            class="w-full sm:w-auto rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadGates"
          />
        </div>
      </template>
    </FilterBar>
    <!-- FAR-861: bulk action bar — visible when 1+ gates are selected. Shows
         selection count, bulk claim, bulk reject (with shared reason input),
         and per-gate outcome reporting. -->
    <div
      v-if="someSelected"
      data-testid="hitl-review-bulk-bar"
      role="status"
      class="mb-4 rounded-lg border border-primary/30 bg-primary/5 p-3"
    >
      <div class="flex flex-wrap items-center gap-3">
        <span class="text-sm font-medium">
          {{ $t('views.SettingsHitlReviewView.bulk_selected', { count: selectedCount }) }}
        </span>
        <button
          type="button"
          :disabled="bulkClaiming || bulkRejecting"
          data-testid="hitl-review-bulk-claim"
          class="rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          @click="bulkClaim"
        >
          {{ bulkClaiming ? $t('views.SettingsHitlReviewView.bulk_claiming') : $t('views.SettingsHitlReviewView.bulk_claim') }}
        </button>
        <button
          type="button"
          :disabled="bulkClaiming || bulkRejecting || showBulkRejectInput"
          data-testid="hitl-review-bulk-reject"
          class="rounded-lg bg-destructive px-3 py-1.5 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
          @click="openBulkReject"
        >
          {{ $t('views.SettingsHitlReviewView.bulk_reject') }}
        </button>
        <button
          type="button"
          :disabled="bulkClaiming || bulkRejecting"
          data-testid="hitl-review-bulk-clear"
          class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
          @click="clearSelection"
        >
          {{ $t('views.SettingsHitlReviewView.bulk_clear') }}
        </button>
      </div>
      <!-- Bulk reject shared reason input -->
      <div v-if="showBulkRejectInput" class="mt-3 flex flex-wrap items-end gap-3">
        <label class="flex-1">
          <span class="mb-1 block text-sm font-medium">{{ $t('views.SettingsHitlReviewView.bulk_reject_reason_label') }}</span>
          <textarea
            v-model="bulkRejectReason"
            rows="2"
            data-testid="hitl-review-bulk-reject-reason"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :placeholder="$t('views.SettingsHitlReviewView.bulk_reject_reason_placeholder')"
          />
        </label>
        <div class="flex gap-2">
          <button
            type="button"
            :disabled="bulkRejecting"
            data-testid="hitl-review-bulk-reject-confirm"
            class="rounded-lg bg-destructive px-3 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
            @click="confirmBulkReject"
          >
            {{ bulkRejecting ? $t('views.SettingsHitlReviewView.bulk_rejecting') : $t('views.SettingsHitlReviewView.bulk_reject_confirm') }}
          </button>
          <button
            type="button"
            :disabled="bulkRejecting"
            data-testid="hitl-review-bulk-reject-cancel"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm hover:bg-accent hover:text-accent-foreground disabled:opacity-50"
            @click="cancelBulkReject"
          >
            {{ $t('views.SettingsHitlReviewView.bulk_reject_cancel') }}
          </button>
        </div>
      </div>
      <!-- Per-gate outcome report -->
      <div
        v-if="bulkOutcomes"
        data-testid="hitl-review-bulk-outcomes"
        class="mt-3 space-y-1 text-sm"
      >
        <p class="font-medium">{{ $t('views.SettingsHitlReviewView.bulk_outcomes_title') }}</p>
        <div v-for="outcome in bulkOutcomes" :key="outcome.key" class="flex items-start gap-2">
          <span
            class="mt-0.5 inline-block h-2 w-2 flex-shrink-0 rounded-full"
            :class="{
              'bg-success': outcome.status === 'succeeded',
              'bg-muted-foreground': outcome.status.startsWith('skipped'),
              'bg-destructive': outcome.status === 'failed',
            }"
          />
          <span>
            <span class="font-mono text-xs">{{ shortId(outcome.key.split(':')[1]) }}</span>
            — {{ $t(`views.SettingsHitlReviewView.bulk_outcome_${outcome.status.replace(/-/g, '_')}`) }}
            <span v-if="outcome.error" class="text-destructive">{{ outcome.error }}</span>
          </span>
        </div>
        <button
          type="button"
          class="mt-1 text-xs text-muted-foreground hover:text-foreground"
          data-testid="hitl-review-bulk-outcomes-dismiss"
          @click="dismissBulkOutcomes"
        >
          {{ $t('views.SettingsHitlReviewView.bulk_outcomes_dismiss') }}
        </button>
      </div>
    </div>
    <div class="flex items-center gap-1 text-xs text-muted-foreground">
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      {{ $t('views.SettingsHitlReviewView.auto_refresh', { seconds: refreshCountdown }) }}
    </div>
    <!-- FAR-612: claim failures render at VIEW level so the immediate list
         refresh below (which drops terminal-run / already-decided gates) can
         never erase the message before it renders. Cleared on the next
         successful action, via the dismiss button, or after 10s. -->
    <ErrorAlert
      v-if="claimFailureBanner"
      data-testid="hitl-review-claim-failure-banner"
      class="mb-4"
      :message="claimFailureBanner"
      :retryable="false"
      :on-dismiss="clearClaimFailureBanner"
      :dismiss-label="$t('views.SettingsHitlReviewView.dismiss')"
    />
    <LoadingSpinner v-if="loading" />
    <!-- FAR-768: an API failure is NOT an empty queue — surfacing "No pending
         HITL gates" during an incident would mislead operators. -->
    <div
      v-else-if="error"
      data-testid="hitl-review-fetch-error"
      class="rounded-lg border border-ink-700 bg-ink-800 p-8 text-center"
    >
      <p class="text-ink-50 font-semibold">{{ $t('views.SettingsHitlReviewView.error_state') }}</p>
      <button
        type="button"
        data-testid="hitl-review-retry"
        class="mt-4 rounded-lg border border-ink-700 bg-ink-800 px-3 py-1.5 text-sm text-ink-300 transition-colors hover:bg-ink-700 hover:text-ink-100"
        @click="loadGates"
      >
        {{ $t('views.SettingsHitlReviewView.error_retry') }}
      </button>
    </div>
    <template v-else>
      <EmptyState
        v-if="fetched && filteredGates.length === 0"
        :title="$t('views.SettingsHitlReviewView.empty_title')"
        :description="$t('views.SettingsHitlReviewView.empty_description')"
      />
      <div v-else>
      <!-- FAR-727: column labels for the gate rows below. The rows are
           interactive cards (button + expandable detail panel), so this is a
           flex header mirroring the row layout rather than a <table> — styled
           to match the shared DataTable thead. -->
      <div
        data-testid="hitl-review-column-headers"
        class="flex items-center gap-4 px-4 pb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground"
      >
        <label class="h-4 w-4 flex-shrink-0">
          <input
            type="checkbox"
            :checked="allSelected"
            :aria-label="$t('views.SettingsHitlReviewView.bulk_select_all')"
            data-testid="hitl-review-select-all"
            class="h-4 w-4 rounded border-input accent-primary"
            @change="toggleSelectAll"
          />
        </label>
        <div class="w-24 flex-shrink-0">{{ $t('views.SettingsHitlReviewView.status_label') }}</div>
        <div class="min-w-0 flex-[2]">{{ $t('views.SettingsHitlReviewView.pipeline_label') }}</div>
        <div class="min-w-0 flex-[2]">{{ $t('views.SettingsHitlReviewView.node_label') }}</div>
        <div class="min-w-0 flex-1">{{ $t('views.SettingsHitlReviewView.assignee_label') }}</div>
        <span class="w-40 flex-shrink-0 text-right">{{ $t('views.SettingsHitlReviewView.created_label') }}</span>
      </div>
      <!-- FAR-858: visually-hidden h2 so the h3 gate labels in HitlGateCard
           don't skip a heading level (page h1 → h2 → h3). -->
      <h2 class="sr-only">{{ $t('views.SettingsHitlReviewView.title') }}</h2>
      <div class="space-y-2">
      <div
        v-for="gate in filteredGates"
        :key="gate.gate_id + gate.run_id"
        class="rounded-lg border bg-card shadow-sm"
      >
        <button
          type="button"
          data-testid="hitl-review-toggle-expand"
          class="flex w-full items-center gap-4 p-4 text-left"
          :class="{ 'border-b': expandedKey === expandKey(gate) }"
          @click="toggleExpand(gate)"
        >
          <label class="h-4 w-4 flex-shrink-0">
            <input
              type="checkbox"
              :checked="isSelected(gate)"
              :aria-label="$t('views.SettingsHitlReviewView.bulk_select_gate', { id: shortId(gate.gate_id) })"
              data-testid="hitl-review-row-checkbox"
              class="h-4 w-4 rounded border-input accent-primary"
              @click.stop
              @change="toggleSelect(gate)"
            />
          </label>
          <svg
            class="h-4 w-4 flex-shrink-0 text-muted-foreground transition-transform"
            :class="{ 'rotate-90': expandedKey === expandKey(gate) }"
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <path d="m9 18 6-6-6-6" />
          </svg>
          <div class="w-24 flex-shrink-0">
            <span :class="statusBadgeClass(gateStatus(gate))">
              {{ gateStatus(gate) }}
            </span>
          </div>
          <div class="min-w-0 flex-[2]">
            <p class="truncate text-sm font-medium" data-testid="hitl-review-pipeline-name">{{ pipelineDisplayName(gate) }}</p>
          </div>
          <div class="min-w-0 flex-[2]">
            <p class="truncate text-sm font-medium text-foreground" data-testid="hitl-review-node-name">
              {{ gate.label || shortId(gate.gate_id) }}
            </p>
            <p v-if="gateDescriptionSnippet(gate)" class="mt-0.5 truncate text-xs text-muted-foreground" data-testid="hitl-review-snippet">
              {{ gateDescriptionSnippet(gate) }}
            </p>
          </div>
          <div class="min-w-0 flex-1">
            <p class="truncate text-xs text-muted-foreground">
              {{ gate.claimed_by ? (gate.claimed_by_name || gate.claimed_by) : $t('views.SettingsHitlReviewView.unassigned') }}
            </p>
          </div>
          <span class="w-40 flex-shrink-0 text-right text-xs text-muted-foreground">
            {{ formatDate(gate.claimed_at || gate.created_at || '') }}
          </span>
        </button>
        <div v-if="expandedKey === expandKey(gate)" class="border-t p-4">
          <!-- No @claimed handler (FAR-686): a loadGates() refetch flips the
               page-level `loading` flag, unmounting this list branch and with
               it the card's in-session claim token. The card already shows
               approve/reject immediately after claiming; the row badge
               converges on the next auto-refresh. @decided is safe: the row
               leaves the list, so there is no in-card state to lose. -->
          <HitlGateCard
            :gate="gate"
            show-run-link
            @claimed="clearClaimFailureBanner"
            @claim-failed="onClaimFailed"
            @decided="onGateDecided"
          />
        </div>
      </div>
      </div>
      </div>
      <!-- FAR-692: server-side pagination over /hitl/gates. Only rendered when
           there is more than one page; the page indicator uses role="status" so
           a page change is announced. No shared pagination component exists in
           components/shared/, so this minimal inline pager is view-local. -->
      <nav
        v-if="totalGates > PAGE_SIZE"
        class="flex items-center justify-center gap-3 py-2"
        :aria-label="$t('views.SettingsHitlReviewView.pagination_label')"
      >
        <button
          type="button"
          data-testid="hitl-review-prev-page"
          class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="page <= 1"
          :aria-label="$t('views.SettingsHitlReviewView.prev_page')"
          @click="goToPage(page - 1)"
        >
          {{ $t('views.SettingsHitlReviewView.prev_page') }}
        </button>
        <span
          data-testid="hitl-review-page-indicator"
          aria-live="polite"
          class="text-sm text-muted-foreground"
        >
          {{ $t('views.SettingsHitlReviewView.page_indicator', { page, total: totalPages }) }}
        </span>
        <button
          type="button"
          data-testid="hitl-review-next-page"
          class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="page >= totalPages"
          :aria-label="$t('views.SettingsHitlReviewView.next_page')"
          @click="goToPage(page + 1)"
        >
          {{ $t('views.SettingsHitlReviewView.next_page') }}
        </button>
      </nav>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { api } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import HitlGateCard from '../components/hitl/HitlGateCard.vue'
import { usePlanStore } from '../stores/planStore'
import { useHitlGateState } from '../composables/useHitlGateState'
import { formatApiError } from '../lib/api/formatError'
import { claimFailureMessage } from '../lib/hitlClaimFailure'
import { formatDateShortWithTime } from '../lib/formatDate'
import { shortId } from '../utils/format'
import Select from '../components/shared/AppSelect.vue'

const planStore = usePlanStore()
const { t } = useI18n()

interface GateItem {
  run_id: string
  gate_id: string
  pipeline_id: string
  /** FAR-727: server-resolved pipeline name (null when the pipeline row is gone). */
  pipeline_name?: string | null
  /** FAR-727: server-resolved human label from the snapshot's hitl_gate_config. */
  label?: string | null
  claimed_by: string | null
  /** FAR-691: claimant's human-readable display name (server-resolved). */
  claimed_by_name?: string | null
  /** FAR-691: server-side stamp — is the claimant the caller? */
  claimed_by_me?: boolean
  claimed_at: string | null
  expires_at: string | null
  decision: string | null
  decision_at: string | null
  created_at?: string
  team_scope?: string
  /** FAR-613: the gate config's human description (null for legacy gates). */
  description?: string | null
  /** FAR-613: the fire-time briefing bundle persisted on the claim row. */
  context?: Record<string, unknown> | null
}

interface PipelineItem {
  id: string
  name: string
}

// FAR-692: the review page lists gates in EVERY state via GET /api/v1/hitl/gates
// (server-side status filter + pagination). The FilterBar's empty selection maps
// to the server's `undecided` default, preserving today's queue view.
type ServerGateStatus = 'undecided' | 'pending' | 'claimed' | 'approved' | 'rejected' | 'all'

const PAGE_SIZE = 25

function serverStatusFor(filter: string): ServerGateStatus {
  if (!filter) return 'undecided'
  return filter as ServerGateStatus
}

// FAR-692: server-side pagination state. totalGates is stamped from each
// /hitl/gates response; the pager renders only when it exceeds PAGE_SIZE.
// NB: every ref the fetch closure reads (statusFilter/page/totalGates) must be
// declared BEFORE useDataFetch — vue-query invokes the fetcher during setup.
const page = ref(1)
const totalGates = ref(0)
const totalPages = computed(() => Math.max(1, Math.ceil(totalGates.value / PAGE_SIZE)))

const statusFilter = ref('')
const pipelineFilter = ref('')
const searchQuery = ref('')
const dateFrom = ref('')
const dateTo = ref('')

const { loading, error, data: gates, fetched, load: loadGates } = useDataFetch<GateItem[]>(
  async () => {
    // Reads the CURRENT status/page refs on every load: filter and page
    // changes re-invoke loadGates(), so each fetch reflects the latest state.
    const res = await api.GET('/api/v1/hitl/gates', {
      params: { query: { status: serverStatusFor(statusFilter.value), page: page.value, page_size: PAGE_SIZE } },
    })
    // FAR-768 regression: a missing envelope is a FAILURE, not an empty queue.
    // The api client returns { error: undefined, data: undefined } for a
    // response with no envelope (e.g. an unrecovered 401, a 5xx with an empty
    // body). Such a response must surface the error state — letting it fall
    // through to { data: [] } would mislead operators into thinking the review
    // queue was empty during an outage.
    if (res.error || !res.data || res.response?.ok === false) {
      return { error: res.error ?? { detail: 'Failed to load HITL gates' } }
    }
    const payload = (res.data as any) || {}
    totalGates.value = payload.total ?? 0
    return { data: ((payload.items || []) as any[]).map((g) => ({
      ...g,
      run_id: String(g.run_id),
      pipeline_id: String(g.pipeline_id),
      claimed_by: g.claimed_by ? String(g.claimed_by) : null,
    })) }
  },
  // silentRefetch (FAR-691): the 30s auto-refresh and the filter/date/page
  // refetches must not flip `loading` — the list branch stays mounted, so an
  // expanded card (and its notes textarea focus) survives every refetch.
  // The initial load still shows the spinner.
  { initialValue: [] as GateItem[], silentRefetch: true }
)

const { load: loadPipelines, data: pipelines } = useDataFetch<PipelineItem[]>(
  async () => {
    const res = await api.GET('/api/v1/pipelines')
    if (res.error) return { error: res.error }
    return { data: (res.data as any)?.items || [] }
  },
  { immediate: false, initialValue: [] as PipelineItem[] }
)

function goToPage(target: number) {
  if (target < 1 || target > totalPages.value || target === page.value) return
  page.value = target
  loadGates()
}

const expandedKey = ref<string | null>(null)
// FAR-612: view-level claim-failure banner. Cards may unmount on refresh
// (terminal-run / already-decided gates drop out of the pending list), so a
// claim failure is hoisted from the card's ``claim-failed`` emit and rendered
// at view level where the refresh can never erase it before it renders.
// Cleared on the next successful claim/decision, via the dismiss button, or
// after 10s.
const claimFailureBanner = ref<string | null>(null)
let claimBannerTimer: ReturnType<typeof setTimeout> | null = null

function showClaimFailureBanner(text: string) {
  claimFailureBanner.value = text
  if (claimBannerTimer) clearTimeout(claimBannerTimer)
  // Failure banners persist longer than the 5s success toasts (the operator
  // needs time to read why the claim failed) but never stay forever.
  claimBannerTimer = setTimeout(() => { claimFailureBanner.value = null }, 10000)
}

function clearClaimFailureBanner() {
  claimFailureBanner.value = null
  if (claimBannerTimer) { clearTimeout(claimBannerTimer); claimBannerTimer = null }
}


const refreshInterval = ref(30000)
const refreshCountdown = ref(30)
let refreshTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null
let refreshInFlight = false
let disposed = false

function expandKey(gate: GateItem): string {
  return `${gate.run_id}:${gate.gate_id}`
}

function gateStatus(gate: GateItem): string {
  if (gate.decision === 'approved') return 'approved'
  if (gate.decision === 'rejected') return 'rejected'
  if (gate.claimed_by) return 'claimed'
  return 'pending'
}

function statusBadgeClass(status: string): string {
  const classMap: Record<string, string> = {
    pending: 'badge badge-status-pending',
    claimed: 'badge badge-context-purple',
    approved: 'badge badge-status-success',
    rejected: 'badge badge-status-destructive',
  }
  return classMap[status] ?? 'badge badge-context-slate'
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-'
  const d = new Date(dateStr)
  if (Number.isNaN(d.getTime())) return '-'
  return formatDateShortWithTime(d)
}

function pipelineName(pipelineId: string): string {
  const p = pipelines.value.find(p => p.id === pipelineId)
  return p ? p.name : ''
}

// FAR-727: the row must never render a bare ID prefix where a name belongs.
// The endpoint resolves `pipeline_name` server-side (join at query time), so
// prefer it; the cached /pipelines list is a legacy-payload fallback. The
// short ID renders only when both fail — the pipeline row is gone (deleted).
function pipelineDisplayName(gate: GateItem): string {
  if (gate.pipeline_name) return gate.pipeline_name
  const cached = pipelineName(gate.pipeline_id)
  if (cached) return cached
  return t('views.SettingsHitlReviewView.deleted_pipeline_fallback', { id: shortId(gate.pipeline_id) })
}

/** FAR-858: one-line snippet for the collapsed row — gate description or condition-result value. */
function gateDescriptionSnippet(gate: GateItem): string {
  let raw = ''
  if (gate.description && gate.description.trim()) {
    raw = gate.description
  } else {
    const ctx = gate.context
    if (ctx && typeof ctx === 'object' && !Array.isArray(ctx)) {
      const cr = (ctx as Record<string, unknown>).condition_result
      if (cr && typeof cr === 'object' && !Array.isArray(cr)) {
        const entry = cr as Record<string, unknown>
        if (typeof entry.value === 'string' && entry.value.trim()) raw = entry.value
      }
    }
  }
  // Cap at 120 chars so the full text is never exposed to the accessibility tree.
  const MAX_SNIPPET = 120
  return raw.length > MAX_SNIPPET ? raw.slice(0, MAX_SNIPPET) + '\u2026' : raw
}

function matchesPipeline(gate: GateItem): boolean {
  if (!pipelineFilter.value) return true
  return gate.pipeline_id === pipelineFilter.value
}

function matchesSearch(gate: GateItem): boolean {
  if (!searchQuery.value) return true
  const q = searchQuery.value.toLowerCase()
  const pName = pipelineDisplayName(gate).toLowerCase()
  return pName.includes(q) || gate.gate_id.toLowerCase().includes(q)
}

function matchesDate(gate: GateItem): boolean {
  if (!dateFrom.value && !dateTo.value) return true
  const ts = gate.created_at || gate.claimed_at
  if (!ts) return false
  const created = new Date(ts)
  if (Number.isNaN(created.getTime())) return false
  if (dateFrom.value && created < new Date(dateFrom.value)) return false
  if (dateTo.value) {
    const to = new Date(dateTo.value)
    to.setHours(23, 59, 59, 999)
    if (created > to) return false
  }
  return true
}

const filteredGates = computed(() => {
  // Status is filtered SERVER-side now (FAR-692): the statusFilter param selects
  // the gate subset on /hitl/gates. Search/pipeline/date remain client-side
  // over the loaded page (documented limitation: search matches within the
  // current page only).
  return gates.value.filter(gate =>
    matchesPipeline(gate) && matchesSearch(gate) && matchesDate(gate))
})

// FAR-686: claim/decide logic lives inside HitlGateCard (shared with
// RunDetailView). The view only hoists the card's feedback: failures persist
// in the view-level banner (FAR-612), successes clear it and refresh the list.
// FAR-645 (rebased onto FAR-686): the claim-conflict discrimination by the
// backend's machine-readable problem type (urn:problem:modulo:<type>) lives in
// the shared lib/hitlClaimFailure module, used by both the card and the bulk
// path below so the two cannot drift.

async function onClaimFailed(payload: { text: string }) {
  showClaimFailureBanner(payload.text)
  // FAR-612: the list on screen is stale after a failed claim (another
  // reviewer took it, the run moved on, the gate was decided). Re-fetch
  // immediately so the list reflects reality instead of waiting for the 30s
  // auto-refresh. Refresh failure must not mask the banner above.
  try {
    await loadGates()
  } catch {
    // Ignore — the claim-failure banner above already explains what happened.
  }
}

async function onGateDecided() {
  clearClaimFailureBanner()
  await loadGates()
}

// ---- Bulk selection + bulk actions (FAR-861) ----
const selectedKeys = ref<Set<string>>(new Set())
const bulkClaiming = ref(false)
const bulkRejecting = ref(false)
const bulkRejectReason = ref('')
const showBulkRejectInput = ref(false)

interface BulkOutcome {
  key: string
  status:
    | 'succeeded'
    | 'skipped-already-decided'
    | 'skipped-claimed-by-other'
    | 'skipped-claimed-by-you'
    | 'skipped-claim-expired'
    | 'skipped-pending'
    | 'failed'
  error?: string
}

const bulkOutcomes = ref<BulkOutcome[] | null>(null)

function gateKey(gate: GateItem): string {
  return `${gate.run_id}:${gate.gate_id}`
}

const allSelected = computed(() => {
  if (filteredGates.value.length === 0) return false
  return filteredGates.value.every(g => selectedKeys.value.has(gateKey(g)))
})

const someSelected = computed(() => selectedKeys.value.size > 0)

const selectedCount = computed(() => selectedKeys.value.size)

// FAR-861: keep the selection in sync with the loaded page. The 30s
// auto-refresh and the post-action refetches can drop gates (decided / terminal
// run) that the operator had selected; without pruning they keep counting
// toward the bulk bar and inflate the "N gates selected" label.
watch(gates, (rows) => {
  if (selectedKeys.value.size === 0) return
  const visible = new Set(rows.map(gateKey))
  const next = new Set([...selectedKeys.value].filter(key => visible.has(key)))
  if (next.size !== selectedKeys.value.size) selectedKeys.value = next
})

function toggleSelectAll() {
  if (allSelected.value) {
    selectedKeys.value = new Set()
  } else {
    selectedKeys.value = new Set(filteredGates.value.map(g => gateKey(g)))
  }
}

function toggleSelect(gate: GateItem) {
  const key = gateKey(gate)
  const next = new Set(selectedKeys.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  selectedKeys.value = next
}

function isSelected(gate: GateItem): boolean {
  return selectedKeys.value.has(gateKey(gate))
}

function clearSelection() {
  selectedKeys.value = new Set()
  bulkOutcomes.value = null
  showBulkRejectInput.value = false
  bulkRejectReason.value = ''
}

function dismissBulkOutcomes() {
  bulkOutcomes.value = null
}

async function bulkClaim() {
  bulkClaiming.value = true
  bulkOutcomes.value = null
  const outcomes: BulkOutcome[] = []
  const selected = filteredGates.value.filter(g => selectedKeys.value.has(gateKey(g)))
  let anyFailed = false

  for (const gate of selected) {
    const key = gateKey(gate)
    const status = gateStatus(gate)

    if (status === 'approved' || status === 'rejected') {
      outcomes.push({ key, status: 'skipped-already-decided' })
      continue
    }
    if (status === 'claimed') {
      // A claimed gate cannot be re-claimed; distinguish the operator's own
      // claim (e.g. this session lost the token) from another reviewer's so
      // the outcome report is not misleading.
      outcomes.push({ key, status: gate.claimed_by_me ? 'skipped-claimed-by-you' : 'skipped-claimed-by-other' })
      continue
    }

    try {
      const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/claim', {
        params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
        body: { expiry_minutes: 15 },
      })
      if (err) {
        outcomes.push({ key, status: 'failed', error: claimFailureMessage(err, t) })
        anyFailed = true
      } else if (data) {
        const d = data as { claim_token: string; expires_at: string }
        const gateState = useHitlGateState(gate.run_id, gate.gate_id)
        gateState.setClaimToken(d.claim_token)
        outcomes.push({ key, status: 'succeeded' })
      }
    } catch (e: unknown) {
      outcomes.push({ key, status: 'failed', error: claimFailureMessage(e, t) })
      anyFailed = true
    }
  }

  bulkOutcomes.value = outcomes
  bulkClaiming.value = false

  if (anyFailed) {
    const failedMessages = outcomes.filter(o => o.status === 'failed').map(o => o.error).join('; ')
    showClaimFailureBanner(t('views.SettingsHitlReviewView.bulk_claim_partial_failure', { errors: failedMessages }))
  }

  try {
    await loadGates()
  } catch {
    // Ignore — the banner above already explains what happened.
  }
}

function openBulkReject() {
  showBulkRejectInput.value = true
  bulkRejectReason.value = ''
}

function cancelBulkReject() {
  showBulkRejectInput.value = false
  bulkRejectReason.value = ''
}

async function confirmBulkReject() {
  bulkRejecting.value = true
  bulkOutcomes.value = null
  const selected = filteredGates.value.filter(g => selectedKeys.value.has(gateKey(g)))
  const outcomes: BulkOutcome[] = []
  let anyFailed = false
  const reason = bulkRejectReason.value.trim() || t('hitl.gate.rejected_by_reviewer')

  for (const gate of selected) {
    const key = gateKey(gate)
    const gateState = useHitlGateState(gate.run_id, gate.gate_id)
    const token = gateState.claimToken.value

    if (gate.decision === 'approved' || gate.decision === 'rejected') {
      outcomes.push({ key, status: 'skipped-already-decided' })
      continue
    }
    if (!gate.claimed_by) {
      outcomes.push({ key, status: 'skipped-pending' })
      continue
    }
    if (gate.claimed_by && !gate.claimed_by_me && !token) {
      outcomes.push({ key, status: 'skipped-claimed-by-other' })
      continue
    }
    if (!token) {
      // The gate is claimed by this operator, but this browser session has no
      // claim token (a reload drops it). It is not "claimed by another
      // reviewer" -- the operator must claim it again before rejecting.
      outcomes.push({ key, status: 'skipped-claim-expired' })
      continue
    }

    try {
      const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/reject', {
        params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
        body: { claim_token: token, reason },
      })
      if (err) {
        outcomes.push({ key, status: 'failed', error: `${t('hitl.gate.reject_failed')} ${formatApiError(err)}` })
        anyFailed = true
      } else {
        gateState.clear()
        outcomes.push({ key, status: 'succeeded' })
      }
    } catch (e: unknown) {
      outcomes.push({ key, status: 'failed', error: `${t('hitl.gate.reject_failed')} ${formatApiError(e)}` })
      anyFailed = true
    }
  }

  bulkOutcomes.value = outcomes
  bulkRejecting.value = false
  showBulkRejectInput.value = false
  bulkRejectReason.value = ''

  if (anyFailed) {
    const failedMessages = outcomes.filter(o => o.status === 'failed').map(o => o.error).join('; ')
    showClaimFailureBanner(t('views.SettingsHitlReviewView.bulk_reject_partial_failure', { errors: failedMessages }))
  }

  try {
    await loadGates()
  } catch {
    // Ignore — the banner above already explains what happened.
  }
}

function toggleExpand(gate: GateItem) {
  const key = expandKey(gate)
  if (expandedKey.value === key) {
    expandedKey.value = null
  } else {
    expandedKey.value = key
  }
}

function startAutoRefresh() {
  refreshTimer = setInterval(() => {
    if (disposed || refreshInFlight) return
    refreshInFlight = true
    loadGates().finally(() => {
      if (disposed) return
      refreshInFlight = false
      refreshCountdown.value = Math.floor(refreshInterval.value / 1000)
    })
  }, refreshInterval.value)
  countdownTimer = setInterval(() => {
    if (disposed) return
    if (refreshCountdown.value > 0) refreshCountdown.value--
  }, 1000)
}

function stopAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null }
}

onMounted(async () => {
  planStore.fetchPlan()
  await loadPipelines()
  startAutoRefresh()
})

onUnmounted(() => {
  disposed = true
  stopAutoRefresh()
  clearClaimFailureBanner()
})
</script>
