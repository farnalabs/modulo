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
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <EmptyState
        v-if="filteredGates.length === 0"
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
        <span class="h-4 w-4 flex-shrink-0" aria-hidden="true"></span>
        <div class="w-24 flex-shrink-0">{{ $t('views.SettingsHitlReviewView.status_label') }}</div>
        <div class="min-w-0 flex-[2]">{{ $t('views.SettingsHitlReviewView.pipeline_label') }}</div>
        <div class="min-w-0 flex-[2]">{{ $t('views.SettingsHitlReviewView.node_label') }}</div>
        <div class="min-w-0 flex-1">{{ $t('views.SettingsHitlReviewView.assignee_label') }}</div>
        <span class="w-40 flex-shrink-0 text-right">{{ $t('views.SettingsHitlReviewView.created_label') }}</span>
      </div>
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
            <p class="truncate text-sm text-muted-foreground" data-testid="hitl-review-node-name">
              <!-- FAR-727: the server-resolved gate label (the edge's human
                   name) — the raw gate-id short ID renders only when the gate
                   config carries no label at all. -->
              <span v-if="gate.label">{{ gate.label }}</span>
              <span v-else class="font-mono text-xs">{{ shortId(gate.gate_id) }}</span>
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
          role="status"
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
import { ref, computed, onMounted, onUnmounted } from 'vue'
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
import { formatDateShortWithTime } from '../lib/formatDate'
import { shortId } from '../utils/format'
import Select from 'primevue/select'

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

const { loading, error, data: gates, load: loadGates } = useDataFetch<GateItem[]>(
  async () => {
    // Reads the CURRENT status/page refs on every load: filter and page
    // changes re-invoke loadGates(), so each fetch reflects the latest state.
    const res = await api.GET('/api/v1/hitl/gates', {
      params: { query: { status: serverStatusFor(statusFilter.value), page: page.value, page_size: PAGE_SIZE } },
    })
    if (res.error) return { error: res.error }
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
