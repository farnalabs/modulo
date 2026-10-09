<template>
  <BackLink to="/library" :label="$t('views.CompositeEditorView.back_to_library')" />
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 flex-col gap-4 p-6" :aria-label="$t('views.CompositeEditorView.loading_canvas')" role="status">
      <SkeletonBlock height-class="h-8 w-64" />
      <SkeletonBlock height-class="flex-1 w-full" />
    </div>

    <div v-else-if="pageError" class="flex flex-1 items-center justify-center p-6">
      <ErrorAlert :message="pageError" :on-retry="retry" />
    </div>

    <template v-else>
      <!-- Toolbar -->
      <div class="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
        <h2 class="text-sm font-semibold">{{ compositeName || '—' }}</h2>
        <span class="mx-2 h-4 w-px bg-border" aria-hidden="true" />
        <button type="button"
          v-if="canManage"
          data-testid="composite-editor-save-as"
          class="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
          @click="showSaveAsComposite = true"
        >
          {{ $t('views.CompositeEditorView.save_as_composite_action') }}
        </button>
        <Button size="small" class="text-xs" data-testid="composite-editor-ports-toggle" @click="showPortPanel = !showPortPanel">
          {{ showPortPanel ? $t('views.CompositeEditorView.hide_ports') : $t('views.CompositeEditorView.ports') }}
        </Button>
        <button type="button"
          v-if="canManage"
          data-testid="composite-editor-publish"
          class="rounded-md bg-green-600 px-3 py-1 text-xs font-medium text-white hover:bg-green-500"
          @click="showPublishFlow = true"
        >
          {{ $t('views.CompositeEditorView.publish') }}
        </button>
      </div>

      <!-- Vue Flow Canvas -->
      <div class="relative flex-1">
        <VueFlow
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: CANVAS_EDGE_STROKE } }"
          fit-view-on-init
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
        >
          <Background :gap="20" :size="1" />
          <!-- Accessible button names for the zoom/fit controls live in one
               place: components/shared/FlowControls.vue (FAR-740). -->
          <FlowControls :show-interactive="false" />
          <template #node-manual="nodeProps">
            <div class="rounded-lg border-2 border-warning/60 bg-warning/10 px-4 py-2 shadow-sm">
              <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-warning-text">{{ $t('views.CompositeEditorView.node_manual_badge') }}</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <template #node-agent="nodeProps">
            <div class="rounded-lg border-2 border-primary/60 bg-primary/10 px-4 py-2 shadow-sm">
              <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-primary">{{ $t('views.CompositeEditorView.node_agent_badge') }}</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <template #node-composite="nodeProps">
            <div class="rounded-lg border-2 border-indigo-500/60 bg-indigo-500/10 px-4 py-2 shadow-sm">
              <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-indigo-700 dark:text-indigo-300">{{ $t('views.CompositeEditorView.node_composite_badge') }}</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <!-- FAR-1141: dispatch nodes get their own canvas badge so they never
               render as AGENT. Display only — no authoring controls. -->
          <template #node-dispatch="nodeProps">
            <div class="rounded-lg border-2 border-cyan-500/60 bg-cyan-500/10 px-4 py-2 shadow-sm">
              <div class="font-brand-mono text-[11px] font-medium lowercase tracking-wide text-cyan-600 dark:text-cyan-300">{{ $t('views.CompositeEditorView.node_dispatch_badge') }}</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
        </VueFlow>
      </div>

      <!-- Port Definition Panel -->
      <aside v-if="showPortPanel" class="w-96 overflow-y-auto border-l bg-card p-4">
        <PortDefinitionPanel
          :ports="ports"
          :node-ids="flowNodeIds"
          :nodes="rawNodes"
          @update:ports="onPortsUpdate"
        />
      </aside>
    </template>

    <!-- Save as composite dialog -->
    <div role="button" tabindex="0" :aria-label="$t('views.CompositeEditorView.close_dialog')" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
      v-if="showSaveAsComposite"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showSaveAsComposite = false"
    >
      <div class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-base font-semibold">{{ $t('views.CompositeEditorView.save_as_composite') }}</h3>
        <div class="space-y-4">
          <div>
            <label for="compositeeditorview-field-2" class="mb-1 block text-sm font-medium">{{ $t('views.CompositeEditorView.name') }}</label>
            <input id="compositeeditorview-field-2"
              v-model="saveAsName"
              data-testid="composite-save-as-name"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              :placeholder="$t('views.CompositeEditorView.name_placeholder')"
            />
          </div>
          <div>
            <label for="compositeeditorview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.CompositeEditorView.description') }}</label>
            <textarea id="compositeeditorview-field-1"
              v-model="saveAsDescription"
              data-testid="composite-save-as-description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              :placeholder="$t('views.CompositeEditorView.description_placeholder')"
            />
          </div>
          <div v-if="saveAsError" role="alert" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ saveAsError }}
          </div>
          <div class="flex justify-end gap-2">
            <button type="button"
              data-testid="composite-save-as-cancel"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showSaveAsComposite = false"
            >
              {{ $t('views.CompositeEditorView.cancel') }}
            </button>
            <Button data-testid="composite-save-as-submit" :disabled="!saveAsName || saving" @click="handleSaveAs">
              {{ saving ? $t('views.CompositeEditorView.saving') : $t('views.CompositeEditorView.save') }}
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- Publish Composite Flow -->
    <PublishCompositeFlow
      v-if="showPublishFlow"
      :composite-id="compositeId"
      :ports="ports"
      @close="showPublishFlow = false"
      @published="onPublished"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import BackLink from '../../components/BackLink.vue'
