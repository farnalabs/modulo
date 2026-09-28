<template>
  <FeatureGate feature-name="eval_system" required-tier="community" show-disabled>

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

              <!-- Policy Gate section -->
              <div class="rounded-lg border border-dashed border-primary/30 bg-primary/5 p-4">
                <h3 class="mb-3 text-sm font-semibold" data-test-id="policy-gate-heading">{{ $t('views.EvalEditorView.policyGate.heading') }}</h3>

                <div class="space-y-3">
                  <div class="flex items-center gap-4">
                    <label class="flex items-center gap-2 text-sm">
                      <input
                        type="radio"
                        value="warn"
                        v-model="policyGate.action"
                        data-test-id="policy-gate-action-warn"
                        class="accent-primary"
                      />
                      <span>{{ $t('views.EvalEditorView.policyGate.actionWarnLabel') }}</span>
                    </label>
                    <label class="flex items-center gap-2 text-sm">
                      <input
                        type="radio"
                        value="block"
                        v-model="policyGate.action"
                        data-test-id="policy-gate-action-block"
                        class="accent-primary"
                      />
                      <span>{{ $t('views.EvalEditorView.policyGate.actionBlockLabel') }}</span>
                    </label>
                  </div>

                  <p class="text-xs text-muted-foreground">
                    {{ policyGate.action === 'block'
                      ? $t('views.EvalEditorView.policyGate.actionBlockDescription')
                      : $t('views.EvalEditorView.policyGate.actionWarnDescription')
                    }}
                  </p>

                  <!-- Delete gate button -->
                  <div v-if="policyGate.exists" class="flex items-center gap-2">
                    <template v-if="!gateDeleteConfirming">
                      <button
                        type="button"
                        data-test-id="policy-gate-delete"
                        ref="gateDeleteBtnRef"
                        :aria-label="$t('views.EvalEditorView.policyGate.deleteAriaLabel')"
                        class="inline-flex items-center gap-1 rounded border border-destructive/30 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                        @click="gateDeleteConfirming = true"
                      >
                        <Trash2 class="h-3 w-3" aria-hidden="true" />
                      </button>
                    </template>
                    <template v-else>
                      <div
                        ref="gateDialogRef"
                        role="dialog"
                        aria-modal="true"
                        :aria-label="$t('views.EvalEditorView.policyGate.deleteConfirm')"
                        data-test-id="policy-gate-confirm-dialog"
                        class="flex items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs"
                        @keydown="onGateDialogKeydown"
                      >
                        <span>
                          {{ $t('views.EvalEditorView.policyGate.deleteConfirm') }}
                          <template v-if="policyGate.action === 'block'">
                            {{ $t('views.EvalEditorView.policyGate.deleteConfirmBlockWarning') }}
                          </template>
                        </span>
                        <button
                          type="button"
                          data-test-id="policy-gate-confirm-delete"
                          class="rounded bg-destructive px-2 py-0.5 text-xs font-medium text-destructive-foreground hover:bg-destructive/90"
                          @click="deletePolicyGate"
                          ref="gateDeleteConfirmBtnRef"
                        >
                          {{ $t('common.confirm') }}
                        </button>
                        <button
                          type="button"
                          class="rounded px-2 py-0.5 text-xs font-medium hover:bg-accent"
                          @click="cancelGateDelete"
                        >
                          {{ $t('common.no') }}
                        </button>
                      </div>
                    </template>
                  </div>

                  <!-- Gate error state -->
                  <div
                    v-if="gateError"
                    role="alert"
                    aria-live="assertive"
                    data-test-id="policy-gate-error"
                    class="flex items-center gap-2 rounded border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive"
                  >
                    <span>{{ $t('views.EvalEditorView.policyGate.errorState') }}</span>
                    <button
                      type="button"
                      data-test-id="policy-gate-retry"
                      :aria-label="$t('views.EvalEditorView.policyGate.retryAriaLabel')"
                      class="rounded bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive hover:bg-destructive/20"
                      @click="retryPolicyGate"
                    >
                      {{ $t('views.EvalEditorView.policyGate.retry') }}
                    </button>
                  </div>
                </div>
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
                  @click="handleCancel"
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
                      v-if="evalGateActions[ev.id]"
                      class="inline-block rounded px-2 py-0.5 text-xs font-medium"
                      :class="evalGateActions[ev.id] === 'block' ? 'bg-destructive/10 text-destructive' : 'bg-amber-500/10 text-amber-700 dark:text-amber-400'"
                      data-test-id="policy-gate-badge"
                    >
                      {{ evalGateActions[ev.id] === 'block' ? $t('views.EvalEditorView.policyGate.badgeBlock') : $t('views.EvalEditorView.policyGate.badgeWarn') }}
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
import { ref, reactive, computed, onMounted, nextTick, watch } from 'vue'
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
import Select from '../components/shared/AppSelect.vue'
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
})

