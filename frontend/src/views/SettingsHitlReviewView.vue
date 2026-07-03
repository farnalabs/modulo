<template>
  <FeatureGate feature-name="team_rbac" required-tier="team" show-disabled>

    <div class="mx-auto max-w-6xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">HITL Review</h1>
      <p class="mt-1 text-muted-foreground">Review and respond to pending human-in-the-loop gates</p>
    </header>

    <div class="flex flex-wrap items-center gap-4">
      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">Status</label>
        <select
          v-model="statusFilter"
          data-testid="hitl-review-status-select"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadGates"
        >
          <option value="">All</option>
          <option value="pending">Pending</option>
          <option value="claimed">Claimed</option>
          <option value="approved">Approved</option>
          <option value="rejected">Rejected</option>
        </select>
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">Pipeline</label>
        <select
          v-model="pipelineFilter"
          data-testid="hitl-review-pipeline-select"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadGates"
        >
          <option value="">All Pipelines</option>
          <option
            v-for="p in pipelines"
            :key="p.id"
            :value="p.id"
          >
            {{ p.name }}
          </option>
        </select>
      </div>

      <div class="flex-1 min-w-[200px]">
        <input
          v-model="searchQuery"
          type="text"
          placeholder="Search pipeline or node name..."
          data-testid="hitl-review-search"
          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @input="loadGates"
        />
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">From</label>
        <input
          v-model="dateFrom"
          type="date"
          data-testid="hitl-review-date-from"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadGates"
        />
      </div>

      <div class="flex items-center gap-2">
        <label class="text-sm font-medium text-muted-foreground">To</label>
        <input
          v-model="dateTo"
          type="date"
          data-testid="hitl-review-date-to"
          class="rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @change="loadGates"
        />
      </div>

      <div class="flex items-center gap-1 text-xs text-muted-foreground">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        Auto-refresh: {{ refreshCountdown }}s
      </div>
    </div>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="error" :message="error" />

    <template v-else>
      <div v-if="filteredGates.length === 0" class="rounded-lg border bg-card p-8 text-center">
        <svg
          class="mx-auto mb-3 h-12 w-12 text-muted-foreground"
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
        >
          <path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0 1 12 2.944a11.955 11.955 0 0 1-8.618 3.04A12.02 12.02 0 0 0 3 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
        </svg>
        <p class="text-lg font-medium">No pending HITL gates</p>
        <p class="mt-1 text-sm text-muted-foreground">All gates have been resolved or no pipelines have hit a human-in-the-loop gate yet.</p>
      </div>

      <div v-else class="space-y-2">
        <div
          v-for="gate in filteredGates"
          :key="gate.gate_id + gate.run_id"
          class="rounded-lg border bg-card shadow-sm"
        >
          <div
            data-testid="hitl-review-toggle-expand"
            class="flex cursor-pointer items-center gap-4 p-4"
            :class="{ 'border-b': expandedKey === expandKey(gate) }"
            role="button"
            tabindex="0"
            @click="toggleExpand(gate)"
            @keydown.enter="toggleExpand(gate)"
            @keydown.space.prevent="toggleExpand(gate)"
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
              <p class="truncate text-sm font-medium">{{ pipelineName(gate.pipeline_id) || shortId(gate.pipeline_id) }}</p>
            </div>

            <div class="min-w-0 flex-[2]">
              <p class="truncate text-sm text-muted-foreground">
                <span class="font-mono text-xs">{{ shortId(gate.gate_id) }}</span>
              </p>
            </div>

            <div class="min-w-0 flex-1">
              <p class="truncate text-xs text-muted-foreground">
                {{ gate.claimed_by ? `Assigned: ${gate.claimed_by}` : 'Unassigned' }}
              </p>
            </div>

            <span class="flex-shrink-0 text-xs text-muted-foreground">
              {{ formatDate(gate.claimed_at || gate.created_at || '') }}
            </span>
          </div>

          <div v-if="expandedKey === expandKey(gate)" class="border-t p-4">
            <div v-if="actionLoading[expandKey(gate)]" class="flex items-center justify-center py-8">
              <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            </div>

            <template v-else>
              <div class="grid grid-cols-2 gap-6">
                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Claim Metadata</h3>
                  <div class="space-y-1 text-sm">
                    <div class="flex justify-between">
                      <span class="text-muted-foreground">Run ID</span>
                      <span class="font-mono text-xs">{{ shortId(gate.run_id) }}</span>
                    </div>
                    <div class="flex justify-between">
                      <span class="text-muted-foreground">Node</span>
                      <span class="font-mono text-xs">{{ shortId(gate.gate_id) }}</span>
                    </div>
                    <div class="flex justify-between">
                      <span class="text-muted-foreground">Pipeline</span>
                      <span>{{ pipelineName(gate.pipeline_id) || shortId(gate.pipeline_id) }}</span>
                    </div>
                    <div class="flex justify-between">
                      <span class="text-muted-foreground">Created</span>
                      <span>{{ formatDate(gate.created_at || '') }}</span>
                    </div>
                    <div v-if="gate.claimed_at" class="flex justify-between">
                      <span class="text-muted-foreground">Claimed</span>
                      <span>{{ formatDate(gate.claimed_at) }}</span>
                    </div>
                    <div v-if="gate.expires_at" class="flex justify-between">
                      <span class="text-muted-foreground">Expires</span>
                      <span>{{ formatDate(gate.expires_at) }}</span>
                    </div>
                    <div v-if="gate.decision_at" class="flex justify-between">
                      <span class="text-muted-foreground">Decided</span>
                      <span>{{ formatDate(gate.decision_at) }}</span>
                    </div>
                    <div v-if="gate.decision" class="flex justify-between">
                      <span class="text-muted-foreground">Decision</span>
                      <span :class="gate.decision === 'approved' ? 'text-success' : 'text-destructive'">{{ gate.decision }}</span>
                    </div>
                    <div v-if="gate.claimed_by" class="flex justify-between">
                      <span class="text-muted-foreground">Assignees</span>
                      <span>{{ gate.claimed_by }}</span>
                    </div>
                    <div v-if="gate.team_scope" class="flex justify-between">
                      <span class="text-muted-foreground">Team</span>
                      <span>{{ gate.team_scope }}</span>
                    </div>
                  </div>
                </div>

                <div>
                  <h3 class="mb-2 text-sm font-semibold text-muted-foreground uppercase tracking-wider">Actions</h3>
                  <div class="space-y-3">
                    <div v-if="gateStatus(gate) === 'pending'">
                      <button
                        :disabled="claiming[expandKey(gate)]"
                        data-testid="hitl-review-claim"
                        class="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                        @click="claimGate(gate)"
                      >
                        {{ claiming[expandKey(gate)] ? 'Claiming...' : 'Claim Gate' }}
                      </button>
                    </div>

                    <div v-if="gateStatus(gate) === 'claimed'">
                      <div class="space-y-2">
                        <textarea
                          v-model="reviewNotes[expandKey(gate)]"
                          rows="2"
                          data-testid="hitl-review-notes"
                          class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          placeholder="Review notes..."
                        />
                        <div class="flex gap-2">
                          <button
                            :disabled="actioning[expandKey(gate)]"
                            data-testid="hitl-review-approve"
                            class="flex-1 rounded-lg bg-success px-4 py-2 text-sm font-medium text-white hover:bg-success/90 disabled:opacity-50"
                            @click="approveGate(gate)"
                          >
                            {{ actioning[expandKey(gate)] === 'approve' ? 'Approving...' : 'Approve' }}
                          </button>
                          <button
                            :disabled="actioning[expandKey(gate)]"
                            data-testid="hitl-review-reject"
                            class="flex-1 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                            @click="rejectGate(gate)"
                          >
                            {{ actioning[expandKey(gate)] === 'reject' ? 'Rejecting...' : 'Reject' }}
                          </button>
                        </div>
                      </div>
                    </div>

                    <div v-if="gateStatus(gate) === 'approved'" class="rounded-lg bg-success/10 p-3 text-sm text-success">
                      Gate was approved. The pipeline has resumed.
                    </div>

                    <div v-if="gateStatus(gate) === 'rejected'" class="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
                      Gate was rejected. The pipeline was routed to the reject target.
                    </div>

                    <div v-if="gateStatus(gate) === 'claimed' && currentClaimToken[expandKey(gate)]">
                      <div class="rounded-lg bg-muted p-3 text-xs">
                        <p class="font-medium text-muted-foreground mb-1">Claim Token</p>
                        <code class="break-all">{{ currentClaimToken[expandKey(gate)] }}</code>
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
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '../lib/api/client'
import { formatApiError, type ProblemDetail } from '../lib/api/formatError'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'
import { shortId } from '../utils/format'

