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
      @update:filter="(key, value) => { if (key === 'status') { statusFilter = value; loadGates() } }"
    >
      <template #after>
        <div class="flex items-center gap-2">
          <Select
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
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadGates"
          />
          <input :aria-label="$t('views.SettingsHitlReviewView.date_label')"
            v-model="dateTo"
            type="date"
            data-testid="hitl-review-date-to"
            class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="loadGates"
          />
        </div>
      </template>
    </FilterBar>
    <div class="flex items-center gap-1 text-xs text-muted-foreground">
      <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      {{ $t('views.SettingsHitlReviewView.auto_refresh', { seconds: refreshCountdown }) }}
    </div>
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <EmptyState
        v-if="filteredGates.length === 0"
        :title="$t('views.SettingsHitlReviewView.empty_title')"
        :description="$t('views.SettingsHitlReviewView.empty_description')"
      />
      <div v-else class="space-y-2">
      <div
        v-for="gate in filteredGates"
        :key="gate.gate_id + gate.run_id"
        class="rounded-lg border bg-card shadow-sm"
      >
        <button
          type="button"
          data-testid="hitl-review-toggle-expand"
          class="flex w-full cursor-pointer items-center gap-4 p-4 text-left"
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
          <span :class="statusBadgeClass(gateStatus(gate))">
            {{ gateStatus(gate) }}
          </span>
          <div class="min-w-0 flex-[2]">
            <p class="truncate text-sm font-medium">{{ pipelineName(gate.pipeline_id) }}<span v-if="!pipelineName(gate.pipeline_id)" class="font-mono text-xs">{{ shortId(gate.pipeline_id) }}</span></p>
          </div>
          <div class="min-w-0 flex-[2]">
            <p class="truncate text-sm text-muted-foreground">
              <span class="font-mono text-xs">{{ shortId(gate.gate_id) }}</span>
            </p>
          </div>
          <div class="min-w-0 flex-1">
            <p class="truncate text-xs text-muted-foreground">
              {{ gate.claimed_by ? $t('views.SettingsHitlReviewView.assigned_to', { user: gate.claimed_by }) : $t('views.SettingsHitlReviewView.unassigned') }}
            </p>
          </div>
          <span class="flex-shrink-0 text-xs text-muted-foreground">
            {{ formatDate(gate.claimed_at || gate.created_at || '') }}
          </span>
        </button>
        <div v-if="expandedKey === expandKey(gate)" class="border-t p-4">
          <div v-if="actionLoading[expandKey(gate)]" class="flex items-center justify-center py-8">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
          <template v-else>
            <div class="grid grid-cols-2 gap-6">
              <div>
                <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.SettingsHitlReviewView.claim_metadata') }}</h3>
                <div class="space-y-1 text-sm">
                  <div class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.run_id') }}</span>
                    <span class="font-mono text-xs">{{ shortId(gate.run_id) }}</span>
                  </div>
                  <div class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.node_label') }}</span>
                    <span class="font-mono text-xs">{{ shortId(gate.gate_id) }}</span>
                  </div>
                  <div class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.pipeline_label') }}</span>
                    <span>{{ pipelineName(gate.pipeline_id) }}<span v-if="!pipelineName(gate.pipeline_id)" class="font-mono text-xs">{{ shortId(gate.pipeline_id) }}</span></span>
                  </div>
                  <div class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.created_label') }}</span>
                    <span>{{ formatDate(gate.created_at || '') }}</span>
                  </div>
                  <div v-if="gate.claimed_at" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.claimed_label') }}</span>
                    <span>{{ formatDate(gate.claimed_at) }}</span>
                  </div>
                  <div v-if="gate.expires_at" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.expires_label') }}</span>
                    <span>{{ formatDate(gate.expires_at) }}</span>
                  </div>
                  <div v-if="gate.decision_at" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.decided_label') }}</span>
                    <span>{{ formatDate(gate.decision_at) }}</span>
                  </div>
                  <div v-if="gate.decision" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.decision_label') }}</span>
                    <span :class="gate.decision === 'approved' ? 'text-success' : 'text-destructive'">{{ gate.decision }}</span>
                  </div>
                  <div v-if="gate.claimed_by" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.assignees_label') }}</span>
                    <span>{{ gate.claimed_by }}</span>
                  </div>
                  <div v-if="gate.team_scope" class="flex justify-between">
                    <span class="text-muted-foreground">{{ $t('views.SettingsHitlReviewView.team_label') }}</span>
                    <span>{{ gate.team_scope }}</span>
                  </div>
                </div>
              </div>
              <div>
                <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">{{ $t('views.SettingsHitlReviewView.actions_label') }}</h3>
                <div class="space-y-3">
                  <div v-if="gateStatus(gate) === 'pending'">
                    <Button :disabled="claiming[expandKey(gate)]" class="w-full" data-testid="hitl-review-claim" @click="claimGate(gate)">
                      {{ claiming[expandKey(gate)] ? $t('views.SettingsHitlReviewView.claiming') : $t('views.SettingsHitlReviewView.claim_gate') }}
                    </Button>
                  </div>
                  <div v-if="gateStatus(gate) === 'claimed'">
                    <div class="space-y-2">
                      <textarea :aria-label="$t('views.SettingsHitlReviewView.review_notes')"
                        v-model="reviewNotes[expandKey(gate)]"
                        rows="2"
                        data-testid="hitl-review-notes"
                        class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        :placeholder="$t('views.SettingsHitlReviewView.review_notes')"
                      />
                      <div class="flex gap-2">
                        <button
                          type="button"
                          :disabled="Boolean(actioning[expandKey(gate)])"
                          data-testid="hitl-review-approve"
                          class="flex-1 rounded-lg bg-success px-4 py-2 text-sm font-medium text-white hover:bg-success/90 disabled:opacity-50"
                          @click="approveGate(gate)"
                        >
                          {{ actioning[expandKey(gate)] === 'approve' ? $t('views.SettingsHitlReviewView.approving') : $t('views.SettingsHitlReviewView.approve') }}
                        </button>
                        <button
                          type="button"
                          :disabled="Boolean(actioning[expandKey(gate)])"
                          data-testid="hitl-review-reject"
                          class="flex-1 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                          @click="rejectGate(gate)"
                        >
                          {{ actioning[expandKey(gate)] === 'reject' ? $t('views.SettingsHitlReviewView.rejecting') : $t('views.SettingsHitlReviewView.reject') }}
                        </button>
                      </div>
                    </div>
                  </div>
                  <div v-if="gateStatus(gate) === 'approved'" class="rounded-lg bg-success/10 p-3 text-sm text-success">
                    {{ $t('views.SettingsHitlReviewView.approved_banner') }}
                  </div>
                  <div v-if="gateStatus(gate) === 'rejected'" class="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
                    {{ $t('views.SettingsHitlReviewView.rejected_banner') }}
                  </div>
                  <div v-if="gateStatus(gate) === 'claimed' && claimTokens[expandKey(gate)]">
                    <div class="rounded-lg bg-muted p-3 text-xs">
                      <p class="font-medium text-muted-foreground mb-1">{{ $t('views.SettingsHitlReviewView.claim_token_label') }}</p>
                      <code class="break-all">{{ claimTokens[expandKey(gate)] }}</code>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="actionMessage[expandKey(gate)]" class="mt-4 text-sm" :class="actionMessage[expandKey(gate)]?.type === 'error' ? 'text-destructive' : 'text-success'">
              {{ actionMessage[expandKey(gate)]?.text }}
              </div>
            </template>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import { api } from '../lib/api/client'