const saving = ref(false)
const formError = ref<string | null>(null)
const formSuccess = ref<string | null>(null)

const editingEvalId = ref<string | null>(null)

const evals = ref<EvalDefinition[]>([])
const evalsLoading = ref(false)

const deletingEvalId = ref<string | null>(null)
const deleting = ref(false)

// Policy Gate state (§3.2)
const policyGate = reactive({
  action: 'warn' as 'warn' | 'block',
  exists: false,
  id: null as string | null,
  version: 1,
})
const gateSnapshot = reactive({
  action: 'warn' as 'warn' | 'block',
})
const gateError = ref(false)
// Eval id whose gate write is pending a retry (set when phase 2 fails after a
// create, where editingEvalId is still null — §3.3 retry mechanics).
const gatePendingEvalId = ref<string | null>(null)
const gateDeleteConfirming = ref(false)
const gateDeleteBtnRef = ref<HTMLElement | null>(null)
const gateDeleteConfirmBtnRef = ref<HTMLElement | null>(null)
const gateDialogRef = ref<HTMLElement | null>(null)
const evalGateActions = ref<Record<string, string>>({})

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

// §3.2 dirty-gate guard: the gate is dirty when its action differs from the
// snapshot taken on edit-start (or from the default 'warn' when creating).
const isGateDirty = computed(() => policyGate.action !== gateSnapshot.action)

function resetForm() {
  form.name = ''
  form.node_id = '__all__'
  form.eval_type = 'llm_judge'
  form.config_json = '{}'
  form.pass_threshold = 0.8
  editingEvalId.value = null
  formError.value = null
  formSuccess.value = null
  // Reset gate state (§3.2 — every lifecycle transition)
  policyGate.action = 'warn'
  policyGate.exists = false
  policyGate.id = null
  policyGate.version = 1
  gateSnapshot.action = 'warn'
  gateError.value = false
  gateDeleteConfirming.value = false
  gateDeleteBtnRef.value = null
  gateDialogRef.value = null
  gatePendingEvalId.value = null
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
    // Fetch gate actions for badge display (§6.1 / criterion 16)
    await loadGateBadges()
  } catch {
    evals.value = []
    evalsError.value = t('views.EvalEditorView.failed_to_load_evals')
  } finally {
    evalsLoading.value = false
  }
}

async function loadGateBadges() {
  const actions: Record<string, string> = {}
  await Promise.all(
    evals.value.map(async (ev) => {
      try {
        const { data } = await api.GET('/api/v1/evals/{eval_id}/policy-gate', {
          params: { path: { eval_id: ev.id } },
        })
        if (data && (data as any).action) {
          actions[ev.id] = (data as any).action
        }
      } catch {
        // 404 means no gate — skip silently
      }
    }),
  )
  evalGateActions.value = actions
}

