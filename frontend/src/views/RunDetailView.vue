<template>
    <div class="page-wide">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else-if="run">
      <nav aria-label="Breadcrumb" class="mb-4 flex items-center gap-1 text-sm text-muted-foreground">
        <router-link to="/runs" class="hover:text-foreground transition-colors">Runs</router-link>
        <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="h-3.5 w-3.5"><polyline points="9 18 15 12 9 6"/></svg>
        <span class="text-foreground font-medium">{{ run.pipeline_name || (run.run_number != null ? '#' + run.run_number : shortId(run.run_id)) }}</span>
      </nav>
      <!-- Run Header -->
      <header class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-3">
            <PageHeader :title="$t('views.RunDetailView.run_detail')" />
            <span :class="statusBadgeClass" class="capitalize">{{ run.status }}</span>
          </div>
          <p class="mt-1 text-sm text-muted-foreground">
            Pipeline: <span class="font-medium text-foreground">{{ formatRun(run) }}</span>
          </p>
          <p class="text-xs text-muted-foreground">
            Run ID: <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ shortId(run.run_id) }}</code>
            <button
              :aria-label="$t('views.RunDetailView.copy_run_id')"
              class="ml-1 inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium text-primary hover:bg-primary/10"
              @click="copyRunId"
            >
              {{ copied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
            </button>
          </p>
        </div>
        <div class="text-right text-xs text-muted-foreground">
          <div v-if="run.total_cost_usd != null" class="text-base font-semibold tabular-nums text-foreground">
            Total: ${{ formattedCost }}
          </div>
          <button
            data-testid="run-detail-share-summary"
            class="mt-2 inline-flex items-center gap-1 rounded-md px-3 py-1.5 text-sm font-medium text-primary hover:bg-primary/10"
            @click="copyShareSummary"
          >
            {{ shareCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.share_summary') }}
          </button>
        </div>
      </header>

      <!-- HITL Gate -->
      <section v-if="run.status === 'awaiting_human' && pendingGates.length > 0" class="rounded-lg border bg-card p-6 mb-6">
        <h2 class="text-base font-semibold tracking-tight mb-4">HITL Gate</h2>
        <div v-for="gate in pendingGates" :key="gate.gate_id" class="space-y-3">
          <div class="flex items-center gap-2 text-sm">
            <span class="font-medium">Gate:</span>
            <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ gate.gate_id }}</code>
          </div>
          <div v-if="gate.claimed_by && !claimToken" class="rounded-lg bg-muted/50 p-3 text-sm text-muted-foreground">
            Claimed by {{ gate.claimed_by }}
          </div>
          <div v-else-if="claimLoading" class="flex justify-center py-4">
            <div class="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
          <template v-else-if="claimToken">
            <div class="space-y-3">
              <textarea
                v-model="hitlNotes"
                rows="2"
                data-testid="run-detail-hitl-notes"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="Review notes (optional)"
              />
              <div class="flex gap-2">
                <button
                  :disabled="Boolean(actioning)"
                  data-testid="run-detail-approve"
                  class="flex-1 rounded-lg bg-success px-4 py-2 text-sm font-medium text-white hover:bg-success/90 disabled:opacity-50"
                  @click="approveGate"
                >
                  {{ actioning === 'approve' ? 'Approving...' : 'Approve' }}
                </button>
                <button
                  :disabled="Boolean(actioning)"
                  data-testid="run-detail-reject"
                  class="flex-1 rounded-lg bg-destructive px-4 py-2 text-sm font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                  @click="rejectGate"
                >
                  {{ actioning === 'reject' ? 'Rejecting...' : 'Reject' }}
                </button>
              </div>
            </div>
          </template>
          <button
            v-else
            :disabled="claimLoading"
            class="w-full rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            data-testid="run-detail-claim-gate"
            @click="claimGate(gate)"
          >
            {{ claimLoading ? 'Claiming...' : 'Claim Gate' }}
          </button>
          <div v-if="hitlMessage" class="text-sm" :class="hitlMessage.type === 'error' ? 'text-destructive' : 'text-success'">
            {{ hitlMessage.text }}
          </div>
        </div>
      </section>

      <!-- Timestamps -->
      <div v-if="runTimestamps" class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.created') }}</span> {{ runTimestamps.created }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.started') }}</span> {{ runTimestamps.started }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.completed') }}</span> {{ runTimestamps.completed }}</div>
      </div>

      <!-- Cancel button for running/pending runs -->
      <div v-if="run.status === 'running' || run.status === 'pending'" class="my-4">
        <button
          :disabled="cancelling"
          data-testid="run-detail-cancel"
          class="inline-flex items-center gap-2 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/20 disabled:opacity-50"
          @click="cancelRun"
        >
          <svg v-if="cancelling" class="h-4 w-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
          <svg v-else xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="8" y1="12" x2="16" y2="12"/></svg>
          {{ cancelling ? 'Stopping...' : 'Stop Run' }}
        </button>
        <span v-if="cancelError" class="ml-3 text-xs text-destructive">{{ cancelError }}</span>
      </div>

      <!-- Trace ID -->
      <div v-if="run.trace_id" class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">{{ $t('views.RunDetailView.otel_trace_id') }}</span>
        <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ run.trace_id }}</code>
        <button
          data-testid="run-detail-copy-trace-id"
          :aria-label="$t('views.RunDetailView.copy_trace_id')"
          class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
          @click="copyTraceId"
        >
          {{ copied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
        </button>
      </div>

      <div v-if="run?.status === 'complete' && lastNodeOutput" class="card p-5 mb-6">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-base font-semibold text-foreground">{{ $t('views.RunDetailView.final_output') }}</h2>
          <button
            class="px-3 py-1.5 text-xs font-medium rounded-lg border border-input bg-background hover:bg-accent transition-colors"
            @click="copyOutput"
            data-testid="run-detail-copy-output"
          >
            {{ outputCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy') }}
          </button>
        </div>
        <pre class="bg-muted/30 rounded-lg p-4 text-sm overflow-x-auto whitespace-pre-wrap">{{ formattedOutput }}</pre>
      </div>

      <!-- Per-Node Execution Trace -->
      <section class="space-y-4 rounded-lg border bg-card p-6">
        <h2 class="text-base font-semibold tracking-tight">{{ $t('views.RunDetailView.execution_trace') }}</h2>

        <div v-if="nodeEntries.length === 0" class="py-4 text-center text-sm text-muted-foreground">
          {{ $t('views.RunDetailView.no_node_data') }}
        </div>

        <table v-else class="w-full text-left text-sm">
          <thead>
            <tr class="border-b text-xs uppercase text-muted-foreground">
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.node') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.status') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.duration') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.input_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.output_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.cost') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.trace_id') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.io') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.logs') }}</th>
              <th class="pb-2 font-medium">{{ $t('views.RunDetailView.prompt') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="node in nodeEntries"
              :key="node.name"
              class="border-b last:border-b-0 hover:bg-muted/30"
            >
              <td class="py-3 pr-4 font-medium">{{ node.name }}</td>
              <td class="py-3 pr-4">
                <span :class="nodeStatusBadgeClass(node)">{{ node.status }}</span>
              </td>
              <td class="py-3 pr-4 tabular-nums text-muted-foreground">{{ node.duration }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.inputTokens ?? '—' }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.outputTokens ?? '—' }}</td>
              <td class="py-3 pr-4 tabular-nums">{{ node.cost != null ? '$' + node.cost.toFixed(6) : '—' }}</td>
              <td class="py-3 pr-4">
                <button
                  v-if="node.traceId"
                  data-testid="run-detail-node-trace-id"
                  :aria-label="$t('views.RunDetailView.copy_node_trace_id')"
                  class="cursor-pointer rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                  :title="node.traceId"
                  @click="copyText(node.traceId!)"
                  @keydown.enter="copyText(node.traceId!)"
                  @keydown.space.prevent="copyText(node.traceId!)"
                >{{ shortId(node.traceId) }}…</button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  v-if="node.io"
                  data-testid="run-detail-toggle-io"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="toggleNodeIO(node.name)"
                >
                  {{ expandedNodes.has(node.name) ? $t('views.RunDetailView.hide') : $t('views.RunDetailView.show') }}
                </button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  v-if="node.hasLogs"
                  data-testid="run-detail-toggle-logs"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="toggleNodeLogs(node.name)"
                >
                  {{ expandedLogs.has(node.name) ? $t('views.RunDetailView.hide') : $t('views.RunDetailView.view') }}
                </button>
                <span v-else class="text-muted-foreground">—</span>
              </td>
              <td class="py-3">
                <button
                  v-if="revealedPrompts[node.name]?.prompt"
                  data-testid="run-detail-show-prompt"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
                  @click="showPrompt(node.name)"
                >
                  {{ $t('views.RunDetailView.view') }}
                </button>
                <button
                  v-else-if="revealedPrompts[node.name] === undefined"
                  data-testid="run-detail-reveal-prompt"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground hover:text-primary"
                  @click="revealPrompt(node.name)"
                >
                  {{ $t('views.RunDetailView.prompt_hidden_click_to_reveal') }}
                </button>
                <span v-else class="text-xs text-muted-foreground">—</span>
              </td>
            </tr>

            <!-- Expandable IO rows -->
            <tr
              v-for="node in nodeEntries"
              :key="'io-' + node.name"
              v-show="expandedNodes.has(node.name)"
            >
              <td colspan="10" class="space-y-3 px-0 pb-4 pt-1">
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.input') }}</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed"><code>{{ node.io?.input ? formatJson(node.io.input) : '—' }}</code></pre>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.output') }}</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed"><code>{{ node.io?.output ? formatJson(node.io.output) : '—' }}</code></pre>
                </div>
              </td>
            </tr>

            <!-- Expandable Log rows -->
            <tr
              v-for="node in nodeEntries"
              :key="'log-' + node.name"
              v-show="expandedLogs.has(node.name)"
            >
              <td colspan="10" class="space-y-3 px-0 pb-4 pt-1">
                <div v-if="getNodeLog(node.name, 'agent_stdout')" class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">{{ $t('views.RunDetailView.agent_stdout') }}</h4>
                  <pre class="max-h-96 overflow-auto rounded bg-background p-3 text-xs leading-relaxed font-mono whitespace-pre-wrap"><code>{{ getNodeLog(node.name, 'agent_stdout') }}</code></pre>
                </div>
                <div v-if="getNodeLog(node.name, 'agent_stderr')" class="rounded-lg border bg-destructive/10 p-4">
                  <h4 class="mb-2 text-xs font-semibold text-destructive">{{ $t('views.RunDetailView.agent_stderr') }}</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed font-mono whitespace-pre-wrap"><code>{{ getNodeLog(node.name, 'agent_stderr') }}</code></pre>
                </div>
                <div v-if="!getNodeLog(node.name, 'agent_stdout') && !getNodeLog(node.name, 'agent_stderr')" class="text-center text-sm text-muted-foreground py-4">
                  {{ $t('views.RunDetailView.no_agent_logs') }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- Workspace Lease -->
      <section v-if="workspaceLease" class="rounded-lg border bg-card p-6">
        <h2 class="mb-3 text-base font-semibold tracking-tight">Workspace</h2>
        <div class="space-y-2 text-sm">
          <div class="flex items-center gap-2">
            <span class="font-medium">Status:</span>
            <span
              class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium"
              :class="workspaceStatusClass"
            >
              <span class="h-1.5 w-1.5 rounded-full" :class="workspaceDotClass" />
              {{ workspaceLease.status }}
            </span>
          </div>
          <div v-if="workspaceLease.sandbox_id" class="flex items-center gap-2">
            <span class="font-medium">Sandbox:</span>
            <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ workspaceLease.sandbox_id }}</code>
          </div>
          <div v-if="workspaceLease.duration_seconds != null">
            <span class="font-medium">Duration:</span>
            <span class="ml-1 tabular-nums">{{ formatDuration(workspaceLease.duration_seconds) }}</span>
          </div>
          <div v-if="workspaceLease.error_message" class="text-destructive">
            <span class="font-medium">Error:</span>
            <span class="ml-1">{{ workspaceLease.error_message }}</span>
          </div>
        </div>
      </section>

      <!-- Total Run Cost -->
      <section v-if="run.total_cost_usd != null" class="rounded-lg border bg-card p-6">
        <div class="flex items-center justify-between">
          <h2 class="text-base font-semibold tracking-tight">{{ $t('views.RunDetailView.total_run_cost') }}</h2>
          <span class="text-2xl font-semibold tabular-nums">${{ formattedCost }}</span>
        </div>
        <p v-if="totalTokens != null" class="mt-1 text-xs text-muted-foreground">
          {{ $t('views.RunDetailView.total_tokens', { count: totalTokens.toLocaleString() }) }}
        </p>
      </section>

      <!-- Prompt Reveal Dialog -->
      <Dialog v-if="selectedPrompt" :open="!!selectedPrompt" @update:open="closePromptDialog">
        <DialogContent class="max-w-3xl">
          <DialogHeader>
            <DialogTitle>
              Prompt — {{ selectedPrompt.nodeName }}
              <span v-if="selectedPrompt.tokenCount != null" class="ml-2 text-sm font-normal text-muted-foreground">
                ~{{ selectedPrompt.tokenCount.toLocaleString() }} tokens
              </span>
            </DialogTitle>
            <DialogDescription class="sr-only">
              {{ $t('views.RunDetailView.prompt_dialog_description') }}
            </DialogDescription>
          </DialogHeader>
          <div class="max-h-[60vh] overflow-auto rounded-lg border bg-muted p-4">
            <pre class="whitespace-pre-wrap text-xs leading-relaxed"><code>{{ selectedPrompt.prompt }}</code></pre>
          </div>
          <DialogFooter>
            <Button
              data-testid="run-detail-copy-prompt"
              @click="copyPromptText"
            >
              {{ promptCopied ? $t('views.RunDetailView.copied') : $t('views.RunDetailView.copy_prompt') }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import PageHeader from '../components/shared/PageHeader.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Dialog from '../components/ui/dialog/Dialog.vue'
import DialogContent from '../components/ui/dialog/DialogContent.vue'
import DialogHeader from '../components/ui/dialog/DialogHeader.vue'
import DialogTitle from '../components/ui/dialog/DialogTitle.vue'
import DialogDescription from '../components/ui/dialog/DialogDescription.vue'
import DialogFooter from '../components/ui/dialog/DialogFooter.vue'
import Button from '../components/ui/button/Button.vue'
import { formatApiError } from '../lib/api/formatError'
import { shortId, formatRun } from '../utils/format'

type RunResponse = components['schemas']['RunResponse'] & {
  created_at?: string | null
  started_at?: string | null
  completed_at?: string | null
}
type RunIOResponse = components['schemas']['RunIOResponse']

interface NodeTokenUsage {
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cost_usd?: number
}

interface NodeEntry {
  name: string
  status: string
  duration: string
  inputTokens: number | null
  outputTokens: number | null
  cost: number | null
  traceId: string | null
  io: { input: unknown; output: unknown } | null
  hasLogs: boolean
}

interface WorkspaceLeaseInfo {
  status: string
  sandbox_id?: string
  duration_seconds?: number
  error_message?: string
}

const route = useRoute()
const { t, locale } = useI18n()
const run = ref<RunResponse | null>(null)
const runIO = ref<RunIOResponse | null>(null)
const expandedNodes = ref(new Set<string>())
const expandedLogs = ref(new Set<string>())
const copied = ref(false)
const shareCopied = ref(false)
const promptCopied = ref(false)
const outputCopied = ref(false)
const pollInterval = ref<ReturnType<typeof setInterval> | null>(null)
const promptLoading = ref(new Set<string>())
const revealedPrompts = ref<Record<string, null | { prompt: string; messages: { role: string; content: string }[]; tokenCount: number; promptAlwaysVisible: boolean }>>({})
const selectedPrompt = ref<{ nodeName: string; prompt: string; tokenCount: number | null } | null>(null)
const workspaceLease = ref<WorkspaceLeaseInfo | null>(null)
const cancelling = ref(false)
const cancelError = ref<string | null>(null)
const pendingGates = ref<components['schemas']['GateResponse'][]>([])
const hitlLoading = ref(false)
const claimToken = ref<string | null>(null)
const claimLoading = ref(false)
const actioning = ref<string | null>(null)
const hitlNotes = ref('')
const hitlMessage = ref<{ type: string; text: string } | null>(null)

const shareSummary = computed(() => {
  const r = run.value
  if (!r) return ''
  const completed = nodeEntries.value.filter(n => n.status === 'complete').length
  const total = nodeEntries.value.length
  const tokens = totalTokens.value?.toLocaleString() ?? '—'
  const cost = r.total_cost_usd != null ? `$${Number(r.total_cost_usd).toFixed(6)}` : '—'
  return [
    `Run: ${r.run_number != null ? `#${r.run_number}` : shortId(r.run_id)}`,
    `Pipeline: ${r.pipeline_name || shortId(r.pipeline_id)}`,
    `Status: ${r.status}`,
    `Nodes: ${completed}/${total}`,
    `Tokens: ${tokens}`,
    `Cost: ${cost}`,
    `Duration: —`,
  ].join('\n')
})

async function copyShareSummary() {
  const text = shareSummary.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    shareCopied.value = true
    setTimeout(() => { shareCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy share summary', e)
  }
}

function toggleNodeIO(name: string) {
  const s = expandedNodes.value
  if (s.has(name)) s.delete(name)
  else s.add(name)
}

function toggleNodeLogs(name: string) {
  const s = expandedLogs.value
  if (s.has(name)) s.delete(name)
  else s.add(name)
}

async function copyTraceId() {
  if (!run.value?.trace_id) return
  await copyText(run.value.trace_id)
}

async function copyRunId() {
  if (!run.value?.run_id) return
  await copyText(run.value.run_id)
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy text', e)
  }
}

async function revealPrompt(nodeName: string) {
  if (promptLoading.value.has(nodeName)) return
  const runId = route.params.id as string
  if (!runId) return

  promptLoading.value = new Set([...promptLoading.value, nodeName])
  try {
    const { data, error: err } = await api.POST(
      '/api/v1/runs/{run_id}/nodes/{node_id}/prompt/reveal',
      {
        params: { path: { run_id: runId, node_id: nodeName } },
      },
    )
    if (err || !data) {
      if (typeof err === 'object' && err !== null && 'name' in err && (err as Record<string, unknown>).name === 'AbortError') throw err
      revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: null }
      const detail = (err as Record<string, unknown>)?.detail
      error.value = `${t('views.RunDetailView.prompt_reveal_error')} ${detail ? String(detail) : ''}`
      return
    }
    const d = data as components['schemas']['PromptRevealResponse']
    const revealed = {
      prompt: d.prompt,
      messages: d.messages.map(message => ({
        role: message.role ?? '',
        content: message.content ?? '',
      })),
      tokenCount: d.token_count,
      promptAlwaysVisible: d.prompt_always_visible,
    }
    revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: revealed }
    if (d.prompt_always_visible) {
      showPrompt(nodeName)
    }
  } finally {
    const s = new Set(promptLoading.value)
    s.delete(nodeName)
    promptLoading.value = s
  }
}