import ErrorAlert from '../../components/shared/ErrorAlert.vue'
import FlowControls from '../../components/shared/FlowControls.vue'
import SkeletonBlock from '../../components/shared/SkeletonBlock.vue'
import { useDataFetch } from '../../composables/useDataFetch'
import { shortId } from '../../utils/format'
import { CANVAS_EDGE_STROKE } from '../../constants/canvas'
import PortDefinitionPanel from '../../components/pipeline/composite/PortDefinitionPanel.vue'
import PublishCompositeFlow from '../../components/pipeline/composite/PublishCompositeFlow.vue'
import type { ParameterPort } from '../../types/pipeline'
import { formatApiError } from '../../lib/api/formatError'
import { api } from '../../lib/api/client'
import { useCurrentUser } from '../../composables/useCurrentUser'
import Button from 'primevue/button'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const compositeId = route.params.id as string

// composite-template create/update/publish require pipeline.create/update
// (operator); hide the write controls from viewers/runners (SECURITY #1461).
const { isOperator } = useCurrentUser()
const canManage = isOperator

const compositeName = ref('')
const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])
const rawNodes = ref<any[]>([])
const rawEdges = ref<any[]>([])
const ports = ref<ParameterPort[]>([])
const showPortPanel = ref(true)
const showSaveAsComposite = ref(false)
const showPublishFlow = ref(false)
const saveAsName = ref('')
const saveAsDescription = ref('')
const saveAsError = ref<string | null>(null)
const saving = ref(false)

const nodeTypes = { agent: 'agent', manual: 'manual', composite: 'composite', dispatch: 'dispatch' }

const flowNodeIds = computed(() => flowNodes.value.map((n: any) => n.id))

function resolveNodeType(nodeType: string): string {
  if (nodeType === 'manual') return 'manual'
  if (nodeType === 'composite') return 'composite'
  // FAR-1141: a dispatch node inside a composite keeps its own type — it must
  // never collapse into the generic `agent` node.
  if (nodeType === 'dispatch') return 'dispatch'
  return 'agent'
}

function convertBackendNode(n: any): any {
  const nodeType = resolveNodeType(n.node_type)
  return {
    id: n.id,
    type: nodeType,
    position: n.position || { x: 0, y: 0 },
    data: { label: n.label || t('views.CompositeEditorView.node_default_label', { id: shortId(n.id) }) },
  }
}

function convertBackendEdge(e: any, i: number): any {
  return {
    id: e.id || `edge-${i}`,
    source: e.source_node_id,
    target: e.target_node_id,
    type: 'smoothstep',
    data: { edge_type: e.edge_type || 'normal' },
  }
}

const { loading, error: pageError, load: retry } = useDataFetch(
  async () => {
    const [templateResp, editorResp] = await Promise.all([
      api.GET('/api/v1/composite-templates/{template_id}', {
        params: { path: { template_id: compositeId } },
      }).catch(() => ({ data: null })),
      api.GET('/api/v1/composite-templates/{template_id}/editor', {
        params: { path: { template_id: compositeId } },
      }).catch(() => ({ data: null })),
    ])
    const templateData = templateResp?.data ?? null
    const editorData = editorResp?.data ?? null

    const template = templateData as any
    const editor = editorData as any

    compositeName.value = template?.name ?? ''
    ports.value = (template?.parameter_ports_json ?? []).map((p: any) => ({
      id: p.id,
      name: p.name,
      label: p.label,
      description: p.description,
      type: p.type || 'string',
      required: p.required || false,
      // Canonical ParameterPort field names — the port object is round-tripped
      // by handleSaveAs, which reads default_value/multiline/options, so every
      // stored field must survive the load mapping (save-as data loss fix).
      default_value: p.default_value,
      multiline: p.multiline || false,
      options: p.options ?? null,
    }))
    rawNodes.value = editor?.nodes ?? []
    rawEdges.value = editor?.edges ?? []
    flowNodes.value = rawNodes.value.map(convertBackendNode)
    flowEdges.value = rawEdges.value.map(convertBackendEdge)

    // useDataFetch's queryFn returns `result.data`; vue-query rejects an
    // undefined query result ("data is undefined"), so always resolve with a
    // data payload.
    return { data: {} }
  },
  { initialValue: {} },
)

function onNodeClick() {
  // Node selection handled by parent if needed
}

function onEdgeClick() {
  // Edge selection handled by parent if needed
}

function onPaneClick() {
  // Deselect
}

function onPortsUpdate(updatedPorts: ParameterPort[]) {
  ports.value = updatedPorts
}

async function handleSaveAs() {
  if (!saveAsName.value) return
  saving.value = true
  saveAsError.value = null
  try {
    await api.POST('/api/v1/composite-templates', {
      body: {
        name: saveAsName.value,
        description: saveAsDescription.value || null,
        version: '1.0.0',
        sub_pipeline_graph_json: {
          nodes: rawNodes.value,
          edges: rawEdges.value,
        },
        parameter_ports_json: ports.value.map(p => ({
          id: p.id,
          name: p.name,
          label: p.label,
          description: p.description || null,
          type: p.type,
          required: p.required,
          default_value: p.default_value ?? null,
          multiline: p.multiline,
          options: p.options ?? null,
          target_injection: {
            mode: 'prompt_replace',
            node_id: '',
            injection_point: 'prompt_template',
          },
        })),
      },
    })
    showSaveAsComposite.value = false
    router.push({ name: 'library' })
  } catch (e: unknown) {
    saveAsError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function onPublished() {
  showPublishFlow.value = false
  router.push({ name: 'library' })
}

</script>
