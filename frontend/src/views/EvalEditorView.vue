<template>
  <FeatureGate feature-name="eval_system" required-tier="team">
    <template #locked="{ tooltip }">
      <div class="mx-auto max-w-5xl space-y-8 p-6">
        <div class="mb-4 flex items-center gap-2 rounded-lg border border-warning/30 bg-warning/5 p-4 text-sm text-warning">
          <LockIcon :locked="true" :tooltip="tooltip" />
          <span>Eval system is not available on your current plan.</span>
        </div>
      </div>
    </template>

    <div class="mx-auto max-w-5xl space-y-8 p-6">
    <header>
      <h1 class="text-3xl font-bold tracking-tight">Eval Editor</h1>
      <p class="mt-1 text-muted-foreground">Create and manage evaluation definitions for your pipelines</p>
    </header>

    <LoadingSpinner v-if="loading" />

    <ErrorAlert v-else-if="pageError" :message="pageError" :on-retry="loadAll" />

    <template v-else>
      <div class="grid gap-6 lg:grid-cols-2">
        <div>
          <label class="mb-1.5 block text-sm font-medium">Pipeline</label>
          <select
            v-model="selectedPipelineId"
            data-testid="eval-editor-pipeline"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            @change="onPipelineChange"
          >
            <option value="">Select a pipeline...</option>
            <option v-for="p in pipelines" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
        </div>

        <div>
          <label class="mb-1.5 block text-sm font-medium">Node <span class="text-muted-foreground">(optional)</span></label>
          <select
            v-model="form.node_id"
            :disabled="!selectedPipelineId || nodesLoading"
            data-testid="eval-editor-node"
            class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            <option value="">All pipeline outputs</option>
            <option v-for="n in nodes" :key="n.id" :value="n.id">{{ n.label || n.node_type || n.id.slice(0, 8) }}</option>
          </select>
          <div v-if="nodesLoading" class="mt-1 text-xs text-muted-foreground">Loading nodes...</div>
          <p v-if="nodesError" class="mt-1 text-xs text-destructive">{{ nodesError }}</p>
        </div>
      </div>

      <div class="grid gap-8 lg:grid-cols-5">
        <div class="lg:col-span-3">
          <div class="rounded-lg border bg-card p-6 shadow-sm">
            <h2 class="mb-4 text-lg font-semibold">{{ editingEvalId ? 'Edit Eval' : 'New Eval' }}</h2>

            <div class="space-y-4">
              <div>
                <label class="mb-1 block text-sm font-medium">Name</label>
                <input
                  v-model="form.name"
                  type="text"
                  data-testid="eval-editor-name"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder="e.g. Response quality check"
                />
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">Eval Type</label>
                <select
                  v-model="form.eval_type"
                  data-testid="eval-editor-eval-type"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <option value="llm_judge">LLM Judge</option>
                  <option value="regex">Regex</option>
                  <option value="json_schema">JSON Schema</option>
                  <option value="custom_function">Custom Function</option>
                </select>
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">Config <span class="text-muted-foreground">(JSON)</span></label>
                <textarea
                  v-model="form.config_json"
                  rows="6"
                  data-testid="eval-editor-config"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 font-mono text-xs ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  placeholder='{ "field": "output", "instructions": "..." }'
                />
                <div v-if="configParseError" class="mt-1 text-xs text-destructive">{{ configParseError }}</div>
              </div>

              <div>
                <label class="mb-1 block text-sm font-medium">
                  Pass Threshold
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
                <label class="mb-1 block text-sm font-medium">Failure Behaviour</label>
                <div class="flex items-center gap-4">
                  <label class="flex cursor-pointer items-center gap-2 text-sm">
                    <input
                      v-model="form.failure_behaviour"
                      type="radio"
                      value="warn"
                      data-testid="eval-editor-failure-warn"
                      class="accent-primary"
                    />
                    Warn
                  </label>
                  <label class="flex cursor-pointer items-center gap-2 text-sm">
                    <input
                      v-model="form.failure_behaviour"
                      type="radio"
                      value="block"
                      data-testid="eval-editor-failure-block"
                      class="accent-primary"
                    />
                    Block
                  </label>
                </div>
                <p class="mt-1 text-xs text-muted-foreground">
                  {{ form.failure_behaviour === 'warn' ? 'Log a warning but allow the pipeline to continue.' : 'Fail the pipeline run immediately.' }}
                </p>
              </div>

              <div class="flex items-center gap-2 pt-2">
                <button
                  :disabled="!canSave || saving"
                  data-testid="eval-editor-save"
                  class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
                  @click="saveEval"
                >
                  {{ saving ? 'Saving...' : editingEvalId ? 'Update' : 'Save' }}
                </button>
                <button
                  v-if="editingEvalId"
                  data-testid="eval-editor-cancel"
                  class="rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
                  @click="resetForm"
                >
                  Cancel
                </button>
              </div>

              <div v-if="formError" class="text-sm text-destructive">{{ formError }}</div>
              <div v-if="formSuccess" class="text-sm text-success">{{ formSuccess }}</div>
            </div>
          </div>
        </div>

        <div class="lg:col-span-2">
          <h2 class="mb-4 text-lg font-semibold">Existing Evals</h2>

          <div v-if="!selectedPipelineId" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            Select a pipeline above to see its evals.
          </div>

          <div v-else-if="evalsLoading" class="flex items-center justify-center py-8">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>

          <p v-else-if="evalsError" class="text-xs text-destructive">{{ evalsError }}</p>

          <div v-else-if="evals.length === 0" class="rounded-lg border bg-card p-6 text-center text-sm text-muted-foreground">
            No evals for this pipeline yet.
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
                      node: {{ ev.node_id.slice(0, 8) }}
                    </span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-1">
                  <button
                    data-testid="eval-editor-edit"
                    aria-label="Edit"
                    class="rounded p-1 text-muted-foreground hover:bg-accent"
                    title="Edit"
                    @click="startEdit(ev)"
                  >
                    <svg class="h-4 w-4" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" />
                    </svg>
                  </button>
                  <button
                    v-if="deletingEvalId !== ev.id"
                    data-testid="eval-editor-delete"
                    aria-label="Delete"
                    class="rounded p-1 text-destructive hover:bg-destructive/10"
                    title="Delete"
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
                      {{ deleting ? '...' : 'Confirm' }}
                    </button>
                    <button
                      data-testid="eval-editor-cancel-delete"
                      class="rounded px-2 py-1 text-xs font-medium hover:bg-accent"
                      @click="deletingEvalId = null"
                    >
                      No
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
import { useApi } from '../composables/useApi'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import { usePlanStore } from '../stores/planStore'
import FeatureGate from '../components/FeatureGate.vue'
import LockIcon from '../components/LockIcon.vue'

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
    return 'Invalid JSON'
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
    const data = await get<PipelineItem[]>('/api/v1/pipelines')
    pipelines.value = data
  } catch (e: unknown) {
    pageError.value = `Failed to load pipelines: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadNodes() {
  if (!selectedPipelineId.value) {
    nodes.value = []
    return
  }
  nodesError.value = null
  nodesLoading.value = true
  try {
    const data = await get<GraphResponse>(`/api/v1/pipelines/${selectedPipelineId.value}/graph`)
    nodes.value = data.nodes ?? []
  } catch (e) {
    nodes.value = []
    nodesError.value = 'Failed to load graph nodes. Please try again.'
  } finally {
    nodesLoading.value = false
  }
}

async function loadEvals() {
  if (!selectedPipelineId.value) {
    evals.value = []
    return
  }
  evalsError.value = null
  evalsLoading.value = true
  try {
    const data = await get<EvalListResponse>(`/api/v1/evals?pipeline_id=${selectedPipelineId.value}`)
    evals.value = data.items ?? []
  } catch (e) {
    evals.value = []
    evalsError.value = 'Failed to load eval definitions. Please try again.'
  } finally {
    evalsLoading.value = false
  }
}

async function onPipelineChange() {
  resetForm()
  deletingEvalId.value = null
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
    formError.value = 'Config JSON is invalid'
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
      formSuccess.value = 'Eval updated.'
    } else {
      await post<unknown>('/api/v1/evals', body)
      formSuccess.value = 'Eval created.'
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
    formError.value = e instanceof Error ? e.message : String(e)
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
    pageError.value = `Failed to load: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
