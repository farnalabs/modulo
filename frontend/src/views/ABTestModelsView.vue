// Generalised variant comparison creator (FAR-332).
//
// Builds a set of self-contained variant rows against a single pipeline, then
// fires them all at once as one all-or-nothing batch. Each row carries:
//   - a stable per-row id (crypto.randomUUID)
//   - a unique, non-blank label
//   - a snapshot picker (defaults to the pipeline's current snapshot)
//   - first-class concrete pickers for the common override dimensions
//     (model backend, prompt version) that translate into the underlying
//     run_context_overrides keys
//   - unknown override keys are rejected (no abstract key/value editor in v1)

<template>
  <PageTabs :tabs="[
    { label: 'Evals', to: '/evals/editor' },
    { label: 'Proposals', to: '/evals/proposals' },
    { label: 'Variants', to: '/variants/compare' },
    { label: 'AB Test', to: '/variants/ab-test' },
  ]" />
  <div class="page-wide">
    <LoadingSpinner v-if="loading" />
    <ErrorAlert v-else-if="error" :message="error" />
    <template v-else>
      <PageHeader
        :title="$t('views.variantCreator.title')"
        :subtitle="$t('views.variantCreator.subtitle')"
      />

      <div class="mb-6 flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-sm">
          <span class="text-muted-foreground">{{ $t('views.variantCreator.pipeline') }}</span>
          <Select
            v-model="selectedPipelineId"
            :placeholder="$t('views.variantCreator.select_a_pipeline')"
            data-testid="variant-builder-pipeline-select"
            :aria-label="$t('views.variantCreator.aria_pipeline')"
            class="min-w-[280px]"
            :options="pipelines.map(p => ({ value: p.id, label: p.name }))"
            option-label="label"
            option-value="value"
          >
            <template #option="{ option }">
              <span :data-value="option.value">{{ option.label }}</span>
            </template>
          </Select>
        </label>

        <label class="flex items-center gap-2 text-sm">
          <span class="text-muted-foreground">{{ $t('views.variantCreator.comparison_name') }}</span>
          <input
            v-model="comparisonName"
            data-testid="variant-builder-name"
            type="text"
            :placeholder="$t('views.variantCreator.comparison_name_placeholder')"
            class="w-72 rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
      </div>

      <template v-if="selectedPipelineId">
        <EmptyState
          v-if="snapshots.length === 0"
          :title="$t('views.variantCreator.no_snapshots')"
          :description="$t('views.variantCreator.no_snapshots_hint')"
        >
          <Button
            as="router-link"
            :to="`/pipelines/${selectedPipelineId}/editor`"
            data-testid="variant-builder-create-snapshot"
          >
            {{ $t('views.variantCreator.create_snapshot') }}
          </Button>
        </EmptyState>

        <section v-else class="space-y-4 rounded-lg border bg-card p-6">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <h2 class="text-base font-semibold tracking-tight">
                {{ $t('views.variantCreator.variants_title') }}
              </h2>
              <p class="text-xs text-muted-foreground">
                {{ $t('views.variantCreator.advanced_overrides_note') }}
              </p>
            <div class="flex items-center gap-3">
              <output
                data-testid="variant-builder-headroom"
                class="text-xs text-muted-foreground"
              >
                {{ $t('views.variantCreator.headroom', { used: variants.length, max: MAX_VARIANTS }) }}
              </output>
              <Button
                :disabled="variants.length >= MAX_VARIANTS"
                size="small"
                data-testid="variant-builder-add"
                class="px-3 py-1.5"
                @click="addVariant"
              >
                {{ $t('views.variantCreator.add_variant') }}
              </Button>
            </div>
          </div>

          <div class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead>
                <tr class="border-b text-xs uppercase text-muted-foreground">
                  <th class="py-2 pr-3 font-medium">{{ $t('views.variantCreator.label') }}</th>
                  <th class="py-2 pr-3 font-medium">{{ $t('views.variantCreator.snapshot') }}</th>
                  <th class="py-2 pr-3 font-medium">{{ $t('views.variantCreator.model_backend') }}</th>
                  <th class="py-2 pr-3 font-medium">{{ $t('views.variantCreator.prompt_version') }}</th>
                  <th class="py-2 font-medium text-right">{{ $t('views.variantCreator.actions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(v, i) in variants"
                  :key="v.id"
                  class="border-b align-top hover:bg-muted/30"
                >
                  <td class="py-2 pr-3">
                    <input
                      v-model="v.label"
                      :data-testid="`variant-builder-label-${i}`"
                      type="text"
                      :placeholder="$t('views.variantCreator.label_placeholder')"
                      :aria-label="`${t('views.variantCreator.label')} ${i + 1}`"
                      class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    />
                    <p v-if="rowError(i)" class="mt-1 text-xs text-destructive" role="alert">
                      {{ rowError(i) }}
                    </p>
                  </td>
                  <td class="py-2 pr-3">
                    <Select
                      v-model="v.snapshotId"
                      :placeholder="$t('views.variantCreator.select_snapshot')"
                      :data-testid="`variant-builder-snapshot-${i}`"
                      :aria-label="$t('views.variantCreator.aria_snapshot')"
                      class="w-full"
                      :options="snapshotOptions"
                      option-label="label"
                      option-value="value"
                    >
                      <template #option="{ option }">
                        <span :data-value="option.value">{{ option.label }}</span>
                      </template>
                    </Select>
                  </td>
                  <td class="py-2 pr-3">
                    <Select
                      v-model="v.modelBackendId"
                      :placeholder="$t('views.variantCreator.select_model')"
                      :data-testid="`variant-builder-model-${i}`"
                      :aria-label="$t('views.variantCreator.aria_model_backend')"
                      class="w-full"
                      :options="modelBackendOptions"
                      option-label="label"
                      option-value="value"
                    >
                      <template #option="{ option }">
                        <span :data-value="option.value">{{ option.label }}</span>
                      </template>
                    </Select>
                  </td>
                  <td class="py-2 pr-3">
                    <Select
                      v-model="v.promptVersion"
                      :placeholder="$t('views.variantCreator.select_prompt_version')"
                      :data-testid="`variant-builder-prompt-${i}`"
                      :aria-label="$t('views.variantCreator.aria_prompt_version')"
                      class="w-full"
                      :options="promptVersionOptions"
                      option-label="label"
                      option-value="value"
                    >
                      <template #option="{ option }">
                        <span :data-value="option.value">{{ option.label }}</span>
                      </template>
                    </Select>
                  </td>
                  <td class="py-2 text-right whitespace-nowrap">
                    <button
                      type="button"
                      :data-testid="`variant-builder-duplicate-${i}`"
                      :disabled="variants.length >= MAX_VARIANTS"
                      class="mr-2 text-xs text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                      :aria-label="$t('views.variantCreator.duplicate')"
                      @click="duplicateVariant(i)"
                    >
                      {{ $t('views.variantCreator.duplicate') }}
                    </button>
                    <button
                      type="button"
                      :data-testid="`variant-builder-remove-${i}`"
                      class="text-xs text-destructive hover:underline"
                      :aria-label="$t('views.variantCreator.remove')"
                      @click="removeVariant(i)"
                    >
                      {{ $t('views.variantCreator.remove') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <output
            v-if="variants.length < 2"
            data-testid="variant-builder-min-two"
            class="text-sm text-muted-foreground"
          >
            {{ $t('views.variantCreator.min_two_hint') }}
          </output>

          <ErrorAlert
            v-if="fireError"
            :message="fireError"
            data-testid="variant-builder-fire-error"
            class="mb-3"
          />

          <div class="flex flex-wrap items-center gap-3 pt-2">
            <Button
              :disabled="!canFire || firing"
              data-testid="variant-builder-fire"
              class="px-5 py-2"
              @click="openFireDialog"
            >
              <span v-if="firing" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
              {{ firing ? $t('views.variantCreator.firing') : $t('views.variantCreator.fire_comparison') }}
            </Button>
            <span v-if="canFire" class="text-xs text-muted-foreground">
              {{ $t('views.variantCreator.fires_n_runs', { count: variants.length }) }}
            </span>
          </div>
        </section>
      </template>

      <EmptyState
        v-else-if="!loading && pipelines.length === 0"
        :title="$t('views.variantCreator.no_pipelines')"
        :description="$t('views.variantCreator.no_pipelines_hint')"
      />
    </template>
  </div>

  <Dialog
    v-if="showFireDialog"
    :visible="showFireDialog"
    :modal="true"
    :dismissable-mask="true"
    data-testid="variant-builder-confirm"
    @update:visible="showFireDialog = false"
  >
    <template #header>
      <div class="text-lg font-semibold">{{ $t('views.variantCreator.confirm_title') }}</div>
    </template>
    <p class="text-sm text-muted-foreground">
      {{ $t('views.variantCreator.confirm_body', { count: variants.length }) }}
    </p>
    <ErrorAlert v-if="fireError" :message="fireError" class="mt-3" />
    <template #footer>
      <div class="flex justify-end gap-3">
        <Button
          severity="secondary"
          outlined
          :disabled="firing"
          data-testid="variant-builder-cancel"
          @click="showFireDialog = false"
        >
          {{ $t('views.variantCreator.cancel') }}
        </Button>
        <Button
          severity="primary"
          :disabled="firing"
          data-testid="variant-builder-confirm-fire"
          @click="fireBatch"
        >
          <span v-if="firing" class="inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
          {{ firing ? $t('views.variantCreator.firing') : $t('views.variantCreator.confirm_fire') }}
        </Button>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { api } from '../lib/api/client'
import type { components } from '../lib/api/client'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import PageTabs from '../components/PageTabs.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import EmptyState from '../components/shared/EmptyState.vue'
import Dialog from 'primevue/dialog'
import Select from 'primevue/select'
import { formatApiError } from '../lib/api/formatError'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const MAX_VARIANTS = 10

type PipelineItem = components['schemas']['PipelineResponse']
type ModelBackend = components['schemas']['ModelBackendResponse']

interface VariantForm {
  id: string
  label: string
  snapshotId: string | null
  modelBackendId: string | null
  promptVersion: string | null
}

const pipelines = ref<PipelineItem[]>([])
const modelBackends = ref<ModelBackend[]>([])
const selectedPipelineId = ref<string>('')
const comparisonName = ref('')
const variants = ref<VariantForm[]>([])
const snapshots = ref<Array<{ id: string; snapshot_version: number; tag: string | null }>>([])
const promptVersionOptions = ref<Array<{ value: string; label: string }>>([])
const error = ref<string | null>(null)
const fireError = ref<string | null>(null)
const firing = ref(false)
const showFireDialog = ref(false)

const { loading: pipelinesLoading, data: pipelinesData } = useDataFetch(
  () => api.GET('/api/v1/pipelines'),
  { immediate: true }
)
const { loading: backendsLoading, data: backendsData } = useDataFetch(
  () => api.GET('/api/v1/model-backends'),
  { immediate: true }
)

watch(() => backendsData.value, (data) => {
  if (data) {
    const resp = data as unknown as { items: ModelBackend[] }
    modelBackends.value = resp.items ?? []
  }
})

const loading = computed(() => pipelinesLoading.value || backendsLoading.value)

const modelBackendOptions = computed(() =>
  modelBackends.value.map(mb => ({ value: mb.id, label: `${mb.display_name} (${mb.provider})` }))
)

const snapshotOptions = computed(() =>
  snapshots.value.map(s => ({
    value: s.id,
    label: s.tag ? `v${s.snapshot_version} (${s.tag})` : `v${s.snapshot_version}`,
  }))
)

const canFire = computed(() => {
  if (!selectedPipelineId.value) return false
  if (variants.value.length < 2) return false
  return variants.value.every(v => v.label.trim() && v.snapshotId)
})

const labels = computed(() => variants.value.map(v => v.label.trim().toLowerCase()))

function rowError(index: number): string | null {
  const v = variants.value[index]
  if (!v) return null
  if (!v.label.trim()) return t('views.variantCreator.error_blank_label')
  if (labels.value.indexOf(v.label.trim().toLowerCase()) !== labels.value.lastIndexOf(v.label.trim().toLowerCase())) {
    return t('views.variantCreator.error_duplicate_label')
  }
  if (!v.snapshotId) return t('views.variantCreator.error_need_snapshot')
  return null
}

function defaultSnapshotId(): string | null {
  return snapshots.value[0]?.id ?? null
}

function addVariant() {
  if (variants.value.length >= MAX_VARIANTS) return
  variants.value.push({
    id: crypto.randomUUID(),
    label: `${t('views.variantCreator.variant_prefix')} ${variants.value.length + 1}`,
    snapshotId: defaultSnapshotId(),
    modelBackendId: null,
    promptVersion: null,
  })
}

function nextUniqueLabel(base: string): string {
  const root = base.trim()
  const existing = new Set(variants.value.map(v => v.label.trim().toLowerCase()))
  if (!existing.has(root.toLowerCase())) return root
  let n = 2
  while (existing.has(`${root} (${n})`.toLowerCase())) n += 1
  return `${root} (${n})`
}

function duplicateVariant(index: number) {
  if (variants.value.length >= MAX_VARIANTS) return
  const src = variants.value[index]
  if (!src) return
  variants.value.push({
    id: crypto.randomUUID(),
    label: nextUniqueLabel(`${src.label.trim()} (copy)`),
    snapshotId: src.snapshotId,
    modelBackendId: src.modelBackendId,
    promptVersion: src.promptVersion,
  })
}

function removeVariant(index: number) {
  variants.value.splice(index, 1)
}

function buildOverrides(v: VariantForm): Record<string, unknown> {
  const o: Record<string, unknown> = {}
  if (v.modelBackendId) o.model_backend_id = v.modelBackendId
  if (v.promptVersion) o.prompt_version = v.promptVersion
  return o
}

function preflight(): boolean {
  for (let i = 0; i < variants.value.length; i += 1) {
    if (rowError(i)) return false
  }
  return true
}

function openFireDialog() {
  if (!preflight()) return
  fireError.value = null
  showFireDialog.value = true
}

async function fireBatch() {
  if (!preflight()) {
    showFireDialog.value = false
    return
  }
  if (variants.value.length < 2) {
    fireError.value = t('views.variantCreator.min_two_hint')
    showFireDialog.value = false
    return
  }
  firing.value = true
  fireError.value = null
  try {
    const variantDefs = variants.value.map(v => ({
      snapshot_id: v.snapshotId as string,
      name: v.label.trim(),
      weight: 1.0,
      run_context_overrides: buildOverrides(v),
      eval_definition_ids: [],
    }))

    const { data: groupData, error: createErr } = await api.POST('/api/v1/variant-groups', {
      body: {
        pipeline_id: selectedPipelineId.value,
        name: comparisonName.value.trim() || `${t('views.variantCreator.comparison_name_auto')} ${new Date().toISOString()}`,
        description: null,
        variants: variantDefs as unknown as components['schemas']['VariantDef'][],
        selection_strategy: 'weighted',
        max_concurrent_runs: 10,
        degraded_evals: false,
      },
    })
    if (createErr) {
      fireError.value = `${t('views.variantCreator.failed_to_create_group')} ${JSON.stringify(createErr)}`
      return
    }
    if (!groupData) {
      fireError.value = t('views.variantCreator.error_unexpected')
      return
    }

    const groupId = (groupData as unknown as { id: string }).id

    const { data: batchData, error: runErr } = await api.POST('/api/v1/variant-groups/{group_id}/batch-run', {
      params: { path: { group_id: groupId } },
      body: {},
    })
    if (runErr) {
      fireError.value = `${t('views.variantCreator.failed_to_run')} ${JSON.stringify(runErr)}`
      return
    }
    if (!batchData) {
      fireError.value = t('views.variantCreator.error_unexpected')
      return
    }

    const firedRuns = (batchData as unknown as { runs: Array<{ run_id: string; variant_name: string }> }).runs ?? []

    showFireDialog.value = false
    router.push({
      name: 'variant-compare-detail',
      params: { batchId: groupId },
      state: { firedRuns },
    })
  } catch (e: unknown) {
    fireError.value = `${t('views.variantCreator.failed_to_run')} ${formatApiError(e)}`
  } finally {
    firing.value = false
  }
}

async function fetchSnapshots(pipelineId: string) {
  snapshots.value = []
  promptVersionOptions.value = []
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/snapshots', {
      params: { path: { pipeline_id: pipelineId } },
    })
    if (data) {
      const resp = data as unknown as { items: Array<{ id: string; snapshot_version: number; tag: string | null }> }
      snapshots.value = resp.items ?? []
    }
  } catch (e) {
    console.warn('Failed to fetch snapshots', e)
  }
  void fetchPromptVersions(pipelineId)
}

async function fetchPromptVersions(pipelineId: string) {
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: pipelineId } },
    })
    const nodes = (data as unknown as { nodes?: Array<{ agent_id?: string | null }> })?.nodes ?? []
    const agentIds = [...new Set(nodes.map(n => n.agent_id).filter((a): a is string => Boolean(a)))]

    const options: Array<{ value: string; label: string }> = []
    for (const agentId of agentIds) {
      const { data: prompts } = await api.GET('/api/v1/agents/{agent_id}/prompts', {
        params: { path: { agent_id: agentId } },
      })
      const list = (prompts as unknown as Array<{ version: string }> | null) ?? []
      for (const p of list) {
        if (!options.some(o => o.value === p.version)) {
          options.push({ value: p.version, label: p.version })
        }
      }
    }
    promptVersionOptions.value = options
  } catch (e) {
    console.warn('Failed to fetch prompt versions', e)
  }
}

watch(() => pipelinesData.value, (data) => {
  if (data) {
    const listResp = data as unknown as { items: PipelineItem[] }
    pipelines.value = listResp.items ?? []

    const queryPipeline = typeof route.query.pipeline_id === 'string' ? route.query.pipeline_id : ''
    const deepLinked = queryPipeline && pipelines.value.some(p => p.id === queryPipeline)
    if (deepLinked) {
      selectedPipelineId.value = queryPipeline
    } else if (!selectedPipelineId.value && pipelines.value.length > 0) {
      selectedPipelineId.value = pipelines.value[0].id
    }
  }
})

watch(selectedPipelineId, async (id) => {
  if (id) {
    comparisonName.value = ''
    variants.value = []
    await fetchSnapshots(id)
  }
})
</script>
