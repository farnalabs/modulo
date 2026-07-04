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
      <h1 class="text-3xl font-bold tracking-tight">{{ $t('views.EvalEditorView.eval_editor') }}</h1>
      <p class="mt-1 text-muted-foreground">{{ $t('views.EvalEditorView.create_and_manage_eval_definitions') }}</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadAll" />

    <template v-else>
      <div class="grid gap-6 lg:grid-cols-2">
        <div>
          <label class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.pipeline') }}</label>
          <select
            v-model="selectedPipelineId"
            data-testid="eval-editor-pipeline"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="onPipelineChange"
          >
            <option value="">{{ $t('views.EvalEditorView.select_a_pipeline') }}</option>
            <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>

        <div>
          <label class="mb-1.5 block text-sm font-medium">{{ $t('views.EvalEditorView.node') }} <span class="text-muted-foreground">({{ $t('views.EvalEditorView.node_optional') }})</span></label>
          <select
            v-model="form.node_id"
            :disabled="!selectedPipelineId || nodesLoading"
            data-testid="eval-editor-node"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            <option value="">{{ $t('views.EvalEditorView.all_pipeline_outputs') }}</option>
            <option v-for="n in nodes" :key="n.id" :value="n.id">{{ n.label || n.node_type || shortId(n.id) }}</option>
          </select>
          <div v-if="nodesLoading" class="mt-1 text-xs text-muted-foreground">{{ $t('views.EvalEditorView.loading_nodes') }}</div>
          <div v-if="nodesError" class="mt-1 text-xs text-destructive">{{ nodesError }}</div>
        </div>
      </div>

      <div class="grid gap-8 lg:grid-cols-5">
        <div class="lg:col-span-3">
          <div class="rounded-lg border bg-card p-6 shadow-sm">
            <h2 class="mb-4 text-lg font-semibold">{{ editingEvalId ? $t('views.EvalEditorView.edit_eval') : $t('views.EvalEditorView.new_eval') }}</h2>

            <div class="space-y-4">
              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.name') }}</label>
                <input
                  v-model="form.name"
                  type="text"
                  data-testid="eval-editor-name"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="$t('views.EvalEditorView.name_placeholder')"
                />
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.eval_type') }}</label>
                <select
                  v-model="form.eval_type"
                  data-testid="eval-editor-eval-type"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value="llm_judge">{{ $t('views.EvalEditorView.llm_judge') }}</option>
                  <option value="regex">{{ $t('views.EvalEditorView.regex') }}</option>
                  <option value="json_schema">{{ $t('views.EvalEditorView.json_schema') }}</option>
                  <option value="custom_function">{{ $t('views.EvalEditorView.custom_function') }}</option>
                </select>
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.config_json') }} <span class="text-muted-foreground">(JSON)</span></label>
                <textarea
                  v-model="form.config_json"
                  rows="6"
                  data-testid="eval-editor-config"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  :placeholder="$t('views.EvalEditorView.config_placeholder')"
                />
                <div v-if="configParseError" class="mt-1 text-xs text-destructive">{{ configParseError }}</div>
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">
                  {{ $t('views.EvalEditorView.pass_threshold') }}
                  <span class="text-muted-foreground">({{ form.pass_threshold.toFixed(2) }})</span>
                </label>
                <div class="flex items-center gap-3">
                  <span class="text-xs text-muted-foreground">0.0</span>
                  <input
                    v-model.number="form.pass_threshold"
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    data-testid="eval-editor-pass-threshold"
                    class="h-2 w-full cursor-pointer appearance-none rounded-full bg-input accent-primary"
                  />
                  <span class="text-xs text-muted-foreground">1.0</span>
                </div>
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">{{ $t('views.EvalEditorView.failure_behaviour') }}</label>
                <div class="flex items-center gap-4">
                  <label class="flex cursor-pointer items-center gap-2 text-sm">
                    <input
                      v-model="form.failure_behaviour"
                      type="radio"
                      value="warn"
                      data-testid="eval-editor-failure-warn"
                      class="accent-primary"
                    />
                    {{ $t('views.EvalEditorView.warn') }}
                  </label>
                  <label class="flex cursor-pointer items-center gap-2 text-sm">
                    <input
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
                <button
                  :disabled="!canSave || saving"
                  data-testid="eval-editor-save"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  @click="saveEval"
                >
                  {{ saving ? $t('common.saving') : editingEvalId ? $t('views.EvalEditorView.update') : $t('common.save') }}
                </button>
                <button
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
          <h2 class="mb-4 text-lg font-semibold">{{ $t('views.EvalEditorView.existing_evals') }}</h2>

          <div v-if="evalsError" class="mb-2 text-sm text-destructive">{{ evalsError }}</div>

          <div v-if="!selectedPipelineId" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            {{ $t('views.EvalEditorView.prompt_select_pipeline') }}
          </div>

          <div v-else-if="evalsLoading" class="flex items-center justify-center py-8">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>

          <div v-else-if="evals.length === 0" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            {{ $t('views.EvalEditorView.no_evals_yet') }}
          </div>

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
                      threshold: {{ ev.pass_threshold.toFixed(2) }}
                    </span>
                    <span v-if="ev.node_id" class="text-xs text-muted-foreground">
                      node: {{ shortId(ev.node_id) }}
                    </span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    data-testid="eval-editor-edit"
                    :aria-label="$t('common.edit')"
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    :title="$t('common.edit')"
                    @click="startEdit(ev)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    v-if="deletingEvalId !== ev.id"
                    data-testid="eval-editor-delete"
                    :aria-label="$t('common.delete')"
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    :title="$t('common.delete')"
                    @click="confirmDelete(ev.id)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" />
                    </svg>
                  </button>
                  <div v-else class="flex items-center gap-1">
                    <button
                      :disabled="deleting"
                      data-testid="eval-editor-confirm-delete"
                      class="rounded bg-destructive px-2 py-1 text-xs font-medium text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                      @click="deleteEval(ev.id)"
                    >
                      {{ deleting ? '...' : $t('common.confirm') }}
                    </button>
                    <button
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
import { useApi } from '../composables/useApi'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { shortId } from '../utils/format'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import PageTabs from "../components/PageTabs.vue"

