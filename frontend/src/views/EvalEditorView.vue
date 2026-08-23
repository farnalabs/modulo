<template>
  <FeatureGate feature-name="eval_system" required-tier="community" show-disabled>

    <PageTabs :tabs="[
      { label: $t('views.EvalEditorView.tab_evals'), to: '/evals/editor' },
      { label: $t('views.EvalEditorView.tab_proposals'), to: '/evals/proposals' },
      { label: $t('views.EvalEditorView.tab_variants'), to: '/variants/compare' },
      { label: $t('views.EvalEditorView.tab_ab_test'), to: '/variants/ab-test' },
    ]" />

    <div class="page-wide">
    <PageHeader :title="$t('views.EvalEditorView.eval_editor')" :subtitle="$t('views.EvalEditorView.create_and_manage_eval_definitions')" />

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadAll" />

    <template v-else>
      <div class="grid gap-6 lg:grid-cols-2">
        <div>
          <label for="evaleditorview-field-8" class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.pipeline') }}</label>
          <Select
  :aria-label="$t('views.EvalEditorView.pipeline_aria')"
  v-model="selectedPipelineId"
  @update:model-value="onPipelineChange"
  :placeholder="$t('views.EvalEditorView.select_a_pipeline')"
  data-testid="eval-editor-pipeline"
  class="w-full"
  :options="[{ value: '__all__', label: $t('common.none') }, ...pipelines.map(p => ({ value: p.id, label: p.name }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
        </div>

        <div>
          <label for="evaleditorview-field-7" class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.node') }} <span class="text-muted-foreground">({{ $t('views.EvalEditorView.node_optional') }})</span></label>
          <Select
  :aria-label="$t('views.EvalEditorView.node_aria')"
  v-model="form.node_id"
  :disabled="!selectedPipelineId || nodesLoading"
  :placeholder="$t('views.EvalEditorView.select_a_node')"
  data-testid="eval-editor-node"
  class="w-full"
  :options="[{ value: '__all__', label: $t('views.EvalEditorView.all_pipeline_outputs') }, ...nodes.map(n => ({ value: n.id, label: n.label || n.node_type || shortId(n.id) }))]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
          <div v-if="nodesLoading" class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.loading_nodes') }}</div>
          <div v-if="nodesError" class="mt-1 text-xs text-destructive">{{ nodesError }}</div>
        </div>
      </div>

      <div class="grid gap-8 lg:grid-cols-5">
        <div class="lg:col-span-3">
          <div class="rounded-lg border bg-card p-6 shadow-sm">
            <h2 class="mb-4 text-base font-semibold">{{ editingEvalId ? $t('views.EvalEditorView.edit_eval') : $t('views.EvalEditorView.new_eval') }}</h2>

            <div class="space-y-4">
              <div>
                <label for="evaleditorview-field-6" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.name') }}</label>
                <input id="evaleditorview-field-6"
                  v-model="form.name"
                  type="text"
                  data-testid="eval-editor-name"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="$t('views.EvalEditorView.name_placeholder')"
                />
              </div>

              <div>
                <label for="evaleditorview-field-5" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.eval_type') }}</label>
                <Select
  :aria-label="$t('views.EvalEditorView.eval_type_aria')"
  v-model="form.eval_type"
  placeholder="llm_judge"
  data-testid="eval-editor-eval-type"
  class="w-full"
  :options="[{ value: 'llm_judge', label: $t('views.EvalEditorView.llm_judge') }, { value: 'regex', label: $t('views.EvalEditorView.regex') }, { value: 'json_schema', label: $t('views.EvalEditorView.json_schema') }, { value: 'custom_function', label: $t('views.EvalEditorView.custom_function') }]"
  option-label="label"
  option-value="value"
>
  <template #option="{ option }">
    <span :data-value="option.value">{{ option.label }}</span>
  </template>
