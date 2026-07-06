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
        <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.ABTestModelsView.ab_test_models') }}</h1>
        <p class="mt-1 text-muted-foreground">
          Compare model backends side by side with weighted A/B testing — eval scores, costs, and token usage
        </p>
      </header>

      <div class="flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-sm">
          <span class="text-muted-foreground">{{ $t('views.ABTestModelsView.pipeline') }}</span>
          <select
            v-model="selectedPipelineId"
            data-testid="ab-test-models-pipeline-select"
            class="min-w-[280px] rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="" disabled>{{ $t('views.ABTestModelsView.select_a_pipeline') }}</option>
            <option v-for="p in pipelines" :key="p.id" :value="p.id">
              {{ p.name }}
            </option>
          </select>
        </label>

        <label class="flex items-center gap-2 text-sm">
          <span class="text-muted-foreground">{{ $t('views.ABTestModelsView.existing_group') }}</span>
          <select
            v-model="selectedGroupId"
            data-testid="ab-test-models-group-select"
            class="min-w-[200px] rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <option value="">{{ $t('views.ABTestModelsView.new_group') }}</option>
            <option v-for="g in filteredGroups" :key="g.id" :value="g.id">
              {{ g.name }}
            </option>
          </select>
        </label>
      </div>

      <template v-if="selectedPipelineId">
        <section class="space-y-4 rounded-lg border bg-card p-6">
          <h2 class="text-lg font-semibold tracking-tight">
            {{ $t(selectedGroupId ? 'views.ABTestModelsView.edit_variant_group' : 'views.ABTestModelsView.new_variant_group') }}
          </h2>
          <div class="grid gap-4 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-sm font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.group_name') }}</label>
              <input
                v-model="groupName"
                data-testid="ab-test-models-group-name"
                type="text"
                :placeholder="$t('views.ABTestModelsView.eg_claude_vs_gpt4o')"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.description') }}</label>
              <input
                v-model="groupDescription"
                data-testid="ab-test-models-group-description"
                type="text"
                :placeholder="$t('views.ABTestModelsView.compare_accuracy_and_cost_across_model_providers')"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>

          <div v-if="!selectedGroupId && availableSnapshotId" class="text-xs text-muted-foreground">
            {{ $t('views.ABTestModelsView.using_snapshot') }} <code class="rounded bg-muted px-1.5 py-0.5 font-mono">{{ shortId(availableSnapshotId) }}…</code>
            <span v-if="availableSnapshotTag" class="ml-1">({{ availableSnapshotTag }})</span>
          </div>

          <div class="space-y-3">
            <div class="flex items-center justify-between">
              <h3 class="text-sm font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.variants_title') }}</h3>
              <button
                :disabled="modelBackends.length === 0"
                data-testid="ab-test-models-add-variant"
                class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                @click="addVariant"
              >
                + Add Variant
              </button>
            </div>

            <p v-if="variants.length === 0" class="py-4 text-center text-sm text-muted-foreground">
              Add at least two variants to run an A/B test.
            </p>

            <div
              v-for="(v, i) in variants"
              :key="v.id"
              class="rounded-lg border bg-muted p-4"
            >
              <div class="mb-3 flex items-center justify-between">
                <span class="text-xs font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.variant_prefix') }} {{ i + 1 }}</span>
                <button
                  :data-testid="`ab-test-models-remove-variant-${i}`"
                  class="text-xs text-destructive hover:underline"
                  @click="removeVariant(i)"
                >
                  {{ $t('views.ABTestModelsView.remove') }}
                </button>
              </div>
              <div class="grid gap-3 sm:grid-cols-3">
                <div>
                  <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.name_label') }}</label>
                  <input
                    v-model="v.name"
                    :data-testid="`ab-test-models-variant-name-${i}`"
                    type="text"
                    :placeholder="$t('views.ABTestModelsView.variant_a')"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
                </div>
                <div>
                  <label class="mb-1 block text-xs font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.model_backend') }}</label>
                  <select
                    v-model="v.modelBackendId"
                    :data-testid="`ab-test-models-model-backend-${i}`"
                    class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <option value="" disabled>{{ $t('views.ABTestModelsView.select_model') }}</option>
                    <option
                      v-for="mb in modelBackends"
                      :key="mb.id"
                      :value="mb.id"
                    >
                      {{ mb.display_name }} ({{ mb.provider }})
                    </option>
                  </select>
                </div>
                <div>
                  <label class="mb-1 block text-xs font-medium text-muted-foreground">
                    Weight: <span class="font-mono tabular-nums">{{ v.weight }}%</span>
                  </label>
                  <input
                    v-model.number="v.weight"
                    :data-testid="`ab-test-models-weight-${i}`"
                    type="range"
                    min="0"
                    max="100"
                    step="1"
                    class="w-full accent-primary"
                  />
                </div>
              </div>
            </div>
          </div>

          <div class="flex flex-wrap gap-3 pt-2">
            <button
              :disabled="!canRun"
              data-testid="ab-test-models-run-ab-test"
              class="inline-flex items-center gap-2 rounded-lg bg-primary px-5 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="saveAndRun"
            >
              <span v-if="running" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              {{ running ? $t('views.ABTestModelsView.running') : $t('views.ABTestModelsView.run_ab_test') }}
            </button>
            <button
              data-testid="ab-test-models-save-group"
              class="inline-flex items-center gap-2 rounded-lg border border-input bg-background px-5 py-2 text-sm font-medium hover:bg-muted/50"
              @click="saveGroup"
            >
              {{ $t('views.ABTestModelsView.save_group') }}
            </button>
          </div>
        </section>

        <section v-if="runEntries.size > 0" class="space-y-4">
          <h2 class="text-xl font-semibold tracking-tight">{{ $t('views.ABTestModelsView.results_title') }}</h2>

          <div class="overflow-x-auto rounded-lg border bg-card">
            <table class="w-full text-left text-sm">
              <thead>
                <tr class="border-b bg-muted/50">
                  <th class="px-4 py-3 font-semibold text-muted-foreground">{{ $t('views.ABTestModelsView.metric') }}</th>
                  <th
                    v-for="s in summaryByVariant"
                    :key="s.name"
                    class="min-w-[180px] px-4 py-3 font-semibold"
                  >
                    <div class="flex flex-col gap-0.5">
                      <span>{{ s.name }}</span>
                      <span v-if="s.modelBackendName" class="text-xs font-normal text-muted-foreground">
                        {{ s.modelBackendName }}
                      </span>
                    </div>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr class="border-b hover:bg-muted/30">
                  <td class="px-4 py-3 font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.eval_pass_rate') }}</td>
                  <td
                    v-for="s in summaryByVariant"
                    :key="`pass-${s.name}`"
                    class="px-4 py-3"
                  >
                    <span
                      v-if="s.passRate !== null"
                      class="inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium"
                      :class="passRateClass(s.passRate)"
                    >
                      <span
                        class="h-1.5 w-1.5 rounded-full"
                        :class="s.passRate >= 80 ? 'bg-success' : s.passRate >= 40 ? 'bg-warning' : 'bg-destructive'"
                      />
                      {{ s.passRate.toFixed(0) }}%
                      <span class="font-normal opacity-70">({{ s.passedCount }}/{{ s.totalEvals }})</span>
                    </span>
                    <span v-else class="text-xs text-muted-foreground">—</span>
                  </td>
                </tr>
                <tr class="border-b hover:bg-muted/30">
                  <td class="px-4 py-3 font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.cost') }}</td>
                  <td
                    v-for="s in summaryByVariant"
                    :key="`cost-${s.name}`"
                    class="px-4 py-3 font-mono tabular-nums text-xs"
                  >
                    <span v-if="s.totalCost !== null">${{ Number(s.totalCost).toFixed(6) }}</span>
                    <span v-else class="text-muted-foreground">—</span>
                  </td>
                </tr>
                <tr class="hover:bg-muted/30">
                  <td class="px-4 py-3 font-medium text-muted-foreground">{{ $t('views.ABTestModelsView.tokens') }}</td>
                  <td
                    v-for="s in summaryByVariant"
                    :key="`tokens-${s.name}`"
                    class="px-4 py-3 font-mono tabular-nums text-xs"
                  >
                    <span v-if="s.tokenTotal !== null">{{ s.tokenTotal.toLocaleString() }}</span>
                    <span v-else class="text-muted-foreground">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div
            v-if="hasTerminalRuns"
            class="flex flex-wrap gap-3"
          >
            <button
              v-for="s in summaryByVariant"
              :key="`promote-${s.name}`"
              :data-testid="`ab-test-models-promote-${s.name}`"
              :disabled="promotingName === s.name"
              class="inline-flex items-center gap-2 rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-muted/50 disabled:opacity-50"
              @click="promoteWinner(s.name)"
            >
              <span v-if="promotingName === s.name" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              {{ $t('views.ABTestModelsView.promote_as_default', { name: s.name }) }}
            </button>
          </div>
        </section>

        <div
          v-else-if="!selectedPipelineId"
          class="rounded-lg border bg-card p-8 text-center text-muted-foreground"
        >
          {{ $t('views.ABTestModelsView.select_pipeline_hint') }}
        </div>
      </template>

      <div
        v-else-if="!loading && pipelines.length === 0"
        class="rounded-lg border bg-card p-8 text-center text-muted-foreground"
      >
        {{ $t('views.ABTestModelsView.no_pipelines_found') }}
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from "../components/PageTabs.vue"
import { shortId } from '../utils/format'

