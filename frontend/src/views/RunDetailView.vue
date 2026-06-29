<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else-if="run">
      <!-- Run Header -->
      <header class="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div class="flex items-center gap-3">
            <h1 class="text-3xl font-bold tracking-tight">Run Detail</h1>
            <span :class="statusBadgeClass">{{ run.status }}</span>
          </div>
          <p class="mt-1 text-sm text-muted-foreground">
            Pipeline: <span class="font-medium text-foreground">{{ run.pipeline_id }}</span>
          </p>
          <p class="text-xs text-muted-foreground">
            Run ID: <code class="rounded bg-muted px-1.5 py-0.5 font-mono">{{ run.run_id }}</code>
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
        <div><span class="font-medium text-foreground">Created:</span> {{ runTimestamps.created }}</div>
        <div><span class="font-medium text-foreground">Started:</span> {{ runTimestamps.started }}</div>
        <div><span class="font-medium text-foreground">Completed:</span> {{ runTimestamps.completed }}</div>
      </div>

      <!-- Trace ID -->
      <div v-if="run.trace_id" class="flex items-center gap-2">
        <span class="text-xs text-muted-foreground">OTel Trace ID:</span>
        <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{{ run.trace_id }}</code>
        <button
          data-testid="run-detail-copy-trace-id"
          aria-label="Copy trace ID"
          class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10"
          @click="copyTraceId"
        >
          {{ copied ? 'Copied!' : 'Copy' }}
        </button>
      </div>

      <!-- Per-Node Execution Trace -->
      <section class="space-y-4 rounded-lg border bg-card p-6">
        <h2 class="text-lg font-semibold tracking-tight">Execution Trace</h2>

        <div v-if="nodeEntries.length === 0" class="py-4 text-center text-sm text-muted-foreground">
          No node data available for this run.
        </div>

        <table v-else class="w-full text-left text-sm">
          <thead>
            <tr class="border-b text-xs uppercase text-muted-foreground">
              <th class="pb-2 pr-4 font-medium">Node</th>
              <th class="pb-2 pr-4 font-medium">Status</th>
              <th class="pb-2 pr-4 font-medium">Duration</th>
              <th class="pb-2 pr-4 font-medium">Input Tokens</th>
              <th class="pb-2 pr-4 font-medium">Output Tokens</th>
              <th class="pb-2 pr-4 font-medium">Cost</th>
              <th class="pb-2 pr-4 font-medium">Trace ID</th>
              <th class="pb-2 font-medium">IO</th>
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
                <code
                  v-if="node.traceId"
                  data-testid="run-detail-node-trace-id"
                  aria-label="Copy node trace ID"
                  class="cursor-pointer rounded bg-muted px-1.5 py-0.5 font-mono text-xs"
                  :title="node.traceId"
                  @click="copyText(node.traceId!)"
                >{{ node.traceId.slice(0, 8) }}…</code>
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
            </tr>

            <!-- Expandable IO rows -->
            <tr
              v-for="node in nodeEntries"
              :key="'io-' + node.name"
              v-show="expandedNodes.has(node.name)"
            >
              <td colspan="8" class="space-y-3 px-0 pb-4 pt-1">
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
          <h2 class="text-lg font-semibold tracking-tight">Total Run Cost</h2>
          <span class="text-2xl font-bold tabular-nums">${{ formattedCost }}</span>
        </div>
        <p v-if="totalTokens != null" class="mt-1 text-xs text-muted-foreground">
          {{ totalTokens.toLocaleString() }} total tokens
        </p>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

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

const shareSummary = computed(() => {
  const r = run.value
  if (!r) return ''
  const completed = nodeEntries.value.filter(n => n.status === 'complete').length
  const total = nodeEntries.value.length
  const tokens = totalTokens.value?.toLocaleString() ?? '—'
  const cost = r.total_cost_usd != null ? `$${Number(r.total_cost_usd).toFixed(6)}` : '—'
  return [
    `Run: ${r.run_id}`,
    `Pipeline: ${r.pipeline_id}`,
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

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
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

onMounted(async () => {
  const runId = route.params.id as string
  if (!runId) {
    error.value = 'No run ID provided'
    loading.value = false
    return
  }

  try {
    const { data: runData } = await api.GET('/api/v1/runs/{run_id}', {
      params: { path: { run_id: runId } },
    })
    if (!runData) {
      error.value = 'Run not found'
      return
    }
    run.value = runData as unknown as RunResponse

    const { data: ioData } = await api.GET('/api/v1/runs/{run_id}/io', {
      params: { path: { run_id: runId } },
    })
    if (ioData) {
      runIO.value = ioData as unknown as RunIOResponse
    }
  } catch (e: unknown) {
    error.value = `Failed to load run: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
})
</script>