const planStore = usePlanStore()

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

const gates = ref<GateItem[]>([])
const pipelines = ref<PipelineItem[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

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

const currentClaimToken = computed(() => claimTokens)

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
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function pipelineName(pipelineId: string): string {
  const p = pipelines.value.find(p => p.id === pipelineId)
  return p ? p.name : ''
}

const filteredGates = computed(() => {
  return gates.value.filter(gate => {
    if (statusFilter.value && gateStatus(gate) !== statusFilter.value) return false
    if (pipelineFilter.value && gate.pipeline_id !== pipelineFilter.value) return false
    if (searchQuery.value) {
      const q = searchQuery.value.toLowerCase()
      const pName = pipelineName(gate.pipeline_id).toLowerCase()
      if (!pName.includes(q) && !gate.gate_id.toLowerCase().includes(q)) return false
    }
    if (dateFrom.value) {
      const from = new Date(dateFrom.value)
      const created = new Date(gate.created_at || gate.claimed_at || '')
      if (created < from) return false
    }
    if (dateTo.value) {
      const to = new Date(dateTo.value)
      to.setHours(23, 59, 59, 999)
      const created = new Date(gate.created_at || gate.claimed_at || '')
      if (created > to) return false
    }
    return true
  })
})

async function loadPipelines() {
  try {
    const { data, error: err } = await api.GET('/api/v1/pipelines')
    if (!err && data) {
      pipelines.value = (data as any).items || []
    }
  } catch {
    // Non-critical
  }
}

async function loadGates() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/hitl/pending')
    if (err) {
      error.value = err && typeof err === 'object' && 'detail' in err
        ? `Failed to load gates: ${(err as ProblemDetail).detail}`
        : `Failed to load gates: ${formatApiError(err)}`
    } else if (data) {
      gates.value = ((data as any).gates || []).map((g: any) => ({
        ...g,
        run_id: String(g.run_id),
        pipeline_id: String(g.pipeline_id),
        claimed_by: g.claimed_by ? String(g.claimed_by) : null,
      }))
    }
  } catch (e: unknown) {
    error.value = `Failed to load gates: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

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
        text: err && typeof err === 'object' && 'detail' in err
          ? `Claim failed: ${(err as ProblemDetail).detail}`
          : `Claim failed: ${formatApiError(err)}`,
      }
    } else if (data) {
      const d = data as any
      claimTokens.value[key] = d.claim_token
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], claimed_by: d.claimed_by ? String(d.claimed_by) : null, claimed_at: d.expires_at, expires_at: d.expires_at }
      }
      actionMessage.value[key] = { type: 'success', text: 'Gate claimed. You can now approve or reject.' }
      setTimeout(() => { actionMessage.value[key] = null }, 5000)
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `Claim failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    claiming.value[key] = false
  }
}