type PipelineItem = components['schemas']['PipelineItem']
type VariantGroup = components['schemas']['VariantGroupResponse']
type ModelBackend = components['schemas']['ModelBackendResponse']
type RunResponse = components['schemas']['RunResponse']
type RunIOResponse = components['schemas']['RunIOResponse']
type RunEvalItem = components['schemas']['RunEvalItem']
type RunEvalListResponse = components['schemas']['RunEvalListResponse']

interface VariantForm {
  id: string
  name: string
  modelBackendId: string | null
  weight: number
}

interface RunEntry {
  runId: string
  variantName: string
  modelBackendName: string
  runStatus: string
  totalCostUsd: number | null
  tokenConsumption: Record<string, unknown> | null
  nodeOutputs: Record<string, unknown> | null
  evalResults: RunEvalItem[]
}

const pipelines = ref<PipelineItem[]>([])
const variantGroups = ref<VariantGroup[]>([])
const modelBackends = ref<ModelBackend[]>([])
const selectedPipelineId = ref<string>('')
const selectedGroupId = ref<string>('')
const groupName = ref('')
const groupDescription = ref('')
const variants = ref<VariantForm[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const isUnmounted = ref(false)
const running = ref(false)
const savedGroupId = ref<string | null>(null)
const promotingName = ref<string | null>(null)

const runEntries = ref<Map<string, RunEntry>>(new Map())
const terminalStatuses = new Set(['complete', 'failed', 'cancelled', 'eval_failed'])

const filteredGroups = computed(() =>
  variantGroups.value.filter(g => g.pipeline_id === selectedPipelineId.value)
)

const availableSnapshotId = computed(() => {
  return snapshotId.value || undefined
})

const availableSnapshotTag = computed(() => {
  return null
})

const snapshotId = ref<string | null>(null)

const modelBackendMap = computed(() => {
  const map = new Map<string, ModelBackend>()
  for (const mb of modelBackends.value) {
    map.set(mb.id, mb)
  }
  return map
})

const canRun = computed(() => {
  if (!selectedPipelineId.value || !groupName.value.trim()) return false
  if (variants.value.length < 2) return false
  return variants.value.every(v => v.name.trim() && v.modelBackendId)
})

const hasTerminalRuns = computed(() => {
  for (const entry of runEntries.value.values()) {
    if (terminalStatuses.has(entry.runStatus)) return true
  }
  return false
})

const summaryByVariant = computed(() => {
  const result: Array<{
    name: string
    modelBackendName: string
    passRate: number | null
    passedCount: number
    totalEvals: number
    totalCost: number | null
    tokenTotal: number | null
  }> = []

  for (const entry of runEntries.value.values()) {
    const passCount = entry.evalResults.filter(r => r.passed).length
    const totalCount = entry.evalResults.length
    const tc = entry.tokenConsumption as { total_tokens?: number } | null
    result.push({
      name: entry.variantName,
      modelBackendName: entry.modelBackendName,
      passRate: totalCount > 0 ? (passCount / totalCount) * 100 : null,
      passedCount: passCount,
      totalEvals: totalCount,
      totalCost: entry.totalCostUsd,
      tokenTotal: tc?.total_tokens ?? null,
    })
  }

  return result
})

function passRateClass(rate: number): string {
  if (rate >= 80) return 'badge badge-status-success'
  if (rate >= 40) return 'badge badge-status-warning'
  return 'badge badge-status-destructive'
}

function addVariant() {
  const usedIds = new Set(modelBackends.value.filter(mb =>
    variants.value.some(v => v.modelBackendId === mb.id)
  ).map(mb => mb.id))

  const available = modelBackends.value.find(mb => !usedIds.has(mb.id))
  variants.value.push({
    id: crypto.randomUUID(),
    name: `Variant ${variants.value.length + 1}`,
    modelBackendId: available?.id ?? null,
    weight: Math.round(100 / (variants.value.length + 1)),
  })
  normalizeWeights()
}

function removeVariant(index: number) {
  variants.value.splice(index, 1)
  normalizeWeights()
}

function normalizeWeights() {
  if (variants.value.length === 0) return
  const total = variants.value.reduce((sum, v) => sum + v.weight, 0)
  if (total === 0) {
    const equal = Math.floor(100 / variants.value.length)
    variants.value.forEach((v, i) => {
      v.weight = i < variants.value.length - 1 ? equal : 100 - equal * (variants.value.length - 1)
    })
    return
  }
  const remaining = 100
  let allocated = 0
  variants.value.forEach((v, i) => {
    if (i === variants.value.length - 1) {
      v.weight = remaining - allocated
    } else {
      v.weight = Math.round((v.weight / total) * remaining)
      allocated += v.weight
    }
  })
}

async function saveGroup() {
  if (!selectedPipelineId.value || !groupName.value.trim()) return
  error.value = null

  const snapshot = snapshotId.value || ''
  const variantDefs = variants.value.map(v => ({
    snapshot_id: snapshot,
    name: v.name.trim(),
    weight: v.weight / 100,
    run_context_overrides: {
      model_backend_id: v.modelBackendId,
    },
    eval_definition_ids: [],
  }))

  try {
    if (selectedGroupId.value) {
      const { data, error: err } = await api.PUT('/api/v1/variant-groups/{group_id}', {
        params: { path: { group_id: selectedGroupId.value } },
        body: {
          pipeline_id: selectedPipelineId.value,
          name: groupName.value.trim(),
          description: groupDescription.value || null,
          variants: variantDefs as unknown as components['schemas']['VariantDef'][],
        },
      })
      if (err) {
        error.value = `Failed to update group: ${JSON.stringify(err)}`
        return
      }
      if (data) {
        savedGroupId.value = (data as unknown as VariantGroup).id
      }
    } else {
      const { data, error: err } = await api.POST('/api/v1/variant-groups', {
        body: {
          pipeline_id: selectedPipelineId.value,
          name: groupName.value.trim(),
          description: groupDescription.value || null,
          variants: variantDefs as unknown as components['schemas']['VariantDef'][],
        },
      })
      if (err) {
        error.value = `Failed to create group: ${JSON.stringify(err)}`
        return
      }
      if (data) {
        const group = data as unknown as VariantGroup
        savedGroupId.value = group.id
        selectedGroupId.value = group.id
        await fetchVariantGroups()
      }
    }
  } catch (e: unknown) {
    error.value = `Failed to save group: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function saveAndRun() {
  await saveGroup()
  if (!savedGroupId.value) return
  await runTest(savedGroupId.value)
}

async function runTest(groupId: string) {
  running.value = true
  error.value = null
  runEntries.value.clear()

  try {
    const { data, error: err } = await api.POST('/api/v1/variant-groups/{group_id}/run', {
      params: { path: { group_id: groupId } },
      body: {},
    })

    if (err) {
      error.value = `Run failed: ${JSON.stringify(err)}`
      return
    }

    if (!data) return

    const { run_id, variant_name } = data as unknown as {
      run_id: string
      variant_name: string
    }

    const mbId = variants.value.find(v => v.name === variant_name)?.modelBackendId
    const mb = mbId ? modelBackendMap.value.get(mbId) : undefined

    const entry: RunEntry = {
      runId: run_id,
      variantName: variant_name,
      modelBackendName: mb?.display_name || variant_name,
      runStatus: 'pending',
      totalCostUsd: null,
      tokenConsumption: null,
      nodeOutputs: null,
      evalResults: [],
    }
    runEntries.value.set(variant_name, entry)

    await pollRunStatus(run_id, variant_name)
  } catch (e: unknown) {
    error.value = `Failed to run A/B test: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    running.value = false
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
            error.value = `Run failed with status: ${status}`
          }
          break
        }
      }
    } catch (err) {
      console.warn('pollRunStatus error:', err)
    }
  }
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
    }
  } catch (err) {
    console.warn('fetchRunIO error:', err)
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
  } catch (err) {
    console.warn('fetchRunEvals error:', err)
  }
}

