<template>
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>
    <template v-else>
      <div class="relative flex-1">
        <VueFlow
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: false, style: { stroke: '#888' } }"
          fit-view-on-init
          @node-click="onNodeClick"
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
        </VueFlow>
      </div>

      <aside v-if="selectedNodeData" class="w-96 overflow-y-auto border-l bg-card p-4">
        <h2 class="mb-4 text-lg font-semibold">Node Properties</h2>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">ID</dt>
            <dd class="font-mono text-xs">{{ selectedNodeData.id }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Type</dt>
            <dd>
              <span
                :class="selectedNodeData.node_type === 'manual'
                  ? 'badge badge-status-warning'
                  : 'badge badge-status-primary'"
              >
                {{ selectedNodeData.node_type === 'manual' ? 'Manual' : 'Agent' }}
              </span>
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Label</dt>
            <dd>{{ selectedNodeData.label || '-' }}</dd>
          </div>
          <div v-if="selectedNodeData.node_type === 'manual' && selectedNodeData.output_schema_id">
            <dt class="text-muted-foreground">Output Schema</dt>
            <dd class="font-mono text-xs">{{ selectedNodeData.output_schema_id }}</dd>
          </div>
          <div v-if="selectedNodeData.node_type === 'agent' && selectedNodeData.agent_id">
            <dt class="text-muted-foreground">Agent</dt>
            <dd class="font-mono text-xs">{{ selectedNodeData.agent_id }}</dd>
          </div>
          <div v-if="selectedNodeData.node_type === 'agent' && selectedNodeData.connector_binding">
            <dt class="text-muted-foreground">Connector</dt>
            <dd class="font-mono text-xs">{{ selectedNodeData.connector_binding.type }} / {{ selectedNodeData.connector_binding.instance_id }}</dd>
          </div>
        </dl>

        <div class="mt-6 space-y-2">
          <button
            v-if="selectedNodeData.node_type === 'manual'"
            class="inline-flex w-full items-center justify-center rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="openAgentPicker"
          >
            Convert to Agent
          </button>
          <button
            v-if="selectedNodeData.node_type === 'agent'"
            class="inline-flex w-full items-center justify-center rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="openRevertDialog"
          >
            Revert to Manual
          </button>
        </div>
      </aside>
    </template>

    <div v-if="showAgentPicker" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showAgentPicker = false">
      <div class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-lg font-semibold">Convert to Agent</h3>
        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Agent</label>
            <select
              v-model="pickerAgentId"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              @change="onAgentChange"
            >
              <option value="">Select an agent...</option>
              <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
            </select>
          </div>
          <div v-if="selectedAgent">
            <label class="mb-1 block text-sm font-medium">Connector</label>
            <select
              v-model="pickerConnectorId"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">Select a connector...</option>
              <option v-for="c in eligibleConnectors" :key="c.id" :value="c.id">{{ c.name }} ({{ c.connector_type_id }})</option>
            </select>
          </div>
          <div v-if="selectedAgent">
            <label class="mb-1 block text-sm font-medium">Model Backend</label>
            <div class="rounded-lg border bg-muted/20 px-3 py-2 text-sm">
              {{ modelBackendName || 'Loading...' }}
            </div>
          </div>
          <div v-if="selectedAgent" class="rounded-lg border bg-muted/20 p-3 text-sm">
            <p class="text-xs text-muted-foreground">Schema</p>
            <p class="mt-0.5 font-medium">Input: {{ agentSchemaName(selectedAgent, 'input') }}</p>
            <p class="font-medium">Output: {{ agentSchemaName(selectedAgent, 'output') }}</p>
          </div>

          <div v-if="convertError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ convertError }}
          </div>

          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showAgentPicker = false"
            >
              Cancel
            </button>
            <button
              :disabled="!canConvert"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="convertToAgent"
            >
              Convert
            </button>
          </div>
        </div>
      </div>
    </div>

    <div v-if="showRevertDialog" class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" @click.self="showRevertDialog = false">
      <div class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-lg font-semibold">Revert to Manual</h3>
        <div v-if="revertLoading" class="flex items-center justify-center py-8">
          <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
        <div v-else class="space-y-4">
          <p class="text-sm text-muted-foreground">
            Select a snapshot that contains the manual configuration for this node.
          </p>
          <div>
            <label class="mb-1 block text-sm font-medium">Snapshot</label>
            <select
              v-model="revertSnapshotId"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">Select a snapshot...</option>
              <option
                v-for="s in snapshots"
                :key="s.id"
                :value="s.id"
              >
                v{{ s.snapshot_version }}{{ s.tag ? ` — ${s.tag}` : '' }}
              </option>
            </select>
          </div>

          <div v-if="revertError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ revertError }}
          </div>

          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showRevertDialog = false"
            >
              Cancel
            </button>
            <button
              :disabled="!revertSnapshotId"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="revertToManual"
            >
              Revert
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { useApi } from '../composables/useApi'

const { get, post } = useApi()
const route = useRoute()
const pipelineId = route.params.id as string

const loading = ref(true)
const pageError = ref<string | null>(null)

const rawNodes = ref<any[]>([])
const rawEdges = ref<any[]>([])
const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])

const selectedNodeData = ref<any | null>(null)
const nodeTypes = { agent: 'agent', manual: 'manual' }