function showPrompt(nodeName: string) {
  const entry = revealedPrompts.value[nodeName]
  if (!entry) return
  selectedPrompt.value = {
    nodeName,
    prompt: entry.prompt,
    tokenCount: entry.tokenCount,
  }
}

function closePromptDialog() {
  selectedPrompt.value = null
}

async function copyPromptText() {
  if (!selectedPrompt.value?.prompt) return
  try {
    await navigator.clipboard.writeText(selectedPrompt.value.prompt)
    promptCopied.value = true
    setTimeout(() => { promptCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy prompt text', e)
  }
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function getNodeLog(nodeName: string, field: string): string | null {
  const outputs = runIO.value?.outputs_json as Record<string, unknown> | null ?? {}
  const nodeOutput = outputs[nodeName] as Record<string, unknown> | undefined
  if (!nodeOutput) return null
  const outputValue = nodeOutput.output as Record<string, unknown> | undefined
  if (!outputValue) return null
  const val = outputValue[field]
  return typeof val === 'string' && val.length > 0 ? val : null
}

const statusBadgeClass = computed(() => {
  const s = run.value?.status ?? ''
  const map: Record<string, string> = {
    running: 'badge badge-status-primary',
    complete: 'badge badge-status-success',
    failed: 'badge badge-status-destructive',
    cancelled: 'badge badge-status-warning',
    pending: 'badge badge-status-muted',
    awaiting_human: 'badge badge-status-pending',
  }
  return map[s] ?? 'badge badge-context-slate'
})

function nodeStatusBadgeClass(node: NodeEntry): string {
  const map: Record<string, string> = {
    running: 'badge badge-status-primary',
    complete: 'badge badge-status-success',
    failed: 'badge badge-status-destructive',
  }
  return map[node.status] ?? 'badge badge-context-slate'
}

const runTimestamps = computed(() => {
  const r = run.value
  if (!r) return null
  return {
    created: r.created_at ? formatTimestamp(r.created_at) : '—',
    started: r.started_at ? formatTimestamp(r.started_at) : '—',
    completed: r.completed_at ? formatTimestamp(r.completed_at) : '—',
  }
})

function formatTimestamp(dateStr: string): string {
  const d = new Date(dateStr)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleString(locale.value, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

const totalTokens = computed(() => {
  if (!run.value?.node_token_usage) return null
  const ntu = run.value.node_token_usage as Record<string, NodeTokenUsage>
  return Object.values(ntu).reduce((sum, n) => sum + (n.total_tokens ?? 0), 0)
})

const formattedCost = computed(() => {
  const c = run.value?.total_cost_usd
  if (c == null) return '0.00'
  return Number(c).toFixed(6)
})

const TERMINAL_STATUSES = ['complete', 'failed', 'cancelled']

const lastNodeOutput = computed(() => {
  const outputs = runIO.value?.outputs_json as Record<string, { input?: unknown; output?: unknown }> | null | undefined
  if (!outputs) return null
  const keys = Object.keys(outputs)
  if (keys.length === 0) return null
  for (let i = keys.length - 1; i >= 0; i--) {
    const entry = outputs[keys[i]]
    if (entry?.output != null) return entry.output
  }
  return null
})

const formattedOutput = computed(() => {
  const output = lastNodeOutput.value
  if (output == null) return ''
  if (typeof output === 'string') return output
  return JSON.stringify(output, null, 2)
})

const workspaceStatusClass = computed(() => {
  const s = workspaceLease.value?.status ?? ''
  const map: Record<string, string> = {
    running: 'bg-primary/10 text-primary',
    pending: 'bg-warning/10 text-warning',
    completed: 'bg-success/10 text-success',
    failed: 'bg-destructive/10 text-destructive',
    expired: 'bg-muted text-muted-foreground',
  }
  return map[s] ?? 'bg-muted text-muted-foreground'
})

const workspaceDotClass = computed(() => {
  const s = workspaceLease.value?.status ?? ''
  const map: Record<string, string> = {
    running: 'bg-primary',
    pending: 'bg-warning',
    completed: 'bg-success',
    failed: 'bg-destructive',
    expired: 'bg-muted-foreground',
  }
  return map[s] ?? 'bg-muted-foreground'
})

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

async function copyOutput() {
  const text = formattedOutput.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    outputCopied.value = true
    setTimeout(() => { outputCopied.value = false }, 2000)
  } catch (e) {
    console.warn('Failed to copy output', e)
  }
}

async function claimGate(gate: components['schemas']['GateResponse']) {
  claimLoading.value = true
  hitlMessage.value = null
  try {
    const { data, error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/claim', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { expiry_minutes: 15 },
    })
    if (err) {
      hitlMessage.value = { type: 'error', text: `Claim failed: ${formatApiError(err)}` }
    } else if (data) {
      const d = data as components['schemas']['ClaimResponse']
      claimToken.value = d.claim_token
      hitlMessage.value = { type: 'success', text: 'Gate claimed. You can now approve or reject.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Claim failed: ${formatApiError(e)}` }
  } finally {
    claimLoading.value = false
  }
}

async function cancelRun() {
  const runId = route.params.id as string
  if (!runId) return
  cancelling.value = true
  cancelError.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/cancel', {
      params: { path: { run_id: runId } },
    })
    if (err) {
      cancelError.value = `Failed to cancel: ${formatApiError(err)}`
    } else {
      if (run.value) run.value.status = 'cancelled'
      if (pollInterval.value) {
        clearInterval(pollInterval.value)
        pollInterval.value = null
      }
    }
  } catch (e: unknown) {
    cancelError.value = `Failed to cancel: ${formatApiError(e)}`
  } finally {
    cancelling.value = false
  }
}

async function approveGate() {
  if (!claimToken.value || pendingGates.value.length === 0) return
  const gate = pendingGates.value[0]
  actioning.value = 'approve'
  hitlMessage.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/approve', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: claimToken.value, notes: hitlNotes.value || null },
    })
    if (err) {
      hitlMessage.value = {
        type: 'error',
        text: `Approve failed: ${formatApiError(err)}`,
      }
    } else {
      pendingGates.value = []
      claimToken.value = null
      hitlNotes.value = ''
      if (run.value) run.value.status = 'running'
      hitlMessage.value = { type: 'success', text: 'Gate approved. Pipeline resuming.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Approve failed: ${formatApiError(e)}` }
  } finally {
    actioning.value = null
  }
}

async function rejectGate() {
  if (!claimToken.value || pendingGates.value.length === 0) return
  const gate = pendingGates.value[0]
  actioning.value = 'reject'
  hitlMessage.value = null
  try {
    const { error: err } = await api.POST('/api/v1/runs/{run_id}/hitl/{gate_id}/reject', {
      params: { path: { run_id: gate.run_id, gate_id: gate.gate_id } },
      body: { claim_token: claimToken.value, reason: hitlNotes.value || 'Rejected by reviewer' },
    })
    if (err) {
      hitlMessage.value = {
        type: 'error',
        text: `Reject failed: ${formatApiError(err)}`,
      }
    } else {
      pendingGates.value = []
      claimToken.value = null
      hitlNotes.value = ''
      if (run.value) run.value.status = 'running'
      hitlMessage.value = { type: 'success', text: 'Gate rejected. Pipeline routed to reject target.' }
      setTimeout(() => { hitlMessage.value = null }, 5000)
    }
  } catch (e: unknown) {
    hitlMessage.value = { type: 'error', text: `Reject failed: ${formatApiError(e)}` }
  } finally {
    actioning.value = null
  }
}

const nodeEntries = computed<NodeEntry[]>(() => {
  const r = run.value
  if (!r) return []

  const ntu = r.node_token_usage as Record<string, NodeTokenUsage> | null ?? {}
  const outputs = runIO.value?.outputs_json as Record<string, unknown> | null ?? {}

  const names = new Set([...Object.keys(ntu), ...Object.keys(outputs)])
  if (names.size === 0) return []

  return Array.from(names).map(name => {
    const usage = ntu[name] as NodeTokenUsage | undefined
    const nodeOutput = outputs[name] as Record<string, unknown> | undefined
    const outputValue = (nodeOutput?.output as Record<string, unknown>) ?? {}
    const hasLogs = !!(outputValue.agent_stdout || outputValue.agent_stderr)

    return {
      name,
      status: run.value?.status ?? 'unknown',
      duration: '—',
      inputTokens: usage?.input_tokens ?? null,
      outputTokens: usage?.output_tokens ?? null,
      cost: usage?.cost_usd ?? null,
      traceId: run.value?.trace_id ?? null,
      io: nodeOutput
        ? {
            input: nodeOutput.input ?? null,
            output: nodeOutput.output ?? null,
          }
        : null,
      hasLogs,
    }
  })
})

async function fetchHitlGates(runId: string) {
  if (hitlLoading.value) return
  hitlLoading.value = true
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/hitl/pending', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      pendingGates.value = ((data as any).gates || []) as components['schemas']['GateResponse'][]
    }
  } catch {
    // silently fail
  } finally {
    hitlLoading.value = false
  }
}