async function promoteWinner(variantName: string) {
  if (!savedGroupId.value) return
  promotingName.value = variantName
  error.value = null

  const variant = variants.value.find(v => v.name === variantName)
  if (!variant) {
    promotingName.value = null
    return
  }

  try {
    const snapshot = snapshotId.value || ''
    const variantDef = {
      snapshot_id: snapshot,
      name: variant.name.trim(),
      weight: 1.0,
      run_context_overrides: {
        model_backend_id: variant.modelBackendId,
      },
      eval_definition_ids: [] as string[],
    }

    const { error: err } = await api.PUT('/api/v1/variant-groups/{group_id}', {
      params: { path: { group_id: savedGroupId.value } },
      body: {
        pipeline_id: selectedPipelineId.value,
        name: `${groupName.value.trim()} (default: ${variantName})`,
        description: groupDescription.value || null,
        variants: [variantDef] as unknown as components['schemas']['VariantDef'][],
      },
    })

    if (err) {
      error.value = `Failed to promote variant: ${JSON.stringify(err)}`
    }
  } catch (e: unknown) {
    error.value = `Failed to promote variant: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    promotingName.value = null
  }
}

onBeforeUnmount(() => {
  isUnmounted.value = true
})

onMounted(async () => {
  await Promise.all([
    fetchPipelines(),
    fetchVariantGroups(),
    fetchModelBackends(),
  ])
  loading.value = false
})

watch(selectedPipelineId, async (id) => {
  if (id) {
    selectedGroupId.value = ''
    groupName.value = ''
    groupDescription.value = ''
    variants.value = []
    runEntries.value.clear()
    savedGroupId.value = null
    await fetchSnapshotForPipeline(id)
  }
})

watch(selectedGroupId, async (id) => {
  if (id) {
    const group = variantGroups.value.find(g => g.id === id)
    if (group) {
      groupName.value = group.name
      groupDescription.value = group.description || ''
      const formVariants: VariantForm[] = group.variants.map((v: unknown) => {
        const vd = v as { name: string; weight: number; run_context_overrides?: Record<string, unknown> }
        return {
          id: crypto.randomUUID(),
          name: vd.name,
          modelBackendId: ((vd.run_context_overrides || {}) as Record<string, string>)['model_backend_id'] ?? null,
          weight: Math.round((vd.weight || 0) * 100),
        }
      })
      variants.value = formVariants
      savedGroupId.value = group.id
    }
  } else {
    groupName.value = ''
    groupDescription.value = ''
    variants.value = []
    savedGroupId.value = null
  }
})

async function fetchPipelines() {
  try {
    const { data, error: err } = await api.GET('/api/v1/pipelines')
    if (err) return
    const listResp = data as unknown as { items: PipelineItem[]; total: number; page: number; page_size: number }
    pipelines.value = listResp.items ?? []
    if (listResp.items.length > 0 && !selectedPipelineId.value) {
      selectedPipelineId.value = listResp.items[0].id
    }
  } catch {
    // Non-critical
  }
}

async function fetchVariantGroups() {
  try {
    const { data, error: err } = await api.GET('/api/v1/variant-groups')
    if (err) return
    variantGroups.value = (Array.isArray(data) ? data : (data as any)?.items ?? []) as unknown as VariantGroup[]
  } catch {
    // Non-critical
  }
}

async function fetchModelBackends() {
  try {
    const { data, error: err } = await api.GET('/api/v1/model-backends')
    if (err) return
    const resp = data as unknown as { items: ModelBackend[]; total: number; page: number; page_size: number }
    modelBackends.value = resp.items ?? []
  } catch {
    // Non-critical
  }
}

async function fetchSnapshotForPipeline(pipelineId: string) {
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: { path: { pipeline_id: pipelineId } },
    })
    if (data) {
      const resp = data as unknown as { items: Array<{ id: string; tag: string | null }>; total: number }
      if (resp.items.length > 0) {
        snapshotId.value = resp.items[0].id
      }
    }
  } catch {
    // Non-critical
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}
</script>
