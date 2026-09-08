<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Button from 'primevue/button'
import Select from 'primevue/select'
import { api } from '../../lib/api/client'

const props = defineProps<{
  agentId?: string | null
}>()

const { t } = useI18n()

interface BindingRow {
  model_backend_id: string
  target_env_var: string
  source_field: string
}

const modelBackends = ref<Array<{ id: string; name: string; provider: string }>>([])
const bindings = ref<BindingRow[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')

const newBackendId = ref<string | null>(null)
const newTargetVar = ref('')
const newSourceField = ref('api_key')

const canAdd = computed(() => Boolean(newBackendId.value && newTargetVar.value.trim() && newSourceField.value.trim()))

function addRow() {
  if (!canAdd.value) return
  const candidate = newTargetVar.value.trim()
  // The server canonicalises target_env_var to UPPERCASE on save — case
  // variants of an existing row duplicate it (409 + a confusing save). Catch
  // the case-insensitive duplicate client-side instead.
  if (bindings.value.some((row) => row.target_env_var.toUpperCase() === candidate.toUpperCase())) {
    error.value = t('views.PipelineEditorView.runner_bindings_duplicate', { target: candidate.toUpperCase() })
    return
  }
  // No client-side uppercasing: the server is the single canonicaliser.
  bindings.value.push({
    model_backend_id: newBackendId.value as string,
    target_env_var: candidate,
    source_field: newSourceField.value.trim(),
  })
  newTargetVar.value = ''
}

function removeRow(idx: number) {
  bindings.value.splice(idx, 1)
}

async function loadBindings(agentId: string) {
  loading.value = true
  error.value = ''
  try {
    const [mbRes, bindingsRes] = await Promise.all([
      api.GET('/api/v1/model-backends'),
      api.GET('/api/v1/agents/{agent_id}/bindings', {
        params: { path: { agent_id: agentId } },
      }),
    ])
    modelBackends.value = (((mbRes.data as { items?: Array<{ id: string; name: string; provider: string }> }).items ?? []) as Array<{ id: string; name: string; provider: string }>).filter((b) => b.provider)
    bindings.value = (bindingsRes.data as { items: BindingRow[] } | undefined)?.items ?? []
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const { data, error: err } = await api.PUT('/api/v1/agents/{agent_id}/bindings', {
      params: { path: { agent_id: props.agentId as string } },
      body: {
        bindings: bindings.value.map((row) => ({
          model_backend_id: row.model_backend_id,
          target_env_var: row.target_env_var,
          source_field: row.source_field,
        })),
      },
    })
    if (err) {
      // Surface the server's remediation detail (validation error message)
      // instead of swallowing it behind the generic copy.
      const detail = (err as { detail?: string } | null)?.detail
      error.value = detail || t('views.PipelineEditorView.runner_bindings_save_failed')
      return
    }
    bindings.value = (data as { items: BindingRow[] }).items
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  if (!props.agentId) {
    loading.value = false
    return
  }
  await loadBindings(props.agentId)
})

// The component can be reused across a pipeline's agents (one editor panel
// per node). Without the reset, switching agents cross-wires the PREVIOUS
// agent's rows + vars into the new agent's first save. agentId is a key of
// the editor's own tabs, so reset state AND reload for the new agent.
watch(
  () => props.agentId,
  async (agentId, previous) => {
    if (agentId === previous) return
    if (!agentId) {
      bindings.value = []
      return
    }
    // Scrap the outgoing agent's in-memory state BEFORE the reload: the
    // rows, the add-row inputs, and any stale banner all belong to it.
    bindings.value = []
    modelBackends.value = []
    newBackendId.value = null
    newTargetVar.value = ''
    newSourceField.value = 'api_key'
    error.value = ''
    await loadBindings(agentId)
  },
)
</script>

<template>
  <div v-if="agentId" data-testid="pipeline-editor-runner-bindings">
    <dt class="text-muted-foreground text-xs uppercase tracking-wider" data-testid="pipeline-editor-runner-bindings-label">
      {{ $t('views.PipelineEditorView.runner_bindings') }}
    </dt>
    <dd class="mt-1">
      <p class="text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.runner_bindings_help') }}</p>
      <p v-if="bindings.length > 0" class="mt-1 text-[11px] text-amber-500">
        {{ $t('views.PipelineEditorView.runner_bindings_local_warning') }}
      </p>
      <ul v-if="bindings.length > 0" class="mt-1 space-y-1" data-testid="pipeline-editor-runner-bindings-rows">
        <li
          v-for="(row, idx) in bindings"
          :key="row.target_env_var"
          class="flex items-center justify-between gap-2 rounded bg-muted/40 px-2 py-1 text-xs"
        >
          <span class="font-mono">{{ row.target_env_var }}&nbsp;←</span>
          <span>{{ modelBackends.find((b) => b.id === row.model_backend_id)?.name || row.model_backend_id }}</span>
          <span class="font-mono text-[10px] opacity-70">{{ row.source_field }}</span>
          <button
            type="button"
            class="text-muted-foreground hover:text-destructive"
            :aria-label="$t('views.PipelineEditorView.runner_bindings_remove')"
            :data-testid="`pipeline-editor-runner-binding-remove-${row.target_env_var}`"
            @click="removeRow(idx)"
          >&times;</button>
        </li>
      </ul>
      <p v-else-if="!loading" class="mt-1 text-[11px] text-muted-foreground">{{ $t('views.PipelineEditorView.runner_bindings_none') }}</p>
      <div class="mt-2 grid grid-cols-3 gap-1">
        <label class="col-span-1">
          <span class="sr-only">{{ $t('views.PipelineEditorView.runner_bindings_backend') }}</span>
          <Select
            v-model="newBackendId"
            :placeholder="$t('views.PipelineEditorView.runner_bindings_backend_placeholder')"
            :options="modelBackends.map((b) => ({ value: b.id, label: b.name }))"
            option-label="label"
            option-value="value"
            class="w-full text-xs"
            data-testid="pipeline-editor-runner-binding-backend"
          />
        </label>
        <input
          v-model="newTargetVar"
          :placeholder="$t('views.PipelineEditorView.runner_bindings_target_placeholder')"
          :aria-label="$t('views.PipelineEditorView.runner_bindings_target')"
          class="w-full rounded border border-input bg-background px-2 py-1 text-xs"
          data-testid="pipeline-editor-runner-binding-target-input"
          @keydown.enter.prevent="addRow"
        />
        <input
          v-model="newSourceField"
          :placeholder="$t('views.PipelineEditorView.runner_bindings_source_placeholder')"
          :aria-label="$t('views.PipelineEditorView.runner_bindings_source')"
          class="w-full rounded border border-input bg-background px-2 py-1 text-xs"
          data-testid="pipeline-editor-runner-binding-source-input"
          @keydown.enter.prevent="addRow"
        />
      </div>
      <p v-if="error" class="mt-1 text-[11px] text-destructive">{{ error }}</p>
      <div class="mt-2 flex gap-2">
        <Button
          type="button"
          size="small"
          :disabled="!canAdd"
          data-testid="pipeline-editor-runner-binding-add"
          @click="addRow"
        >{{ $t('views.PipelineEditorView.runner_bindings_add') }}</button>
        <Button
          type="button"
          size="small"
          :loading="saving"
          :disabled="loading"
          data-testid="pipeline-editor-runner-binding-save"
          @click="save"
        >{{ $t('views.PipelineEditorView.runner_bindings_save') }}</Button>
      </div>
    </dd>
  </div>
</template>