async function approveGate(gate: GateItem) {
  const key = expandKey(gate)
  const token = claimTokens.value[key]
  if (!token) {
    actionMessage.value[key] = { type: 'error', text: 'No claim token. Claim the gate first.' }
    return
  }
  actioning.value[key] = 'approve'
  actionLoading.value[key] = true
  actionMessage.value[key] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/approve', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: token, notes: reviewNotes.value[key] || null },
    })
    if (err) {
      actionMessage.value[key] = {
        type: 'error',
        text: err && typeof err === 'object' && 'detail' in err
          ? `Approve failed: ${(err as ProblemDetail).detail}`
          : `Approve failed: ${formatApiError(err)}`,
      }
    } else {
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], decision: 'approved', decision_at: new Date().toISOString() }
      }
      actionMessage.value[key] = { type: 'success', text: 'Gate approved. Pipeline resuming.' }
      setTimeout(() => { actionMessage.value[key] = null }, 5000)
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `Approve failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    actioning.value[key] = null
    actionLoading.value[key] = false
  }
}

async function rejectGate(gate: GateItem) {
  const key = expandKey(gate)
  const token = claimTokens.value[key]
  if (!token) {
    actionMessage.value[key] = { type: 'error', text: 'No claim token. Claim the gate first.' }
    return
  }
  const reason = reviewNotes.value[key] || 'Rejected by reviewer'
  actioning.value[key] = 'reject'
  actionLoading.value[key] = true
  actionMessage.value[key] = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/reject', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: token, reason },
    })
    if (err) {
      actionMessage.value[key] = {
        type: 'error',
        text: err && typeof err === 'object' && 'detail' in err
          ? `Reject failed: ${(err as ProblemDetail).detail}`
          : `Reject failed: ${formatApiError(err)}`,
      }
    } else {
      const idx = gates.value.findIndex(g => expandKey(g) === key)
      if (idx !== -1) {
        gates.value[idx] = { ...gates.value[idx], decision: 'rejected', decision_at: new Date().toISOString() }
      }
      actionMessage.value[key] = { type: 'success', text: 'Gate rejected. Pipeline routed to reject target.' }
      setTimeout(() => { actionMessage.value[key] = null }, 5000)
    }
  } catch (e: unknown) {
    actionMessage.value[key] = { type: 'error', text: `Reject failed: ${e instanceof Error ? e.message : String(e)}` }
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
    loadGates()
    refreshCountdown.value = Math.floor(refreshInterval.value / 1000)
  }, refreshInterval.value)
  countdownTimer = setInterval(() => {
    if (refreshCountdown.value > 0) refreshCountdown.value--
  }, 1000)
}

function stopAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null }
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null }
}

onMounted(async () => {
  planStore.fetchPlan()
  await Promise.all([loadGates(), loadPipelines()])
  startAutoRefresh()
})

onUnmounted(() => {
  stopAutoRefresh()
})
</script>
