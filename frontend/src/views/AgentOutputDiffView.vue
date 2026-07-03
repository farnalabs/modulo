<template>
  <div class="mx-auto max-w-6xl space-y-8 p-6">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <header>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.AgentOutputDiffView.agent_output_diff') }}</h1>
        <p class="mt-1 text-muted-foreground">
          Compare agent outputs across two pipeline runs
        </p>
      </header>

      <div class="flex flex-wrap items-end gap-4 rounded-lg border bg-card p-6">
        <div class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">Run A</span>
          <div class="flex gap-2">
            <select
              data-testid="diff-recent-runs-a"
              class="w-72 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              @change="onSelectRunA"
            >
              <option v-if="loadingRuns" value="" disabled>Loading...</option>
              <option v-else value="" disabled>Select recent run...</option>
              <option v-for="run in recentRuns" :key="run.id" :value="run.id">
                {{ run.pipeline_name }} — {{ run.status }} ({{ run.created_at }})
              </option>
            </select>
            <input
              v-model="runIdA"
              data-testid="diff-run-id-a"
              type="text"
                placeholder="Paste a run ID (or select from dropdown)"
              class="w-48 rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        </div>
        <label class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">{{ $t('views.AgentOutputDiffView.node_id') }}</span>
          <input
            v-model="nodeId"
            data-testid="diff-node-id"
            type="text"
            placeholder="node_name"
            class="w-48 rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <div class="flex flex-col gap-1.5">
          <span class="text-xs font-medium text-muted-foreground">Run B</span>
          <div class="flex gap-2">
            <select
              data-testid="diff-recent-runs-b"
              class="w-72 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              @change="onSelectRunB"
            >
              <option v-if="loadingRuns" value="" disabled>Loading...</option>
              <option v-else value="" disabled>Select recent run...</option>
              <option v-for="run in recentRuns" :key="run.id" :value="run.id">
                {{ run.pipeline_name }} — {{ run.status }} ({{ run.created_at }})
              </option>
            </select>
            <input
              v-model="runIdB"
              data-testid="diff-run-id-b"
              type="text"
              placeholder="Paste a run ID (or select from dropdown)"
              class="w-48 rounded-lg border border-input bg-background px-3 py-2 font-mono text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
        </div>
        <button
          :disabled="!canCompare"
          data-testid="diff-compare-btn"
          class="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          @click="compare"
        >
          Compare
        </button>
      </div>

      <div v-if="result" class="space-y-6">
        <div
          v-if="!result.has_diff"
          data-testid="diff-identical-banner"
          class="rounded-lg border border-green-500/30 bg-green-500/10 p-4 text-center text-sm font-medium text-green-600"
        >
          Outputs are identical
        </div>

        <div class="flex flex-wrap gap-4 text-sm">
          <span class="rounded bg-muted px-2.5 py-1 tabular-nums text-muted-foreground">
            <strong class="text-foreground">{{ totalLines }}</strong> lines total
          </span>
          <span class="rounded bg-green-500/10 px-2.5 py-1 tabular-nums text-green-600">
            +<strong>{{ addedCount }}</strong> added
          </span>
          <span class="rounded bg-red-500/10 px-2.5 py-1 tabular-nums text-red-600">
            -<strong>{{ removedCount }}</strong> removed
          </span>
          <span class="rounded bg-muted px-2.5 py-1 tabular-nums text-muted-foreground">
            <strong class="text-foreground">{{ unchangedCount }}</strong> unchanged
          </span>
        </div>

        <details class="rounded-lg border bg-card">
          <summary class="cursor-pointer px-4 py-3 text-sm font-medium text-muted-foreground hover:text-foreground">
            Raw outputs (collapsible)
          </summary>
          <div class="grid grid-cols-1 gap-4 border-t p-4 md:grid-cols-2">
            <div>
              <h4 class="mb-2 text-xs font-semibold text-muted-foreground">Run A: {{ runIdA }}</h4>
              <pre class="max-h-64 overflow-auto rounded bg-muted p-3 text-xs leading-relaxed"><code>{{ result.node_output_a ? JSON.stringify(result.node_output_a, null, 2) : '—' }}</code></pre>
            </div>
            <div>
              <h4 class="mb-2 text-xs font-semibold text-muted-foreground">Run B: {{ runIdB }}</h4>
              <pre class="max-h-64 overflow-auto rounded bg-muted p-3 text-xs leading-relaxed"><code>{{ result.node_output_b ? JSON.stringify(result.node_output_b, null, 2) : '—' }}</code></pre>
            </div>
          </div>
        </details>

        <div class="overflow-hidden rounded-lg border bg-card">
          <div class="border-b bg-muted/50 px-4 py-2 text-xs font-medium text-muted-foreground">
            Line-level diff <span class="ml-2 text-green-500">(+) added</span> <span class="ml-1 text-red-500">(-) removed</span>
          </div>
          <div class="overflow-x-auto">
            <table class="w-full text-left font-mono text-xs leading-relaxed">
              <tbody>
                <tr
                  v-for="(line, idx) in result.diff_lines"
                  :key="idx"
                  :class="diffRowClass(line)"
                >
                  <td class="w-12 select-none px-2 text-right text-muted-foreground/50">{{ line.line_a ?? '' }}</td>
                  <td class="w-px select-none px-1 text-muted-foreground/30">{{ diffMarker(line) }}</td>
                  <td class="w-12 select-none px-2 text-right text-muted-foreground/50">{{ line.line_b ?? '' }}</td>
                  <td class="whitespace-pre px-2 py-0.5" :class="diffContentClass(line)">{{ line.content }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'

type NodeOutputDiffResponse = components['schemas']['NodeOutputDiffResponse']
type NodeOutputDiffLine = components['schemas']['NodeOutputDiffLine']

const runIdA = ref('')
const nodeId = ref('')
const runIdB = ref('')
const loading = ref(false)
const error = ref<string | null>(null)
const result = ref<NodeOutputDiffResponse | null>(null)

interface RecentRun {
  id: string
  pipeline_name: string
  status: string
  created_at: string
}

const recentRuns = ref<RecentRun[]>([])
const loadingRuns = ref(false)

async function fetchRecentRuns() {
  loadingRuns.value = true
  try {
    const { data, error: err } = await api.GET('/api/v1/admin/dashboard/summary')
    if (!err && data) {
      recentRuns.value = (data as any).recent_runs ?? []
    }
  } catch {
    console.warn('Failed to fetch recent runs for diff selectors')
  } finally {
    loadingRuns.value = false
  }
}

onMounted(() => {
  fetchRecentRuns()
})

function onSelectRunA(event: Event) {
  const target = event.target as HTMLSelectElement
  if (target.value) {
    runIdA.value = target.value
  }
}

function onSelectRunB(event: Event) {
  const target = event.target as HTMLSelectElement
  if (target.value) {
    runIdB.value = target.value
  }
}

const canCompare = computed(() => {
  return runIdA.value.trim() && nodeId.value.trim() && runIdB.value.trim()
})

const totalLines = computed(() => result.value?.diff_lines.length ?? 0)

const addedCount = computed(() => {
  return result.value?.diff_lines.filter(l => l.type === 'added').length ?? 0
})

const removedCount = computed(() => {
  return result.value?.diff_lines.filter(l => l.type === 'removed').length ?? 0
})

const unchangedCount = computed(() => {
  return result.value?.diff_lines.filter(l => l.type === 'unchanged').length ?? 0
})

function diffRowClass(line: NodeOutputDiffLine): string {
  if (line.type === 'added') return 'bg-green-500/5'
  if (line.type === 'removed') return 'bg-red-500/5'
  return ''
}

function diffContentClass(line: NodeOutputDiffLine): string {
  if (line.type === 'added') return 'text-green-600'
  if (line.type === 'removed') return 'text-red-600'
  return 'text-foreground'
}

function diffMarker(line: NodeOutputDiffLine): string {
  if (line.type === 'added') return '+'
  if (line.type === 'removed') return '-'
  return ' '
}

async function compare() {
  if (!canCompare.value) return
  loading.value = true
  error.value = null
  result.value = null

  try {
    const { data, error: err } = await api.POST('/api/v1/runs/diff', {
      body: {
        run_id_a: runIdA.value.trim(),
        node_id_a: nodeId.value.trim(),
        run_id_b: runIdB.value.trim(),
        node_id_b: nodeId.value.trim(),
      },
    })
    if (err) {
      error.value = `Diff failed: ${JSON.stringify(err)}`
      return
    }
    result.value = data as unknown as NodeOutputDiffResponse
  } catch (e: unknown) {
    error.value = `Failed to compare outputs: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}
</script>
