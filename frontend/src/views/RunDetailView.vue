<template>
  <BackLink to="/" label="Back to Dashboard" />
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else-if="run">
      <!-- Run Header -->
      <header class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-3">
            <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.RunDetailView.run_detail') }}</h1>
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
              {{ copied ? 'Copied!' : 'Copy' }}
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
            {{ shareCopied ? 'Copied!' : 'Share Summary' }}
          </button>
        </div>
      </header>

      <!-- Timestamps -->
      <div v-if="runTimestamps" class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.created') }}</span> {{ runTimestamps.created }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.started') }}</span> {{ runTimestamps.started }}</div>
        <div><span class="font-medium text-foreground">{{ $t('views.RunDetailView.completed') }}</span> {{ runTimestamps.completed }}</div>
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
          {{ copied ? 'Copied!' : 'Copy' }}
        </button>
      </div>

      <div v-if="run?.status === 'complete' && lastNodeOutput" class="card p-5 mb-6">
        <div class="flex items-center justify-between mb-3">
          <h2 class="text-lg font-semibold text-foreground">Final Output</h2>
          <button
            class="px-3 py-1.5 text-xs font-medium rounded-lg border border-input bg-background hover:bg-accent transition-colors"
            @click="copyOutput"
            data-testid="run-detail-copy-output"
          >
            {{ outputCopied ? 'Copied!' : 'Copy' }}
          </button>
        </div>
        <pre class="bg-muted/30 rounded-lg p-4 text-sm overflow-x-auto whitespace-pre-wrap">{{ formattedOutput }}</pre>
      </div>

      <!-- Per-Node Execution Trace -->
      <section class="space-y-4 rounded-lg border bg-card p-6">
        <h2 class="text-lg font-semibold tracking-tight">{{ $t('views.RunDetailView.execution_trace') }}</h2>

        <div v-if="nodeEntries.length === 0" class="py-4 text-center text-sm text-muted-foreground">
          No node data available for this run.
        </div>

        <table v-else class="w-full text-left text-sm">
          <thead>
            <tr class="border-b text-xs uppercase text-muted-foreground">
              <th class="pb-2 pr-4 font-medium">Node</th>
              <th class="pb-2 pr-4 font-medium">Status</th>
              <th class="pb-2 pr-4 font-medium">Duration</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.input_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.output_tokens') }}</th>
              <th class="pb-2 pr-4 font-medium">Cost</th>
              <th class="pb-2 pr-4 font-medium">{{ $t('views.RunDetailView.trace_id') }}</th>
              <th class="pb-2 pr-4 font-medium">IO</th>
              <th class="pb-2 font-medium">Prompt</th>
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
                  {{ expandedNodes.has(node.name) ? 'Hide' : 'Show' }}
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
                  View
                </button>
                <button
                  v-else-if="revealedPrompts[node.name] === undefined"
                  data-testid="run-detail-reveal-prompt"
                  class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-muted-foreground hover:text-primary"
                  @click="revealPrompt(node.name)"
                >
                  [Prompt hidden — click to reveal]
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
              <td colspan="9" class="space-y-3 px-0 pb-4 pt-1">
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">Input</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed"><code>{{ node.io?.input ? formatJson(node.io.input) : '—' }}</code></pre>
                </div>
                <div class="rounded-lg border bg-muted p-4">
                  <h4 class="mb-2 text-xs font-semibold text-muted-foreground">Output</h4>
                  <pre class="max-h-48 overflow-auto rounded bg-background p-3 text-xs leading-relaxed"><code>{{ node.io?.output ? formatJson(node.io.output) : '—' }}</code></pre>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- Total Run Cost -->
      <section v-if="run.total_cost_usd != null" class="rounded-lg border bg-card p-6">
        <div class="flex items-center justify-between">
          <h2 class="text-lg font-semibold tracking-tight">{{ $t('views.RunDetailView.total_run_cost') }}</h2>
          <span class="text-2xl font-bold tabular-nums">${{ formattedCost }}</span>
        </div>
        <p v-if="totalTokens != null" class="mt-1 text-xs text-muted-foreground">
          {{ totalTokens.toLocaleString() }} total tokens
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
              Rendered prompt sent to the LLM for this node.
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
              {{ promptCopied ? 'Copied!' : 'Copy Prompt' }}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import BackLink from '../components/BackLink.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import Dialog from '../components/ui/dialog/Dialog.vue'
import DialogContent from '../components/ui/dialog/DialogContent.vue'
import DialogHeader from '../components/ui/dialog/DialogHeader.vue'
import DialogTitle from '../components/ui/dialog/DialogTitle.vue'
import DialogDescription from '../components/ui/dialog/DialogDescription.vue'
import DialogFooter from '../components/ui/dialog/DialogFooter.vue'
import Button from '../components/ui/button/Button.vue'
import { shortId, formatRun } from '../utils/format'

type RunResponse = components['schemas']['RunResponse']
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
}

const route = useRoute()
const loading = ref(true)
const error = ref<string | null>(null)
const run = ref<RunResponse | null>(null)
const runIO = ref<RunIOResponse | null>(null)
const expandedNodes = ref(new Set<string>())
const copied = ref(false)
const shareCopied = ref(false)
const promptCopied = ref(false)
const outputCopied = ref(false)
const pollInterval = ref<ReturnType<typeof setInterval> | null>(null)
const promptLoading = ref(new Set<string>())
const revealedPrompts = ref<Record<string, null | { prompt: string; messages: { role: string; content: string }[]; tokenCount: number; promptAlwaysVisible: boolean }>>({})
const selectedPrompt = ref<{ nodeName: string; prompt: string; tokenCount: number | null } | null>(null)

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
  } catch {
    // clipboard not available
  }
}

function toggleNodeIO(name: string) {
  const s = expandedNodes.value
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
  } catch {
    // clipboard not available
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
      revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: null }
      return
    }
    const d = data as components['schemas']['PromptRevealResponse']
    const revealed = {
      prompt: d.prompt,
      messages: d.messages,
      tokenCount: d.token_count,
      promptAlwaysVisible: d.prompt_always_visible,
    }
    revealedPrompts.value = { ...revealedPrompts.value, [nodeName]: revealed }
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
  } catch {
    // clipboard not available
  }
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2)
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
    created: '—',
    started: '—',
    completed: '—',
  }
})

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

async function copyOutput() {
  const text = formattedOutput.value
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    outputCopied.value = true
    setTimeout(() => { outputCopied.value = false }, 2000)
  } catch {
    // clipboard not available
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
    }
  })
})

async function fetchRunData(runId: string) {
  const { data: runData } = await api.GET('/api/v1/runs/{run_id}', {
    params: { path: { run_id: runId } },
  })
  if (runData) run.value = runData as unknown as RunResponse
  const { data: ioData } = await api.GET('/api/v1/runs/{run_id}/io', {
    params: { path: { run_id: runId } },
  })
  if (ioData) runIO.value = ioData as unknown as RunIOResponse
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

onMounted(async () => {
  const runId = route.params.id as string
  if (!runId) {
    error.value = 'No run ID provided'
    loading.value = false
    return
  }

  try {
    await fetchRunData(runId)
    if (run.value?.status === 'complete' && nodeEntries.value.length > 0) {
      const last = nodeEntries.value[nodeEntries.value.length - 1]
      expandedNodes.value.add(last.name)
    }
    startPolling(runId)
  } catch (e: unknown) {
    error.value = `Failed to load run: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  if (pollInterval.value) {
    clearInterval(pollInterval.value)
  }
})
</script>
