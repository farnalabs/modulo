<template>
  <section class="border-t bg-card p-4" data-testid="system-map-attribution-bindings">
    <div class="flex items-center justify-between gap-2">
      <div>
        <h2 class="text-sm font-semibold">{{ $t('views.SystemMapDetailView.attribution_bindings') }}</h2>
        <p class="mt-1 text-xs text-muted-foreground">{{ $t('views.SystemMapDetailView.attribution_bindings_hint') }}</p>
      </div>
      <LoadingSpinner v-if="loading" />
    </div>

    <div v-if="error" class="mt-3 rounded border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive" role="alert">{{ error }}</div>

    <form v-if="!readOnly" class="mt-4 grid gap-3 md:grid-cols-4" @submit.prevent="createBinding">
      <label class="grid gap-1 text-xs">
        <span>{{ $t('views.SystemMapDetailView.binding_kind') }}</span>
        <select v-model="bindingKind" class="rounded border bg-background px-2 py-1.5" data-testid="system-map-binding-kind">
          <option value="trigger">{{ $t('views.SystemMapDetailView.trigger_binding') }}</option>
          <option value="action_pipeline">{{ $t('views.SystemMapDetailView.action_pipeline_binding') }}</option>
        </select>
      </label>
      <label class="grid gap-1 text-xs">
        <span>{{ $t('views.SystemMapDetailView.binding_node') }}</span>
        <select v-model="nodeKey" class="rounded border bg-background px-2 py-1.5" data-testid="system-map-binding-node" required>
          <option value="" disabled>{{ $t('views.SystemMapDetailView.select_node') }}</option>
          <option v-for="node in eligibleNodes" :key="node.node_key" :value="node.node_key">{{ node.display_name }}</option>
        </select>
      </label>
      <label class="grid gap-1 text-xs">
        <span>{{ $t('views.SystemMapDetailView.binding_pipeline') }}</span>
        <select v-model="pipelineId" class="rounded border bg-background px-2 py-1.5" data-testid="system-map-binding-pipeline" required>
          <option value="" disabled>{{ $t('views.SystemMapDetailView.select_pipeline') }}</option>
          <option v-for="pipeline in pipelines" :key="pipeline.id" :value="pipeline.id">{{ pipeline.name }}</option>
        </select>
      </label>
      <label v-if="bindingKind === 'trigger'" class="grid gap-1 text-xs">
        <span>{{ $t('views.SystemMapDetailView.binding_trigger') }}</span>
        <select v-model="triggerId" class="rounded border bg-background px-2 py-1.5" data-testid="system-map-binding-trigger" required>
          <option value="" disabled>{{ $t('views.SystemMapDetailView.select_trigger') }}</option>
          <option v-for="trigger in triggers" :key="trigger.id" :value="trigger.id">{{ trigger.name || trigger.trigger_type || trigger.id }}</option>
        </select>
      </label>
      <button type="submit" class="rounded border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-50 md:col-start-4" :disabled="saving || !nodeKey || !pipelineId || (bindingKind === 'trigger' && !triggerId)" data-testid="system-map-binding-create">{{ $t('views.SystemMapDetailView.add_binding') }}</button>
    </form>

    <ul v-if="bindings.length" class="mt-4 divide-y rounded border text-xs" data-testid="system-map-binding-list">
      <li v-for="binding in bindings" :key="binding.id" class="flex items-center justify-between gap-3 p-2">
        <span>{{ binding.binding_kind }} · {{ binding.node_key }} · {{ binding.pipeline_id }}<span v-if="binding.trigger_id"> · {{ binding.trigger_id }}</span></span>
        <button v-if="!readOnly" type="button" class="rounded px-2 py-1 text-destructive hover:bg-destructive/10 disabled:opacity-50" :disabled="saving" :data-testid="`system-map-binding-delete-${binding.id}`" @click="removeBinding(binding.id)">{{ $t('views.SystemMapDetailView.remove') }}</button>
      </li>
    </ul>
    <p v-else class="mt-4 text-xs text-muted-foreground" data-testid="system-map-binding-empty">{{ $t('views.SystemMapDetailView.no_attribution_bindings') }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { AttributionBinding, AttributionBindingCreate, SystemMapNode } from '../../stores/systemMaps'
import { api } from '../../lib/api/client'
import LoadingSpinner from '../shared/LoadingSpinner.vue'

interface Pipeline { id: string; name: string }
interface Trigger { id: string; name?: string | null; trigger_type?: string | null }

const props = defineProps<{
  nodes: SystemMapNode[]
  bindings: AttributionBinding[]
  readOnly?: boolean
  createHandler: (binding: AttributionBindingCreate) => Promise<void>
  deleteHandler: (bindingId: string) => Promise<void>
}>()
const { t } = useI18n()

const bindingKind = ref<'trigger' | 'action_pipeline'>('trigger')
const nodeKey = ref('')
const pipelineId = ref('')
const triggerId = ref('')
const pipelines = ref<Pipeline[]>([])
const triggers = ref<Trigger[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const readOnly = computed(() => props.readOnly === true)
const eligibleNodes = computed(() => props.nodes.filter((node) => node.node_type === (bindingKind.value === 'trigger' ? 'pipeline' : 'improvement_loop')))

watch(bindingKind, () => { nodeKey.value = ''; pipelineId.value = ''; triggerId.value = '' })
watch(pipelineId, async (value) => {
  triggerId.value = ''
  if (!value || bindingKind.value !== 'trigger') { triggers.value = []; return }
  try {
    const response = await api.GET('/api/v1/triggers', { params: { query: { pipeline_id: value, page_size: 100 } } })
    const data = response.data as { items?: Trigger[] } | undefined
    triggers.value = data?.items ?? []
  } catch (err) {
    triggers.value = []
    error.value = err instanceof Error ? err.message : t('systemMaps.errors.load_triggers')
  }
})

async function loadOptions(): Promise<void> {
  loading.value = true
  try {
    const response = await api.GET('/api/v1/pipelines', { params: { query: { page_size: 100 } } })
    const data = response.data as { items?: Pipeline[] } | undefined
    pipelines.value = data?.items ?? []
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('systemMaps.errors.load_pipelines')
  } finally {
    loading.value = false
  }
}

async function createBinding(): Promise<void> {
  if (bindingKind.value === 'trigger' && !triggerId.value) return
  saving.value = true
  error.value = null
  try {
    await props.createHandler({
      node_key: nodeKey.value,
      pipeline_id: pipelineId.value,
      trigger_id: bindingKind.value === 'trigger' ? triggerId.value : null,
      binding_kind: bindingKind.value,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('systemMaps.errors.create_attribution_binding')
  } finally {
    saving.value = false
  }
}

async function removeBinding(bindingId: string): Promise<void> {
  saving.value = true
  error.value = null
  try {
    await props.deleteHandler(bindingId)
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('systemMaps.errors.delete_attribution_binding')
  } finally {
    saving.value = false
  }
}

onMounted(loadOptions)
</script>