async function fetchPolicyGate(evalId: string) {
  gateError.value = false
  gatePendingEvalId.value = null
  let gate: { action?: string; id?: string; version?: number } | null = null
  try {
    const res = await api.GET('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: evalId } },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } — it never
    // throws, so the envelope error (404 etc.) must be handled here, not in a catch.
    gate = (res.data as { action?: string; id?: string; version?: number } | null) ?? null
  } catch {
    // Network-level failure only — treat as "no gate known".
    gate = null
  }
  if (gate && typeof gate.action === 'string') {
    policyGate.action = gate.action as 'warn' | 'block'
    policyGate.exists = true
    policyGate.id = gate.id ?? null
    policyGate.version = gate.version ?? 1
    gateSnapshot.action = gate.action as 'warn' | 'block'
  } else {
    // 404 / no gate exists — reset to defaults so a previously viewed eval's
    // gate identity never leaks into this one (§3.2, criteria 13/24).
    policyGate.action = 'warn'
    policyGate.exists = false
    policyGate.id = null
    policyGate.version = 1
    gateSnapshot.action = 'warn'
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

  // Capture before any state mutation: on the create path editingEvalId is
  // null, and savedEvalId (the new id) must not be read as "was editing".
  const wasEditing = editingEvalId.value !== null
  saving.value = true
  formError.value = null
  formSuccess.value = null
  gateError.value = false

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
    pass_threshold: form.pass_threshold,
  }

  // Phase 1: save the eval
  let savedEvalId: string | null = null
  try {
    const evalId = editingEvalId.value
    if (evalId) {
      await api.PUT('/api/v1/evals/{eval_id}', {
        params: { path: { eval_id: evalId } },
        body,
      })
      savedEvalId = evalId
    } else {
      const { data: created } = await api.POST('/api/v1/evals', { body })
      savedEvalId = (created as any)?.id ?? null
    }
  } catch (e: unknown) {
    formError.value = formatApiError(e)
    saving.value = false
    return
  }

  // Phase 2: save the policy gate (if modified or new). Two-phase reporting
  // (§3.3): the eval half reports its own success even when the gate half
  // fails, and the form is NOT reset so the retry stays available.
  const gateModified = policyGate.action !== gateSnapshot.action || !policyGate.exists
  if (gateModified && savedEvalId) {
    let gateSaveFailed = false
    try {
      if (policyGate.exists && policyGate.id) {
        // Update existing gate
        const res = await api.PUT('/api/v1/evals/{eval_id}/policy-gate', {
          params: { path: { eval_id: savedEvalId } },
          body: { action: policyGate.action },
        })
        // openapi-fetch resolves non-2xx as { data: undefined, error } — it
        // never throws, so the envelope error must be checked here.
        if (res.error || !res.data) {
          gateSaveFailed = true
        } else {
          const d = res.data as any
          policyGate.id = d.id ?? policyGate.id
          policyGate.version = d.version ?? policyGate.version + 1
          gateSnapshot.action = policyGate.action
        }
      } else {
        // Create new gate
        const res = await api.POST('/api/v1/evals/{eval_id}/policy-gate', {
          params: { path: { eval_id: savedEvalId } },
          body: { action: policyGate.action },
        })
        if (res.error || !res.data) {
          gateSaveFailed = true
        } else {
          const d = res.data as any
          policyGate.id = d.id ?? null
          policyGate.exists = true
          policyGate.version = d.version ?? 1
          gateSnapshot.action = policyGate.action
        }
      }
    } catch {
      // Network-level failure — same two-phase reporting path.
      gateSaveFailed = true
    }
    if (gateSaveFailed) {
      gateError.value = true
      gatePendingEvalId.value = savedEvalId
      formSuccess.value = wasEditing
        ? t('views.EvalEditorView.eval_updated')
        : t('views.EvalEditorView.eval_created')
      await loadEvals()
      saving.value = false
      return
    }
  }

  // Reset and show success
  resetForm()
  formSuccess.value = wasEditing
    ? t('views.EvalEditorView.eval_updated')
    : t('views.EvalEditorView.eval_created')
  await loadEvals()
  saving.value = false
}

function startEdit(ev: EvalDefinition) {
  // §3.2 dirty-gate guard: prompt when switching evals with unsaved gate changes
  if (editingEvalId.value !== null && !confirmGateDirty()) return
  editingEvalId.value = ev.id
  form.name = ev.name
  form.node_id = ev.node_id ?? '__all__'
  form.eval_type = ev.eval_type
  form.config_json = JSON.stringify(ev.config_json, null, 2)
  form.pass_threshold = ev.pass_threshold ?? 0.8
  formError.value = null
  formSuccess.value = null
  // Fetch gate state for this eval (§3.2 — switching evals re-populates)
  fetchPolicyGate(ev.id)
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

// §3.3 focus trap for the delete-confirmation dialog
function onGateDialogKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    cancelGateDelete()
    return
  }
  if (e.key === 'Tab' && gateDialogRef.value) {
    const focusable = gateDialogRef.value.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )
    if (focusable.length === 0) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault()
        last.focus()
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault()
        first.focus()
      }
    }
  }
}