</Select>
              </div>

              <div>
                <label for="evaleditorview-field-4" class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.config_json') }} <span class="text-muted-foreground">{{ $t('views.EvalEditorView.config_json_hint') }}</span></label>
                <textarea id="evaleditorview-field-4"
                  v-model="form.config_json"
                  rows="6"
                  data-testid="eval-editor-config"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="configPlaceholder"
                />
                <div v-if="configParseError" class="mt-1 text-xs text-destructive">{{ configParseError }}</div>
              </div>

              <div>
                <label for="evaleditorview-field-3" class="mb-1 block text-sm font-medium">
                  {{ $t('views.EvalEditorView.pass_threshold') }}
                  <span class="text-muted-foreground">({{ form.pass_threshold.toFixed(2) }})</span>
                </label>
                <div class="flex items-center gap-3">
                  <span class="text-xs text-muted-foreground">0.0</span>
                  <input id="evaleditorview-field-3"
                    v-model.number="form.pass_threshold"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    data-testid="eval-editor-pass-threshold"
                    class="h-2 w-full cursor-pointer appearance-none rounded-full bg-input accent-primary"
                    :aria-label="$t('views.EvalEditorView.pass_threshold_aria')"
                  />
                  <span class="text-xs text-muted-foreground">1.0</span>
                </div>
              </div>

              <div>
                <span class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.failure_behaviour') }}</span>
                <div class="flex items-center gap-4">
                  <label for="evaleditorview-field-2" class="flex cursor-pointer items-center gap-2 text-sm">
                    <input id="evaleditorview-field-2"
                      v-model="form.failure_behaviour"
                      type="radio"
                      value="warn"
                      data-testid="eval-editor-failure-warn"
                      class="accent-primary"
                    />
                    {{ $t('views.EvalEditorView.warn') }}
                  </label>
                  <label for="evaleditorview-field-1" class="flex cursor-pointer items-center gap-2 text-sm">
                    <input id="evaleditorview-field-1"
                      v-model="form.failure_behaviour"
                      type="radio"
                      value="block"
                      data-testid="eval-editor-failure-block"
                      class="accent-primary"
                    />
                    {{ $t('views.EvalEditorView.block') }}
                  </label>
                </div>
                <p class="mt-1 text-xs text-muted-foreground">
                  {{ form.failure_behaviour === 'warn' ? $t('views.EvalEditorView.warn_description') : $t('views.EvalEditorView.block_description') }}
                </p>
              </div>

              <div class="flex items-center gap-2 pt-2">
              <Button :disabled="!canSave || saving" data-testid="eval-editor-save" @click="saveEval">
                {{ saving ? $t('common.saving') : editingEvalId ? $t('views.EvalEditorView.update') : $t('common.save') }}
              </Button>
                <button
                  type="button"
                  v-if="editingEvalId"
                  data-testid="eval-editor-cancel"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  @click="resetForm"
                >
                  {{ $t('common.cancel') }}
                </button>
              </div>

              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div v-if="formSuccess" class="text-sm text-success">{{ formSuccess }}</div>
            </div>
          </div>
        </div>

        <div class="lg:col-span-2">
          <h2 class="mb-4 text-base font-semibold">{{ $t('views.EvalEditorView.existing_evals') }}</h2>

          <div v-if="evalsError" class="mb-2 text-sm text-destructive">{{ evalsError }}</div>

          <div v-if="!selectedPipelineId" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            {{ $t('views.EvalEditorView.prompt_select_pipeline') }}
          </div>

          <div v-else-if="evalsLoading" class="flex items-center justify-center py-8">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>

          <EmptyState
            v-else-if="evals.length === 0"
            :title="$t('views.EvalEditorView.no_evals_yet')"
          />

          <div v-else class="space-y-2">
            <div
              v-for="ev in evals"
              :key="ev.id"
              class="rounded-lg border bg-card p-4 shadow-sm"
            >
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0 flex-1">
                  <p class="truncate font-medium">{{ ev.name }}</p>
                  <div class="mt-1 flex flex-wrap items-center gap-2">
                    <span class="inline-block rounded bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">{{ ev.eval_type }}</span>
                    <span
                      class="inline-block rounded px-2 py-0.5 text-xs font-medium"
                      :class="ev.failure_behaviour === 'block' ? 'bg-destructive/10 text-destructive' : 'bg-pending/10 text-pending'"
                    >
                      {{ ev.failure_behaviour }}
                    </span>
                    <span v-if="ev.pass_threshold != null" class="text-xs text-muted-foreground">
                      {{ $t('views.EvalEditorView.threshold', { value: ev.pass_threshold.toFixed(2) }) }}
                    </span>
                    <span v-if="ev.node_id" class="text-xs text-muted-foreground font-mono">
                      {{ $t('views.EvalEditorView.node_prefix', { id: shortId(ev.node_id) }) }}
                    </span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    type="button"
                    data-testid="eval-editor-edit"
                    :aria-label="$t('common.edit')"
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    :title="$t('common.edit')"
                    @click="startEdit(ev)"
                  >
                    <Pencil class="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    v-if="deletingEvalId !== ev.id"
                    data-testid="eval-editor-delete"
                    :aria-label="$t('common.delete')"
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    :title="$t('common.delete')"
                    @click="confirmDelete(ev.id)"
                  >
                    <Trash2 class="h-4 w-4" aria-hidden="true" />
                  </button>
                  <div v-else class="flex items-center gap-1">
                    <button
                      type="button"
                      :disabled="deleting"
                      data-testid="eval-editor-confirm-delete"
                      class="rounded bg-destructive px-2 py-1 text-xs font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                      @click="deleteEval(ev.id)"
                    >
                      {{ deleting ? $t('common.deleting') : $t('common.confirm') }}
                    </button>
                    <button
                      type="button"
                      data-testid="eval-editor-cancel-delete"
                      class="rounded px-2 py-1 text-xs font-medium hover:bg-accent"
                      @click="deletingEvalId = null"
                    >
                      {{ $t('common.no') }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
  </FeatureGate>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { useDataFetch } from '../composables/useDataFetch'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { shortId } from '../utils/format'
import { formatApiError } from '../lib/api/formatError'
import EmptyState from '../components/shared/EmptyState.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import PageHeader from '../components/shared/PageHeader.vue'
import Button from 'primevue/button'
import Select from 'primevue/select'
import PageTabs from "../components/PageTabs.vue"
import { Pencil, Trash2 } from '@lucide/vue'
import { api } from '../lib/api/client'

const { t } = useI18n()

const planStore = usePlanStore()

interface PipelineItem {
  id: string
  name: string
  description: string | null
}

interface GraphNode {
  id: string
  node_type: string
  label: string | null
  agent_id: string | null
  position: { x: number; y: number }
}

interface EvalDefinition {
  id: string
  pipeline_id: string
  node_id: string | null
  name: string
  eval_type: string
  config_json: Record<string, unknown>
  failure_behaviour: string
  pass_threshold: number | null
  suite_id: string | null
  created_by: string
}

const selectedPipelineId = ref('__all__')
const nodes = ref<GraphNode[]>([])
const nodesLoading = ref(false)

const nodesError = ref<string | null>(null)
const evalsError = ref<string | null>(null)

const form = reactive({
  name: '',
  node_id: '__all__',
  eval_type: 'llm_judge',
  config_json: '{}',
  pass_threshold: 0.8,
  failure_behaviour: 'warn',
})

const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const editingEvalId = ref<string | null>(null)

const evals = ref<EvalDefinition[]>([])
const evalsLoading = ref(false)

const deletingEvalId = ref<string | null>(null)
const deleting = ref(false)

const configParseError = computed(() => {
  if (!form.config_json.trim()) return null
  try {
    JSON.parse(form.config_json)
    return null
  } catch {
    return t('views.EvalEditorView.invalid_json')
  }
})

const configPlaceholder = computed(() => {
  return t(`views.EvalEditorView.configPlaceholder.${form.eval_type || 'llm_judge'}`)
})

const canSave = computed(() => {
  return (
    selectedPipelineId.value && selectedPipelineId.value !== '__all__' &&
    form.name.trim() &&
    form.eval_type &&
    !configParseError.value
  )
})

function resetForm() {
  form.name = ''
  form.node_id = '__all__'
  form.eval_type = 'llm_judge'
  form.config_json = '{}'
  form.pass_threshold = 0.8
  form.failure_behaviour = 'warn'
  editingEvalId.value = null
  formError.value = null
  formSuccess.value = null
}

const { loading, error: pageError, data: pipelinesResp, load: loadAll } = useDataFetch(
  async () => {
    const { data } = await api.GET('/api/v1/pipelines')
    return { data: (data as any)?.items ?? [], error: undefined }
  },
  { initialValue: [] as PipelineItem[] },
)

const pipelines = computed(() => (pipelinesResp.value ?? []) as PipelineItem[])

async function loadNodes() {
  if (!selectedPipelineId.value || selectedPipelineId.value === '__all__') {
    nodes.value = []
    return
  }
  nodesLoading.value = true
  nodesError.value = null
  try {
    const { data } = await api.GET('/api/v1/pipelines/{pipeline_id}/graph', {
      params: { path: { pipeline_id: selectedPipelineId.value } },
    })
    nodes.value = (data as any)?.nodes ?? []
  } catch (e) {
    nodes.value = []
    nodesError.value = t('views.EvalEditorView.failed_to_load_nodes')
    console.warn('Failed to load nodes:', e)
  } finally {
    nodesLoading.value = false
  }
}

async function loadEvals() {
  if (!selectedPipelineId.value || selectedPipelineId.value === '__all__') {
    evals.value = []
    return
  }
  evalsLoading.value = true
  evalsError.value = null
  try {
    const { data } = await api.GET('/api/v1/evals', {
      params: { query: { pipeline_id: selectedPipelineId.value } as any },
    })
    evals.value = (data as any)?.items ?? []
  } catch {
    evals.value = []
    evalsError.value = t('views.EvalEditorView.failed_to_load_evals')
  } finally {
    evalsLoading.value = false
  }
}

async function onPipelineChange() {
  resetForm()
  deletingEvalId.value = null
  nodesError.value = null
  evalsError.value = null
  await Promise.all([loadNodes(), loadEvals()])
}

async function saveEval() {
  if (!canSave.value) return

  saving.value = true
  formError.value = null
  formSuccess.value = null

  let configParsed: Record<string, unknown> = {}
  try {
    configParsed = JSON.parse(form.config_json)
  } catch {
    formError.value = t('views.EvalEditorView.config_json_is_invalid')
    saving.value = false
    return
  }

  const body = {
    pipeline_id: selectedPipelineId.value,
    node_id: form.node_id === '__all__' ? null : form.node_id,
    name: form.name.trim(),
    eval_type: form.eval_type,
    config_json: configParsed,
    failure_behaviour: form.failure_behaviour,
    pass_threshold: form.pass_threshold,
  }
  try {
    if (editingEvalId.value) {
      await api.PUT('/api/v1/evals/{eval_id}', {
        params: { path: { eval_id: editingEvalId.value } },
        body,
      })
      formSuccess.value = t('views.EvalEditorView.eval_updated')
    } else {
      await api.POST('/api/v1/evals', { body })
      formSuccess.value = t('views.EvalEditorView.eval_created')
    }
    resetForm()
    await loadEvals()
    setTimeout(() => { formSuccess.value = null }, 2000)
  } catch (e: unknown) {
    formError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function startEdit(ev: EvalDefinition) {
  editingEvalId.value = ev.id
  form.name = ev.name
  form.node_id = ev.node_id ?? '__all__'
  form.eval_type = ev.eval_type
  form.config_json = JSON.stringify(ev.config_json, null, 2)
  form.pass_threshold = ev.pass_threshold ?? 0.8
  form.failure_behaviour = ev.failure_behaviour
  formError.value = null
  formSuccess.value = null
}

function confirmDelete(id: string) {
  deletingEvalId.value = id
  deleting.value = false
}

async function deleteEval(id: string) {
  deleting.value = true
  try {
    await api.DELETE('/api/v1/evals/{eval_id}', {
      params: { path: { eval_id: id } },
    })
    evals.value = evals.value.filter(e => e.id !== id)
    deletingEvalId.value = null
  } catch (e: unknown) {
    const errMsg = formatApiError(e)
    if (errMsg.toLowerCase().includes('not found') || errMsg.includes('404')) {
      formError.value = t('views.EvalEditorView.eval_already_deleted')
    } else {
      formError.value = errMsg
    }
  } finally {
    deleting.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