import { formatApiError } from '../lib/api/formatError'
import PageHeader from '../components/shared/PageHeader.vue'
import FilterBar from '../components/shared/FilterBar.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import EmptyState from '../components/shared/EmptyState.vue'
import { usePlanStore } from '../stores/planStore'
import { formatDateShortWithTime } from '../lib/formatDate'
import { shortId } from '../utils/format'
import Button from 'primevue/button'
import Select from 'primevue/select'

const planStore = usePlanStore()
const { t } = useI18n()

interface GateItem {
  run_id: string
  gate_id: string
  pipeline_id: string
  claimed_by: string | null
  claimed_at: string | null
  expires_at: string | null
  decision: string | null
  decision_at: string | null
  created_at?: string
  team_scope?: string
}

interface PipelineItem {
  id: string
  name: string
}

const { loading, error, data: gates, load: loadGates } = useDataFetch<GateItem[]>(
  async () => {
    const res = await api.GET('/api/v1/hitl/pending')
    const raw = (res.data as any)?.gates || []
    return { data: raw.map((g: any) => ({
      ...g,
      run_id: String(g.run_id),
      pipeline_id: String(g.pipeline_id),
      claimed_by: g.claimed_by ? String(g.claimed_by) : null,
    })) }
  },
  { initialValue: [] as GateItem[] }
)

const { load: loadPipelines, data: pipelines } = useDataFetch<PipelineItem[]>(
  async () => {
    const res = await api.GET('/api/v1/pipelines')
    if (res.error) return { error: res.error }
    return { data: (res.data as any)?.items || [] }
  },
  { immediate: false, initialValue: [] as PipelineItem[] }
)

const statusFilter = ref('')
const pipelineFilter = ref('')
const searchQuery = ref('')
const dateFrom = ref('')
const dateTo = ref('')

