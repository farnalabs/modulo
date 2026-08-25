<template>
  <BackLink to="/library" label="Back to Library" />
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>

    <template v-else>
      <!-- Toolbar -->
      <div class="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
        <h2 class="text-sm font-semibold">{{ compositeName }}</h2>
        <span class="mx-2 h-4 w-px bg-border" />
        <button type="button"
          v-if="canManage"
          class="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
          @click="showSaveAsComposite = true"
        >
          Save as composite
        </button>
        <Button size="small" class="text-xs" @click="showPortPanel = !showPortPanel">
          {{ showPortPanel ? 'Hide Ports' : 'Ports' }}
        </Button>
        <button type="button"
          v-if="canManage"
          class="rounded-md bg-green-600 px-3 py-1 text-xs font-medium text-white hover:bg-green-500"
          @click="showPublishFlow = true"
        >
          Publish
        </button>
      </div>

      <!-- Vue Flow Canvas -->
      <div class="relative flex-1">
        <VueFlow
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: '#888' } }"
          fit-view-on-init
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
        >
          <Background :gap="20" :size="1" />
          <Controls :showInteractive="false" />
          <template #node-manual="nodeProps">
            <div class="rounded-lg border-2 border-warning/60 bg-warning/10 px-4 py-2 shadow-sm">
              <div class="text-xs font-medium text-warning">MANUAL</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <template #node-agent="nodeProps">
            <div class="rounded-lg border-2 border-primary/60 bg-primary/10 px-4 py-2 shadow-sm">
              <div class="text-xs font-medium text-primary">AGENT</div>
              <div class="text-sm font-semibold">{{ nodeProps.data.label }}</div>
            </div>
          </template>
          <template #node-composite="nodeProps">
            <div class="rounded-lg border-2 border-indigo-500/60 bg-indigo-500/10 px-4 py-2 shadow-sm">
              <div class="text-xs font-medium text-indigo-400">COMPOSITE</div>
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
    <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
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
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="My Composite"
            />
          </div>
          <div>
            <label for="compositeeditorview-field-1" class="mb-1 block text-sm font-medium">{{ $t('views.CompositeEditorView.description') }}</label>
            <textarea id="compositeeditorview-field-1"
              v-model="saveAsDescription"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              placeholder="Optional description"
            />
          </div>
          <div v-if="saveAsError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ saveAsError }}
          </div>
          <div class="flex justify-end gap-2">
            <button type="button"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showSaveAsComposite = false"
            >
              Cancel
            </button>
            <Button :disabled="!saveAsName || saving" @click="handleSaveAs">
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
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
import { useRoute, useRouter } from 'vue-router'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import BackLink from '../../components/BackLink.vue'
import { useDataFetch } from '../../composables/useDataFetch'
import { shortId } from '../../utils/format'
import PortDefinitionPanel from '../../components/pipeline/composite/PortDefinitionPanel.vue'
import PublishCompositeFlow from '../../components/pipeline/composite/PublishCompositeFlow.vue'
import type { ParameterPort } from '../../types/pipeline'
import { formatApiError } from '../../lib/api/formatError'
import { api, getAccessToken } from '../../lib/api/client'
import { decodeJwtPayload } from '../../lib/jwt'
import Button from 'primevue/button'

const route = useRoute()
const router = useRouter()
const compositeId = route.params.id as string

// composite-template create/update/publish require pipeline.create/update
// (operator); hide the write controls from viewers/runners (SECURITY #1461).
const canManage = computed(() => {
  const role = (decodeJwtPayload(getAccessToken()) as Record<string, unknown> | null)?.org_role as string | undefined
  return role === 'operator' || role === 'admin'
})

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

const nodeTypes = { agent: 'agent', manual: 'manual', composite: 'composite' }

const flowNodeIds = computed(() => flowNodes.value.map((n: any) => n.id))

function convertBackendNode(n: any): any {
  const nodeType = n.node_type === 'manual' ? 'manual' : n.node_type === 'composite' ? 'composite' : 'agent'
  return {
    id: n.id,
    type: nodeType,
    position: n.position || { x: 0, y: 0 },
    data: { label: n.label || 'Node ' + shortId(n.id) },
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

const { loading, error: pageError } = useDataFetch(
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
      default: p.default_value,
    }))
    rawNodes.value = editor?.nodes ?? []
    rawEdges.value = editor?.edges ?? []
    flowNodes.value = rawNodes.value.map(convertBackendNode)
    flowEdges.value = rawEdges.value.map(convertBackendEdge)

    return {}
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
