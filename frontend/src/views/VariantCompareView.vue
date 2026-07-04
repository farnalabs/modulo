<template>
  <PageTabs :tabs="[
      { label: 'Evals', to: '/evals/editor' },
      { label: 'Proposals', to: '/evals/proposals' },
      { label: 'Variants', to: '/variants/compare' },
      { label: 'AB Test', to: '/variants/ab-test' },
    ]" />

    <div class="mx-auto max-w-6xl space-y-8 p-6">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <header>
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.variantCompare.title') }}</h1>
        <p class="mt-1 text-muted-foreground">
          {{ $t('views.variantCompare.subtitle') }}
        </p>
      </header>

      <div class="flex flex-wrap items-center gap-4">
        <select
          v-model="selectedGroupId"
          data-testid="variant-compare-group-select"
          class="min-w-[280px] rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="" disabled>{{ $t('views.variantCompare.selectGroup') }}</option>
          <option v-for="g in groups" :key="g.id" :value="g.id">
            {{ g.name }}
          </option>
        </select>

        <button
          :disabled="!selectedGroupId || runningVariants.size > 0"
          data-testid="variant-compare-run-variants"
          class="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          @click="runVariants"
        >
          <span v-if="runningVariants.size > 0" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
          {{ runningVariants.size > 0 ? $t('views.variantCompare.running') : $t('views.variantCompare.runVariants') }}
        </button>

        <span v-if="selectedGroup" class="text-xs text-muted-foreground">
          {{ $t('views.variantCompare.runs', { count: selectedGroup.run_count }) }} ·
          {{ selectedGroup.selection_strategy }}
        </span>
      </div>

      <template v-if="selectedGroup">
        <div class="overflow-x-auto rounded-lg border bg-card">
          <table class="w-full text-left text-sm">
            <thead>
              <tr class="border-b bg-muted/50">
                <th class="sticky left-0 z-10 bg-muted/50 px-4 py-3 font-semibold">{{ $t('views.variantCompare.node') }}</th>
                <th
                  v-for="v in variants"
                  :key="v.name"
                  class="min-w-[160px] px-4 py-3 font-semibold"
                >
                  <div class="flex flex-col gap-0.5">
                    <span>{{ v.name }}</span>
                    <span class="text-xs font-normal text-muted-foreground">{{ $t('views.variantCompare.weight') }}: {{ v.weight }}</span>
                  </div>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="node in nodeNames"
                :key="node"
                class="border-b last:border-b-0 hover:bg-muted/30"
              >
                <td class="sticky left-0 z-10 bg-card px-4 py-3 font-mono text-xs">
                  {{ node }}
                </td>
                <td
                  v-for="v in variants"
                  :key="`${node}-${v.name}`"
                  class="px-4 py-3 align-top"
                >
                  <div class="flex flex-col gap-1.5">
                    <div>
                  <span
                    v-if="getCellStatus(node, v.name) === 'pass'"
                    class="badge badge-status-success"
                  >
                    {{ $t('views.variantCompare.statusPass') }}
                  </span>
                  <span
                    v-else-if="getCellStatus(node, v.name) === 'fail'"
                    class="badge badge-status-destructive"
                  >
                    {{ $t('views.variantCompare.statusFail') }}
                  </span>
                  <span
                    v-else-if="getCellStatus(node, v.name) === 'partial'"
                    class="badge badge-status-warning"
                  >
                    {{ $t('views.variantCompare.statusPartial') }}
                  </span>
                      <span
                        v-else
                        class="text-xs text-muted-foreground"
                      >—</span>
                    </div>
                    <div v-if="getNodeEvalResults(node, v.name).length > 0" class="flex flex-wrap gap-1">
                      <span
                        v-for="er in getNodeEvalResults(node, v.name)"
                        :key="er.eval_id"
                        class="inline-flex items-center gap-0.5 rounded bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground"
                        :title="er.detail ?? undefined"
                      >
                        {{ er.score !== null ? er.score.toFixed(2) : '—' }}
                      </span>
                    </div>
                  </div>
                </td>
              </tr>
              <tr v-if="nodeNames.length === 0" class="border-b last:border-b-0">
                <td colspan="100" class="px-4 py-8 text-center text-sm text-muted-foreground">
                  <template v-if="runEntries.size === 0">
                    {{ $t('views.variantCompare.noRunData') }}
                  </template>
                  <template v-else>
                    {{ $t('views.variantCompare.waitingForRuns') }}
                  </template>
                </td>
              </tr>
            </tbody>
            <tfoot v-if="summaryByVariant.length > 0">
              <tr class="border-t bg-muted/30 font-medium">
                <td class="sticky left-0 z-10 bg-muted/30 px-4 py-3">{{ $t('views.variantCompare.summary') }}</td>
                <td
                  v-for="s in summaryByVariant"
                  :key="s.name"
                  class="px-4 py-3"
                >
                  <div class="flex flex-col gap-1 text-xs">
                    <div class="flex items-center gap-1.5">
                      <span
                        v-if="s.passRate !== null"
                        class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium"
                        :class="passRateClass(s.passRate)"
                      >
                        {{ s.passRate.toFixed(0) }}%
                      </span>
                      <span v-else class="text-muted-foreground">—</span>
                      <span class="text-muted-foreground">{{ $t('views.variantCompare.pass') }}</span>
                    </div>
                    <div v-if="s.totalCost !== null" class="text-muted-foreground">
                      {{ $t('views.variantCompare.cost', { cost: Number(s.totalCost).toFixed(6) }) }}
                    </div>
                    <div v-if="s.tokenTotal !== null" class="text-muted-foreground">
                      {{ $t('views.variantCompare.tokens', { count: s.tokenTotal.toLocaleString() }) }}
                    </div>
                    <div class="flex gap-2 text-muted-foreground">
                      <span v-if="s.approved > 0" class="text-success">{{ $t('views.variantCompare.approved', { count: s.approved }) }}</span>
                      <span v-if="s.rejected > 0" class="text-destructive">{{ $t('views.variantCompare.rejected', { count: s.rejected }) }}</span>
                      <span v-if="s.pending > 0" class="text-warning">{{ $t('views.variantCompare.pending', { count: s.pending }) }}</span>
                      <span v-if="s.approved === 0 && s.rejected === 0 && s.pending === 0" class="text-muted-foreground/60">{{ $t('views.variantCompare.noHitl') }}</span>
                    </div>
                  </div>
                </td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div v-if="nodeNames.length > 0 && diffVariantsAvailable.length >= 2" class="space-y-4">
          <h2 class="text-xl font-semibold tracking-tight">{{ $t('views.variantCompare.outputDiffViewer') }}</h2>
          <div class="flex flex-wrap gap-4">
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.node') }}:</span>
              <select
                v-model="diffNode"
                data-testid="variant-compare-diff-node"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option v-for="n in nodeNames" :key="n" :value="n">{{ n }}</option>
              </select>
            </label>
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.variantA') }}:</span>
              <select
                v-model="diffVarA"
                data-testid="variant-compare-diff-variant-a"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option v-for="v in diffVariantsAvailable" :key="v" :value="v">{{ v }}</option>
              </select>
            </label>
            <label class="flex items-center gap-2 text-sm">
              <span class="text-muted-foreground">{{ $t('views.variantCompare.variantB') }}:</span>
              <select
                v-model="diffVarB"
                data-testid="variant-compare-diff-variant-b"
                class="rounded-lg border border-input bg-background px-3 py-1.5 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option v-for="v in diffVariantsAvailable" :key="v" :value="v">{{ v }}</option>
              </select>
            </label>
          </div>
          <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div class="overflow-auto rounded-lg border bg-card">
              <div class="border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
                {{ diffVarA || '—' }}
              </div>
              <pre class="overflow-x-auto p-3 text-xs leading-relaxed"><code>{{ diffContentA }}</code></pre>
            </div>
            <div class="overflow-auto rounded-lg border bg-card">
              <div class="border-b bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
                {{ diffVarB || '—' }}
              </div>
              <pre class="overflow-x-auto p-3 text-xs leading-relaxed"><code>{{ diffContentB }}</code></pre>
            </div>
          </div>
        </div>
      </template>

      <div
        v-else-if="!loading && groups.length === 0"
        class="rounded-lg border bg-card p-8 text-center text-muted-foreground"
      >
        {{ $t('views.variantCompare.noGroups') }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"

const { t } = useI18n()

type VariantGroup = components['schemas']['VariantGroupResponse']
type RunResponse = components['schemas']['RunResponse']
type RunIOResponse = components['schemas']['RunIOResponse']
type RunEvalItem = components['schemas']['RunEvalItem']
type RunEvalListResponse = components['schemas']['RunEvalListResponse']

interface RunEntry {
  runId: string
  variantName: string
  runStatus: string
  totalCostUsd: number | null
  tokenConsumption: Record<string, unknown> | null
  nodeOutputs: Record<string, unknown> | null
  evalResults: RunEvalItem[]
}

const groups = ref<VariantGroup[]>([])
const selectedGroupId = ref<string | null>(null)
const selectedGroup = ref<VariantGroup | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const isUnmounted = ref(false)

const runEntries = ref<Map<string, RunEntry>>(new Map())
const runningVariants = ref<Set<string>>(new Set())

const diffNode = ref<string | null>(null)
const diffVarA = ref<string | null>(null)
const diffVarB = ref<string | null>(null)

const terminalStatuses = new Set(['complete', 'failed', 'cancelled', 'eval_failed'])

const variants = computed(() => selectedGroup.value?.variants ?? [])

const nodeNames = computed(() => {
  const names = new Set<string>()
  for (const entry of runEntries.value.values()) {
    if (entry.nodeOutputs) {
      Object.keys(entry.nodeOutputs).forEach(k => names.add(k))
    }
  }
  return Array.from(names).sort()
})

const variantRunData = computed(() => {
  const map = new Map<string, RunEntry>()
  for (const entry of runEntries.value.values()) {
    map.set(entry.variantName, entry)
  }
  return map
})

const diffVariantsAvailable = computed(() => {
  return Array.from(variantRunData.value.keys())
})

const summaryByVariant = computed(() => {
  const result: Array<{
    name: string
    passRate: number | null
    totalCost: number | null
    tokenTotal: number | null
    approved: number
    rejected: number
    pending: number
  }> = []
  const seen = new Set<string>()

  for (const entry of runEntries.value.values()) {
    const passCount = entry.evalResults.filter(r => r.passed).length
    const totalCount = entry.evalResults.length
    const tc = entry.tokenConsumption as { total_tokens?: number } | null
    result.push({
      name: entry.variantName,
      passRate: totalCount > 0 ? (passCount / totalCount) * 100 : null,
      totalCost: entry.totalCostUsd,
      tokenTotal: tc?.total_tokens ?? null,
      approved: 0,
      rejected: 0,
      pending: 0,
    })
    seen.add(entry.variantName)
  }

  for (const v of variants.value) {
    if (!seen.has(v.name)) {
      result.push({
        name: v.name,
        passRate: null,
        totalCost: null,
        tokenTotal: null,
        approved: 0,
        rejected: 0,
        pending: 0,
      })
    }
  }

  return result
})

function passRateClass(rate: number): string {
  if (rate >= 80) return 'badge badge-status-success'
  if (rate >= 40) return 'badge badge-status-warning'
  return 'badge badge-status-destructive'
}

function getNodeEvalResults(nodeName: string, variantName: string): RunEvalItem[] {
  const entry = variantRunData.value.get(variantName)
  if (!entry) return []
  return entry.evalResults.filter(r => r.node_id === nodeName)
}

function getCellStatus(nodeName: string, variantName: string): 'pass' | 'fail' | 'partial' | null {
  const evals = getNodeEvalResults(nodeName, variantName)
  if (evals.length === 0) return null
  const allPass = evals.every(r => r.passed)
  if (allPass) return 'pass'
  const allFail = evals.every(r => !r.passed)
  if (allFail) return 'fail'
  return 'partial'
}

function getNodeOutput(nodeName: string, variantName: string): unknown {
  const entry = variantRunData.value.get(variantName)
  if (!entry?.nodeOutputs) return null
  return (entry.nodeOutputs as Record<string, unknown>)[nodeName] ?? null
}

const diffContentA = computed(() => {
  if (!diffNode.value || !diffVarA.value) return ''
  const output = getNodeOutput(diffNode.value, diffVarA.value)
  return output ? JSON.stringify(output, null, 2) : ''
})

const diffContentB = computed(() => {
  if (!diffNode.value || !diffVarB.value) return ''
  const output = getNodeOutput(diffNode.value, diffVarB.value)
  return output ? JSON.stringify(output, null, 2) : ''
})

onMounted(() => {
  fetchGroups()
})

watch(selectedGroupId, async (id) => {
  if (id) {
    await fetchGroupDetail(id)
  }
})

watch(diffVariantsAvailable, (available) => {
  if (!diffVarA.value && available.length > 0) {
    diffVarA.value = available[0]
  }
  if (!diffVarB.value && available.length > 1) {
    diffVarB.value = available[1]
  }
  if (diffVarA.value && available.length > 0 && diffNode.value === null && nodeNames.value.length > 0) {
    diffNode.value = nodeNames.value[0]
  }
})

onBeforeUnmount(() => {
  isUnmounted.value = true
})

async function fetchGroups() {
  loading.value = true
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/variant-groups')
    if (err) {
      error.value = `${t('views.variantCompare.failedToLoadGroups')} ${JSON.stringify(err)}`
      return
    }
    const list = (data ?? []) as unknown as VariantGroup[]
    groups.value = list
    if (list.length > 0 && !selectedGroupId.value) {
      selectedGroupId.value = list[0].id
    }
  } catch (e: unknown) {
    error.value = `${t('views.variantCompare.failedToLoadGroups')} ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

async function fetchGroupDetail(id: string) {
  error.value = null
  try {
    const { data, error: err } = await api.GET('/api/v1/variant-groups/{group_id}', {
      params: { path: { group_id: id } },
    })
    if (err) {
      error.value = `${t('views.variantCompare.failedToLoadGroup')} ${JSON.stringify(err)}`
      return
    }
    selectedGroup.value = data as unknown as VariantGroup
  } catch (e: unknown) {
    error.value = `${t('views.variantCompare.failedToLoadGroup')} ${e instanceof Error ? e.message : String(e)}`
  }
}

async function runVariants() {
  if (!selectedGroupId.value) return
  const groupId = selectedGroupId.value
  error.value = null

  try {
    const { data, error: err } = await api.POST('/api/v1/variant-groups/{group_id}/run', {
      params: { path: { group_id: groupId } },
      body: {},
    })

    if (err) {
      error.value = `${t('views.variantCompare.runFailed')} ${JSON.stringify(err)}`
      return
    }

    if (!data) return

    const { run_id, variant_name } = data as unknown as {
      run_id: string
      variant_name: string
    }

    runningVariants.value.add(variant_name)

    const entry: RunEntry = {
      runId: run_id,
      variantName: variant_name,
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    }
    runEntries.value.set(variant_name, entry)

    await pollRunStatus(run_id, variant_name)
  } catch (e: unknown) {
    error.value = `${t('views.variantCompare.failedToRunVariants')} ${e instanceof Error ? e.message : String(e)}`
  }
}

async function pollRunStatus(runId: string, variantName: string) {
  let status = 'pending'

  while (!isUnmounted.value && !terminalStatuses.has(status)) {
    await delay(2000)

    try {
      const { data } = await api.GET('/api/v1/runs/{run_id}', {
        params: { path: { run_id: runId } },
      })

      if (data) {
        const runResp = data as unknown as RunResponse
        status = runResp.status

        const existing = runEntries.value.get(variantName)
        if (existing) {
          runEntries.value.set(variantName, {
            ...existing,
            runStatus: status,
            totalCostUsd: runResp.total_cost_usd,
            tokenConsumption: runResp.token_consumption,
          })
        }

        if (terminalStatuses.has(status)) {
          if (status === 'complete') {
            await Promise.all([
              fetchRunIO(runId, variantName),
              fetchRunEvals(runId, variantName),
            ])
          } else {
            error.value = `${t('views.variantCompare.runFailedStatus')} ${status}`
          }
          break
        }
      }
    } catch {
      // Retry on next poll interval
    }
  }

  runningVariants.value.delete(variantName)
}

async function fetchRunIO(runId: string, variantName: string) {
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/io', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      const ioResp = data as unknown as RunIOResponse
      const existing = runEntries.value.get(variantName)
      if (existing) {
        runEntries.value.set(variantName, {
          ...existing,
          nodeOutputs: ioResp.outputs_json,
        })
      }

      if (diffNode.value === null && ioResp.outputs_json) {
        const keys = Object.keys(ioResp.outputs_json)
        if (keys.length > 0) {
          diffNode.value = keys[0]
        }
      }
    }
  } catch {
    // Non-critical; diff viewer will be unavailable for this variant
  }
}

async function fetchRunEvals(runId: string, variantName: string) {
  try {
    const { data } = await api.GET('/api/v1/runs/{run_id}/evals', {
      params: { path: { run_id: runId } },
    })
    if (data) {
      const evalResp = data as unknown as RunEvalListResponse
      const existing = runEntries.value.get(variantName)
      if (existing) {
        runEntries.value.set(variantName, {
          ...existing,
          evalResults: evalResp.items ?? [],
        })
      }
    }
  } catch {
    // Non-critical; comparison cells will show no data
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}
</script>