const expandedKey = ref<string | null>(null)
const claimTokens = ref<Record<string, string>>({})
const claiming = ref<Record<string, boolean>>({})
const actioning = ref<Record<string, string | null>>({})
const actionLoading = ref<Record<string, boolean>>({})
const actionMessage = ref<Record<string, { type: string; text: string } | null>>({})
const reviewNotes = ref<Record<string, string>>({})

const refreshInterval = ref(30000)
const refreshCountdown = ref(30)
let refreshTimer: ReturnType<typeof setInterval> | null = null
let countdownTimer: ReturnType<typeof setInterval> | null = null
let refreshInFlight = false
let disposed = false
const actionMessageTimers: ReturnType<typeof setTimeout>[] = []

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

function matchesStatus(gate: GateItem): boolean {
  if (!statusFilter.value) return true
  return gateStatus(gate) === statusFilter.value
}

function matchesPipeline(gate: GateItem): boolean {
  if (!pipelineFilter.value) return true
  return gate.pipeline_id === pipelineFilter.value
}

function matchesSearch(gate: GateItem): boolean {
  if (!searchQuery.value) return true
  const q = searchQuery.value.toLowerCase()
  const pName = pipelineName(gate.pipeline_id).toLowerCase()
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
  return gates.value.filter(gate =>
    matchesStatus(gate) && matchesPipeline(gate) && matchesSearch(gate) && matchesDate(gate))
})

async function claimGate(gate: GateItem) {
  const key = expandKey(gate)
  claiming.value[key] = true
  actionMessage.value[key] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/claim', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { expiry_minutes: 15 },
    })
    if (err) {
      actionMessage.value[key] = {
        type: 'error',
        text: `${t('views.SettingsHitlReviewView.claim_failed')} ${formatApiError(err)}`,
      }
    } else if (data) {
      const d = data as any
      claimTokens.value[key] = d.claim_token
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], claimed_by: t('views.SettingsHitlReviewView.claimed_by_you'), claimed_at: new Date().toISOString(), expires_at: d.expires_at }
      }
      actionMessage.value[key] = { type: 'success', text: t('views.SettingsHitlReviewView.gate_claimed_you_can_now_approve_or_reject') }
      actionMessageTimers.push(setTimeout(() => { actionMessage.value[key] = null }, 5000))
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `${t('views.SettingsHitlReviewView.claim_failed')} ${formatApiError(e)}` }
  } finally {
    claiming.value[key] = false
  }
}

async function approveGate(gate: GateItem) {
  const key = expandKey(gate)
  const token = claimTokens.value[key]
  if (!token) {
    actionMessage.value[key] = { type: 'error', text: t('views.SettingsHitlReviewView.no_claim_token_claim_the_gate_first') }
    return
  }
  actioning.value[key] = 'approve'
  actionLoading.value[key] = true
  actionMessage.value[key] = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/approve', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: token, notes: reviewNotes.value[key] || null },
    })
    if (err) {
      actionMessage.value[key] = {
        type: 'error',
        text: `${t('views.SettingsHitlReviewView.approve_failed')} ${formatApiError(err)}`,
      }
    } else {
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], decision: 'approved', decision_at: new Date().toISOString() }
      }
      actionMessage.value[key] = { type: 'success', text: t('views.SettingsHitlReviewView.gate_approved_pipeline_resuming') }
      actionMessageTimers.push(setTimeout(() => { actionMessage.value[key] = null }, 5000))
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `${t('views.SettingsHitlReviewView.approve_failed')} ${formatApiError(e)}` }
  } finally {
    actioning.value[key] = null
    actionLoading.value[key] = false
  }
}

async function rejectGate(gate: GateItem) {
  const key = expandKey(gate)
  const token = claimTokens.value[key]
  if (!token) {
    actionMessage.value[key] = { type: 'error', text: t('views.SettingsHitlReviewView.no_claim_token_claim_the_gate_first') }
    return
  }
  const reason = reviewNotes.value[key] || t('views.SettingsHitlReviewView.rejected_by_reviewer')
  actioning.value[key] = 'reject'
  actionLoading.value[key] = true
  actionMessage.value[key] = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/reject', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: token, reason },
    })
    if (err) {
      actionMessage.value[key] = {
        type: 'error',
        text: `${t('views.SettingsHitlReviewView.reject_failed')} ${formatApiError(err)}`,
      }
    } else {
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], decision: 'rejected', decision_at: new Date().toISOString() }
      }
      actionMessage.value[key] = { type: 'success', text: t('views.SettingsHitlReviewView.gate_rejected_pipeline_routed_to_reject_target') }
      actionMessageTimers.push(setTimeout(() => { actionMessage.value[key] = null }, 5000))
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `${t('views.SettingsHitlReviewView.reject_failed')} ${formatApiError(e)}` }
  } finally {
    actioning.value[key] = null
    actionLoading.value[key] = false
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
  actionMessageTimers.forEach(timer => clearTimeout(timer))
  actionMessageTimers.length = 0
})
</script>