const agents = ref<any[]>([])
const connectors = ref<any[]>([])
const modelBackends = ref<any[]>([])
const schemas = ref<any[]>([])
const snapshots = ref<any[]>([])

const showAgentPicker = ref(false)
const showRevertDialog = ref(false)
const pickerAgentId = ref<string>('')
const pickerConnectorId = ref<string>('')
const revertSnapshotId = ref<string>('')
const convertError = ref<string | null>(null)
const revertError = ref<string | null>(null)
const revertLoading = ref(false)

const selectedAgent = computed(() => agents.value.find(a => a.id === pickerAgentId.value) || null)

const eligibleConnectors = computed(() => {
  if (!selectedAgent.value) return []
  const refs: Array<{ connector_type: string }> = selectedAgent.value.connector_type_refs || []
  const allowedTypes = refs.map(r => r.connector_type)
  return connectors.value.filter(c => allowedTypes.includes(c.connector_type_id))
})

const modelBackendName = computed(() => {
  if (!selectedAgent.value) return ''
  const mb = modelBackends.value.find(b => b.id === selectedAgent.value.model_backend_id)
  return mb ? `${mb.display_name} (${mb.provider})` : 'Unknown'
})

function agentSchemaName(agent: any, dir: 'input' | 'output') {
  const s = schemas.value.find(s => s.id === agent[`${dir}_schema_id`])
  return s ? s.name : `${dir}_schema_id}`
}

const canConvert = computed(() => pickerAgentId.value && pickerConnectorId.value)

function convertBackendNode(n: any): any {
  const nodeType = n.node_type === 'manual' ? 'manual' : 'agent'
  return {
    id: n.id,
    type: nodeType,
    position: n.position || { x: 0, y: 0 },
    data: { label: n.label || n.id.slice(0, 8) },
  }
}

function convertBackendEdge(e: any, i: number): any {
  return {
    id: e.id || `edge-${i}`,
    source: e.source_node_id,
    target: e.target_node_id,
    type: 'smoothstep',
  }
}

async function loadGraph() {
  try {
    const result = await get<any>(`/api/v1/pipelines/${pipelineId}/graph`)
    rawNodes.value = result.nodes || []
    rawEdges.value = result.edges || []
    flowNodes.value = rawNodes.value.map(convertBackendNode)
    flowEdges.value = rawEdges.value.map(convertBackendEdge)
  } catch (e: unknown) {
    pageError.value = `Failed to load graph: ${e instanceof Error ? e.message : String(e)}`
  }
}

async function loadCatalog() {
  try {
    const [a, c, mb, s, snaps] = await Promise.all([
      get<any>('/api/v1/agents').catch(() => ({ items: [] })),
      get<any>('/api/v1/connectors').catch(() => ({ items: [] })),
      get<any>('/api/v1/model-backends').catch(() => ({ items: [] })),
      get<any>('/api/v1/schemas').catch(() => ({ items: [] })),
      get<any>(`/api/v1/pipelines/${pipelineId}/snapshots`).catch(() => ({ items: [] })),
    ])
    agents.value = a.items || []
    connectors.value = c.items || []
    modelBackends.value = mb.items || []
    schemas.value = s.items || []
    snapshots.value = (snaps.items || []).filter((sn: any) => sn.snapshot_version > 0)
  } catch {
    // Initial load may fail silently; data stays empty
  }
}

function onNodeClick(event: any) {
  const node = event.node
  if (!node) return
  const backendNode = rawNodes.value.find((n: any) => n.id === node.id)
  selectedNodeData.value = backendNode || null
}

function onPaneClick() {
  selectedNodeData.value = null
}

function openAgentPicker() {
  convertError.value = null
  pickerAgentId.value = ''
  pickerConnectorId.value = ''
  showAgentPicker.value = true
}

function openRevertDialog() {
  revertError.value = null
  revertSnapshotId.value = ''
  showRevertDialog.value = true
}

function onAgentChange() {
  pickerConnectorId.value = ''
}

async function convertToAgent() {
  if (!canConvert.value || !selectedNodeData.value) return
  convertError.value = null
  try {
    const nodeId = selectedNodeData.value.id
    await post(`/api/v1/pipelines/${pipelineId}/nodes/${nodeId}/convert-to-agent`, {
      agent_id: pickerAgentId.value,
      connector_binding: {
        type: connectors.value.find(c => c.id === pickerConnectorId.value)?.connector_type_id || '',
        instance_id: pickerConnectorId.value,
      },
      model_backend_id: selectedAgent.value?.model_backend_id,
    })
    showAgentPicker.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    convertError.value = e instanceof Error ? e.message : String(e)
  }
}

async function revertToManual() {
  if (!revertSnapshotId.value || !selectedNodeData.value) return
  revertError.value = null
  revertLoading.value = true
  try {
    const nodeId = selectedNodeData.value.id
    await post(`/api/v1/pipelines/${pipelineId}/nodes/${nodeId}/revert-to-manual?snapshot_id=${revertSnapshotId.value}`)
    showRevertDialog.value = false
    await loadGraph()
    selectedNodeData.value = rawNodes.value.find((n: any) => n.id === nodeId) || null
  } catch (e: unknown) {
    revertError.value = e instanceof Error ? e.message : String(e)
  } finally {
    revertLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadGraph(), loadCatalog()])
  loading.value = false
})
</script>