async function fetchRunData(runId: string) {
  try {
    const { data: runData } = await api.GET('/api/v1/runs/{run_id}', {
      params: { path: { run_id: runId } },
    })
    if (runData) {
      run.value = runData as unknown as RunResponse
      if (run.value.status === 'awaiting_human') {
        fetchHitlGates(runId)
      }
    }
    const { data: ioData } = await api.GET('/api/v1/runs/{run_id}/io', {
      params: { path: { run_id: runId } },
    })
    if (ioData) runIO.value = ioData as unknown as RunIOResponse
  } catch (e) {
    console.warn('Failed to fetch run data', e)
  }
}

function startPolling(runId: string) {
  pollInterval.value = setInterval(async () => {
    if (run.value && TERMINAL_STATUSES.includes(run.value.status)) {
      clearInterval(pollInterval.value!)
      pollInterval.value = null
      return
    }
    await fetchRunData(runId)
  }, 3000)
}

import { useDataFetch } from '../composables/useDataFetch'

interface RunFetchResult {
  run: RunResponse | null
  io: RunIOResponse | null
  workspace: WorkspaceLeaseInfo | null
}

const { loading, error } = useDataFetch<RunFetchResult>(
  async () => {
    const runId = route.params.id as string
    if (!runId) {
      return { data: { run: null, io: null, workspace: null }, error: { detail: t('views.RunDetailView.no_run_id_provided') } }
    }

    try {
      const [runResp, ioResp, wsResp] = await Promise.all([
        api.GET('/api/v1/runs/{run_id}', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
        api.GET('/api/v1/runs/{run_id}/io', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
        api.GET('/api/v1/runs/{run_id}/workspace-lease', { params: { path: { run_id: runId } } }).catch(() => ({ data: null })),
      ])
      const runData = runResp?.data
      const ioData = ioResp?.data
      const wsData = wsResp?.data

      if (runData) {
        run.value = runData as unknown as RunResponse
        if (run.value.status === 'awaiting_human') {
          fetchHitlGates(runId)
        }
      }
      if (ioData) runIO.value = ioData as unknown as RunIOResponse
      if (wsData) workspaceLease.value = wsData as unknown as WorkspaceLeaseInfo

      if (run.value?.status === 'complete' && nodeEntries.value.length > 0) {
        const last = nodeEntries.value[nodeEntries.value.length - 1]
        expandedNodes.value.add(last.name)
      }
      startPolling(runId)

      return { data: { run: run.value, io: runIO.value, workspace: workspaceLease.value }, error: undefined }
    } catch (e: unknown) {
      return { data: undefined, error: { detail: `${t('views.RunDetailView.failed_to_load_run')} ${formatApiError(e)}` } }
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pollInterval.value) {
    clearInterval(pollInterval.value)
  }
})
</script>