const { t } = useI18n()

const planStore = usePlanStore()
const { get, post, put, del } = useApi()

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

interface EvalListResponse {
  items: EvalDefinition[]
  total: number
  page: number
  page_size: number
}

interface GraphResponse {
  nodes: GraphNode[]
  edges: unknown[]
  validation_issues: string[]
}

const loading = ref(true)
const pageError = ref<string | null>(null)

const pipelines = ref<PipelineItem[]>([])
const selectedPipelineId = ref('')
const nodes = ref<GraphNode[]>([])
const nodesLoading = ref(false)

const nodesError = ref<string | null>(null)
const evalsError = ref<string | null>(null)

const form = reactive({
  name: '',
  node_id: '',
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

const canSave = computed(() => {
  return (
    selectedPipelineId.value &&
    form.name.trim() &&
    form.eval_type &&
    !configParseError.value
  )
})

function resetForm() {
  form.name = ''
  form.node_id = ''
  form.eval_type = 'llm_judge'
  form.config_json = '{}'
  form.pass_threshold = 0.8
  form.failure_behaviour = 'warn'
  editingEvalId.value = null
  formError.value = null
  formSuccess.value = null
}

async function loadPipelines() {
  try {
    const data = await get<{ items: PipelineItem[]; total: number; page: number; page_size: number }>('/api/v1/pipelines')
    pipelines.value = data.items ?? []
  } catch (e: unknown) {
    pageError.value = `${t('views.EvalEditorView.failed_to_load_pipelines')} ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadNodes() {
  if (!selectedPipelineId.value) {
    nodes.value = []
    return
  }
  nodesLoading.value = true
  nodesError.value = null
  try {
    const data = await get<GraphResponse>(`/api/v1/pipelines/${selectedPipelineId.value}/graph`)
    nodes.value = data.nodes ?? []
  } catch {
    nodes.value = []
    nodesError.value = t('views.EvalEditorView.failed_to_load_nodes')
  } finally {
    nodesLoading.value = false
  }
}

async function loadEvals() {
  if (!selectedPipelineId.value) {
    evals.value = []
    return
  }
  evalsLoading.value = true
  evalsError.value = null
  try {
    const data = await get<EvalListResponse>(`/api/v1/evals?pipeline_id=${selectedPipelineId.value}`)
    evals.value = data.items ?? []
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

  const body: Record<string, unknown> = {
    pipeline_id: selectedPipelineId.value,
    name: form.name.trim(),
    eval_type: form.eval_type,
    config_json: configParsed,
    failure_behaviour: form.failure_behaviour,
    pass_threshold: form.pass_threshold,
  }
  if (form.node_id) {
    body.node_id = form.node_id
  }

  try {
    if (editingEvalId.value) {
      await put<unknown>(`/api/v1/evals/${editingEvalId.value}`, body)
      formSuccess.value = t('views.EvalEditorView.eval_updated')
    } else {
      await post<unknown>('/api/v1/evals', body)
      formSuccess.value = t('views.EvalEditorView.eval_created')
    }
    resetForm()
    await loadEvals()
    setTimeout(() => { formSuccess.value = null }, 2000)
  } catch (e: unknown) {
    formError.value = e instanceof Error ? e.message : String(e)
  } finally {
    saving.value = false
  }
}

function startEdit(ev: EvalDefinition) {
  editingEvalId.value = ev.id
  form.name = ev.name
  form.node_id = ev.node_id ?? ''
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
    await del(`/api/v1/evals/${id}`)
    evals.value = evals.value.filter(e => e.id !== id)
    deletingEvalId.value = null
  } catch (e: unknown) {
    if (e instanceof Error && (e.message === 'Not found' || e.message.includes('404'))) {
      formError.value = t('views.EvalEditorView.eval_already_deleted')
    } else {
      formError.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    deleting.value = false
  }
}

async function loadAll() {
  loading.value = true
  pageError.value = null
  try {
    await loadPipelines()
  } catch (e: unknown) {
    pageError.value = `${t('views.EvalEditorView.failed_to_load')} ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