// §3.3 auto-focus the confirm button when the delete-confirmation dialog opens
watch(gateDeleteConfirming, (confirming) => {
  if (confirming) {
    nextTick(() => gateDeleteConfirmBtnRef.value?.focus())
  }
})

// §3.2 dirty-gate guard: returns true if the gate is dirty and the user should
// be prompted before discarding. Returns false if not dirty (proceed freely).
function confirmGateDirty(): boolean {
  if (!isGateDirty.value) return true
  return window.confirm(t('views.EvalEditorView.policyGate.unsavedChangesConfirm'))
}

function handleCancel() {
  if (!confirmGateDirty()) return
  resetForm()
}

function cancelGateDelete() {
  gateDeleteConfirming.value = false
  nextTick(() => {
    gateDeleteBtnRef.value?.focus()
  })
}

async function deletePolicyGate() {
  if (!editingEvalId.value) return
  gateDeleteConfirming.value = false
  try {
    const res = await api.DELETE('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: editingEvalId.value } },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } without
    // throwing — surface the envelope error so the 404 branch below runs.
    if (res.error) throw res.error
    policyGate.exists = false
    policyGate.id = null
    policyGate.action = 'warn'
    policyGate.version = 1
    gateSnapshot.action = 'warn'
    gateError.value = false
    // Refresh badges
    await loadGateBadges()
  } catch (e: unknown) {
    const errMsg = formatApiError(e)
    if (errMsg.toLowerCase().includes('not found') || errMsg.includes('404')) {
      // Gate already deleted
      policyGate.exists = false
      policyGate.id = null
      policyGate.action = 'warn'
      policyGate.version = 1
      gateSnapshot.action = 'warn'
    } else {
      formError.value = errMsg
    }
  }
}

async function retryPolicyGate() {
  // Retry target: the eval being edited, or — after a failed gate create on
  // the create path, where editingEvalId is still null — the eval id created
  // in phase 1 (§3.3).
  const evalId = editingEvalId.value ?? gatePendingEvalId.value
  if (!evalId) return
  try {
    // Always the update endpoint — even when the failed write was the create
    // (spec: the retry re-issues the same action via the update route).
    const res = await api.PUT('/api/v1/evals/{eval_id}/policy-gate', {
      params: { path: { eval_id: evalId } },
      body: { action: policyGate.action },
    })
    // openapi-fetch resolves non-2xx as { data: undefined, error } — a failed
    // retry keeps the error state available (§3.3 — no backoff/circuit breaker).
    if (res.error || !res.data) return
    const d = res.data as any
    policyGate.id = d.id ?? policyGate.id
    policyGate.exists = true
    policyGate.version = d.version ?? policyGate.version + 1
    gateSnapshot.action = policyGate.action
    gateError.value = false
    gatePendingEvalId.value = null
    // §6.1 refresh the eval-list badge after a successful retry
    await loadGateBadges()
  } catch {
    // Network-level failure — keep error state available (§3.3)
  }
}

onMounted(() => { planStore.fetchPlan(); loadAll() })
</script>
