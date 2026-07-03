<template>
  <FeatureGate feature-name="eval_system" required-tier="team" show-disabled>

    <PageTabs :tabs="[
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
      { label: 'Variants', to: '/variants/compare' },
      { label: 'AB Test', to: '/variants/ab-test' },
    ]" />

    <div class="mx-auto max-w-5xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Eval Proposals Queue</h1>
      <p class="mt-1 text-muted-foreground">Eval gaps detected by the feedback system — review and publish as eval definitions</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadProposals" />

    <template v-else>
      <div v-if="proposals.length === 0" class="rounded-lg border bg-card p-8 text-center text-sm text-muted-foreground">
        No eval gap proposals found. All checked gates have adequate eval coverage.
      </div>

      <div v-else class="space-y-4">
        <div
          v-for="p in proposals"
          :key="p.id"
          class="rounded-lg border bg-card p-5 shadow-sm"
          :data-testid="'proposal-card-' + p.id"
        >
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0 flex-1 space-y-3">
              <div class="flex flex-wrap items-center gap-2">
                <span class="inline-block rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                  {{ p.pipeline_name || 'Unnamed Pipeline' }}
                </span>
                <span
                  class="inline-block rounded px-2 py-0.5 text-xs font-medium"
                  :class="statusBadgeClass(p.feedback_status)"
                >
                  {{ p.feedback_status }}
                </span>
                <span v-if="p.run_id" class="text-xs text-muted-foreground">
                  Run: {{ p.run_id.slice(0, 8) }}
                </span>
              </div>

              <div>
                <p class="text-sm font-medium text-foreground">Gap Description</p>
                <p class="mt-0.5 text-sm text-muted-foreground">{{ p.rejection_reason }}</p>
              </div>

              <div class="grid grid-cols-2 gap-4 text-xs text-muted-foreground">
                <div>
                  <span class="font-medium text-foreground">Gate:</span> {{ p.gate_id }}
                </div>
                <div>
                  <span class="font-medium text-foreground">Node:</span> {{ p.producing_node_id }}
                </div>
                <div v-if="p.created_at">
                  <span class="font-medium text-foreground">Detected:</span> {{ formatDate(p.created_at) }}
                </div>
                <div v-if="p.needs_human_review">
                  <span class="font-medium text-amber-500">Needs human review</span>
                </div>
              </div>
            </div>

            <div v-if="isActionable(p.feedback_status)" class="flex shrink-0 items-center gap-2">
              <button
                :disabled="actioningId === p.id"
                data-testid="proposal-publish"
                class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                @click="publishProposal(p)"
              >
                {{ actioningId === p.id ? 'Publishing...' : 'Publish' }}
              </button>
              <button
                :disabled="actioningId === p.id"
                data-testid="proposal-dismiss"
                class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent disabled:opacity-50"
                @click="dismissProposal(p.id)"
              >
                Dismiss
              </button>
            </div>
          </div>

          <div v-if="actionMessages[p.id]" class="mt-3 text-sm" :class="actionMessages[p.id].type === 'error' ? 'text-destructive' : 'text-success'">
            {{ actionMessages[p.id].text }}
          </div>
        </div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../lib/api/client'
import { useApi } from '../composables/useApi'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import PageTabs from "../components/PageTabs.vue"

const planStore = usePlanStore()

interface EvalProposalItem {
  id: string
  run_id: string | null
  gate_id: string
  rejected_by: string | null
  rejection_reason: string
  rejected_output: Record<string, unknown>
  producing_node_id: string
  producing_agent_id: string | null
  feedback_status: string
  feedback_handler_type: string
  correction_run_id: string | null
  eval_gap: boolean | null
  needs_human_review: boolean
  pipeline_name: string | null
  created_at: string | null
}

interface ProposalsResponse {
  items: EvalProposalItem[]
  total: number
  page: number
  page_size: number
}

const { patch } = useApi()

const proposals = ref<EvalProposalItem[]>([])
const loading = ref(true)
const pageError = ref<string | null>(null)
const actioningId = ref<string | null>(null)
const actionMessages = ref<Record<string, { type: string; text: string }>>({})

function statusBadgeClass(status: string): string {
  const classMap: Record<string, string> = {
    pending: 'bg-pending/10 text-pending',
    routing: 'bg-warning/10 text-warning',
    correcting: 'bg-purple-100 text-purple-700',
    resolved: 'bg-success/10 text-success',
    escalated: 'bg-destructive/10 text-destructive',
    dismissed: 'bg-muted text-muted-foreground',
  }
  return classMap[status] ?? 'bg-muted text-muted-foreground'
}

function formatDate(dateStr: string): string {
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function isActionable(status: string): boolean {
  return status === 'pending' || status === 'routing'
}

async function loadProposals() {
  loading.value = true
  pageError.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/feedback/proposals', { params: {} as any })
    if (err) {
      pageError.value = `Failed to load proposals: ${err}`
    } else if (data) {
      proposals.value = (data as unknown as ProposalsResponse).items
    }
  } catch (e: unknown) {
    pageError.value = `Failed to load proposals: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function publishProposal(p: EvalProposalItem) {
  actioningId.value = p.id
  delete actionMessages.value[p.id]
  try {
    await patch(`/api/v1/feedback/${p.id}/status`, { status: 'resolved' })
    actionMessages.value[p.id] = { type: 'success', text: 'Proposal published. Eval definition created.' }
    p.feedback_status = 'resolved'
    setTimeout(() => { delete actionMessages.value[p.id] }, 3000)
  } catch (e: unknown) {
    actionMessages.value[p.id] = { type: 'error', text: `Publish failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    actioningId.value = null
  }
}

async function dismissProposal(id: string) {
  actioningId.value = id
  delete actionMessages.value[id]
  try {
    await patch(`/api/v1/feedback/${id}/status`, { status: 'dismissed' })
    actionMessages.value[id] = { type: 'success', text: 'Proposal dismissed.' }
    const prop = proposals.value.find(p => p.id === id)
    if (prop) prop.feedback_status = 'dismissed'
    setTimeout(() => { delete actionMessages.value[id] }, 3000)
  } catch (e: unknown) {
    actionMessages.value[id] = { type: 'error', text: `Dismiss failed: ${e instanceof Error ? e.message : String(e)}` }
  } finally {
    actioningId.value = null
  }
}

onMounted(() => { planStore.fetchPlan(); loadProposals() })
</script>
