<template>
  <BackLink to="/library" label="Back to Library" class="ml-6" />
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>
    <div v-else-if="flowNodes.length === 0" class="flex flex-1 items-center justify-center">
      <p class="text-sm italic text-muted-foreground/60 select-none">no components in pipeline</p>
    </div>
    <template v-else>
      <div class="relative flex-1">
        <!-- Toolbar -->
        <div class="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
          <h2 class="text-sm font-semibold">{{ $t('views.PipelineEditorView.pipeline_editor') }}</h2>
          <span class="mx-2 h-4 w-px bg-border" />
          <div class="relative" @click.stop>
            <button
              class="rounded-md bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500"
              @click="showSaveAsDropdown = !showSaveAsDropdown"
            >
              Save as template
            </button>
            <div
              v-if="showSaveAsDropdown"
              class="absolute left-0 top-full mt-1 w-48 rounded-lg border bg-card py-1 shadow-lg"
            >
              <button
                class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
                @click="openSaveAsComposite"
              >
                <svg xmlns="http://www.w3.org/2000/svg" class="h-4 w-4 text-indigo-400" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v8M4.93 10.93 12 18l7.07-7.07"/><path d="M4 20h16"/></svg>
                Composite
              </button>
            </div>
          </div>
        </div>

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
          <template #edge-default="edgeProps">
            <div v-if="edgeProps.data?.hitl_gate_config" class="absolute -translate-y-4 translate-x-2">
              <span class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning">HITL</span>
            </div>
          </template>
        </VueFlow>
      </div>

      <!-- Node Properties Panel -->
      <aside v-if="selectedNodeData && !selectedEdgeData" class="w-96 overflow-y-auto border-l bg-card p-4">
        <h2 class="mb-4 text-lg font-semibold">Node Properties</h2>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">ID</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedNodeData.id) }}</dd>
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
            <dd class="font-mono text-xs">{{ shortId(selectedNodeData.output_schema_id) }}</dd>
          </div>
          <div v-if="selectedNodeData.node_type === 'agent' && selectedNodeData.agent_id">
            <dt class="text-muted-foreground">Agent</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedNodeData.agent_id) }}</dd>
          </div>
          <div v-if="selectedNodeData.node_type === 'agent' && selectedNodeData.connector_binding">
            <dt class="text-muted-foreground">Connector</dt>
            <dd class="font-mono text-xs">{{ selectedNodeData.connector_binding.type }}{{ selectedNodeData.connector_binding.instance_id ? ' / ' + shortId(selectedNodeData.connector_binding.instance_id) : '' }}</dd>
          </div>
        </dl>

        <div class="mt-6 space-y-2">
          <button
            v-if="selectedNodeData.node_type === 'manual'"
            data-testid="pipeline-editor-convert-to-agent"
            class="inline-flex w-full items-center justify-center rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90"
            @click="openAgentPicker"
          >
            Convert to Agent
          </button>
          <button
            v-if="selectedNodeData.node_type === 'agent'"
            data-testid="pipeline-editor-revert-to-manual"
            class="inline-flex w-full items-center justify-center rounded-lg border border-input bg-background px-4 py-2 text-sm font-medium hover:bg-accent"
            @click="openRevertDialog"
          >
            {{ $t('views.PipelineEditorView.revert_to_manual') }}
          </button>
        </div>
      </aside>

      <!-- Edge Properties Panel (with HITL gate config) -->
      <aside v-if="selectedEdgeData" class="w-96 overflow-y-auto border-l bg-card p-4">
        <h2 class="mb-4 text-lg font-semibold">Edge Properties</h2>
        <dl class="space-y-3 text-sm">
          <div>
            <dt class="text-muted-foreground">Source</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.source_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Target</dt>
            <dd class="font-mono text-xs">{{ shortId(selectedEdgeData.target_node_id) }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Type</dt>
            <dd>
              <select
                v-model="edgeForm.edge_type"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              >
                <option value="normal">Normal</option>
                <option value="reject">Reject</option>
                <option value="conditional">Conditional</option>
              </select>
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Condition Expression</dt>
            <dd>
              <input
                v-model="edgeForm.condition_expression"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="JMESPath expression (e.g. score > `0.5`)"
                :disabled="edgeForm.edge_type !== 'conditional'"
              />
            </dd>
          </div>
        </dl>

        <hr class="my-4 border-t" />

        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold">HITL Gate</h3>
          <label class="inline-flex cursor-pointer items-center">
            <input
              v-model="edgeForm.hitl_enabled"
              type="checkbox"
              class="h-4 w-4 rounded border-gray-300 text-primary focus:ring-primary"
            />
            <span class="ml-2 text-xs text-muted-foreground">Enabled</span>
          </label>
        </div>

        <div v-if="edgeForm.hitl_enabled" class="mt-4 space-y-4">
          <div>
            <label class="mb-1 block text-xs font-medium text-muted-foreground">Label</label>
            <input
              v-model="edgeForm.label"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="e.g. Review before deploy"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-muted-foreground">Description</label>
            <textarea
              v-model="edgeForm.description"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="Describe what the reviewer should check"
              rows="2"
            />
          </div>
          <div>
            <label class="mb-1 block text-xs font-medium text-muted-foreground">Claim Expiry (minutes)</label>
            <input
              v-model.number="edgeForm.claim_expiry_minutes"
              type="number"
              min="1"
              max="1440"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            />
          </div>
          <div class="flex items-center gap-2">
            <input
              v-model="edgeForm.human_only"
              type="checkbox"
              class="h-4 w-4 rounded border-gray-300 text-primary focus:ring-primary"
            />
            <label class="text-xs text-muted-foreground">Human only (block LLM auto-approval)</label>
          </div>

          <hr class="border-t" />

          <div>
            <label class="mb-1 block text-xs font-medium text-muted-foreground">Condition Type</label>
            <select
              v-model="edgeForm.condition_type"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="none">None (always gate)</option>
              <option value="jmespath">JMESPath Expression</option>
              <option value="eval">Eval Reference</option>
            </select>
          </div>

          <div v-if="edgeForm.condition_type === 'jmespath'">
            <label class="mb-1 block text-xs font-medium text-muted-foreground">JMESPath Condition</label>
            <input
              v-model="edgeForm.condition"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
              placeholder="e.g. score > `0.5`"
            />
            <p class="mt-1 text-[10px] text-muted-foreground">
              Evaluated against pipeline state. If truthy, gate activates.
            </p>
          </div>

          <div v-if="edgeForm.condition_type === 'eval'" class="space-y-3">
            <div>
              <label class="mb-1 block text-xs font-medium text-muted-foreground">Eval Name</label>
              <input
                v-model="edgeForm.eval_name"
                class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm font-mono"
                placeholder="e.g. quality-check"
              />
            </div>
            <div class="flex gap-2">
              <div class="flex-1">
                <label class="mb-1 block text-xs font-medium text-muted-foreground">Threshold</label>
                <input
                  v-model.number="edgeForm.eval_threshold"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                />
              </div>
              <div class="flex-1">
                <label class="mb-1 block text-xs font-medium text-muted-foreground">Operator</label>
                <select
                  v-model="edgeForm.eval_operator"
                  class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
                >
                  <option value="lt">lt (score &lt; threshold)</option>
                  <option value="gt">gt (score &gt; threshold)</option>
                  <option value="lte">lte (score &le; threshold)</option>
                  <option value="gte">gte (score &ge; threshold)</option>
                  <option value="eq">eq (score == threshold)</option>
                  <option value="neq">neq (score != threshold)</option>
                </select>
              </div>
            </div>
            <p class="mt-1 text-[10px] text-muted-foreground">
              If condition is true, gate fires. If false, gate is skipped.
            </p>
          </div>

          <div class="flex gap-2 pt-2">
            <button
              data-testid="pipeline-editor-save-edge"
              class="flex-1 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              :disabled="savingEdge"
              @click="saveEdgeConfig"
            >
              {{ savingEdge ? 'Saving...' : 'Save Edge' }}
            </button>
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="selectedEdgeData = null"
            >
              Close
            </button>
          </div>
          <div v-if="edgeSaveError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ edgeSaveError }}
          </div>
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
              data-testid="pipeline-editor-agent-select"
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
              data-testid="pipeline-editor-connector-select"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">Select a connector...</option>
              <option v-for="c in eligibleConnectors" :key="c.id" :value="c.id">{{ c.name }} ({{ c.connector_type_id }})</option>
            </select>
          </div>
          <div v-if="selectedAgent">
            <label class="mb-1 block text-sm font-medium">Model Backend</label>
            <div class="rounded-lg border bg-muted px-3 py-2 text-sm">
              {{ modelBackendName || 'Loading...' }}
            </div>
          </div>
          <div v-if="selectedAgent" class="rounded-lg border bg-muted p-3 text-sm">
            <p class="text-xs text-muted-foreground">Schema</p>
            <p class="mt-0.5 font-medium">Input: {{ agentSchemaName(selectedAgent, 'input') }}</p>
            <p class="font-medium">Output: {{ agentSchemaName(selectedAgent, 'output') }}</p>
          </div>

          <div v-if="convertError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ convertError }}
          </div>

          <div class="flex justify-end gap-2">
            <button
              data-testid="pipeline-editor-cancel"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showAgentPicker = false"
            >
              Cancel
            </button>
            <button
              :disabled="!canConvert"
              data-testid="pipeline-editor-convert"
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
        <h3 class="mb-4 text-lg font-semibold">{{ $t('views.PipelineEditorView.revert_dialog_title') }}</h3>
        <div v-if="revertLoading" class="flex items-center justify-center py-8">
          <div class="h-6 w-6 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
        <div v-else class="space-y-4">
          <p class="text-sm text-muted-foreground">
            {{ $t('views.PipelineEditorView.select_snapshot_description') }}
          </p>
          <div>
            <label class="mb-1 block text-sm font-medium">{{ $t('views.PipelineEditorView.snapshot_label') }}</label>
            <select
              v-model="revertSnapshotId"
              data-testid="pipeline-editor-snapshot-select"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
            >
              <option value="">{{ $t('views.PipelineEditorView.select_snapshot_placeholder') }}</option>
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
              data-testid="pipeline-editor-revert-cancel"
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showRevertDialog = false"
            >
              {{ $t('views.PipelineEditorView.cancel') }}
            </button>
            <button
              :disabled="!revertSnapshotId"
              data-testid="pipeline-editor-revert"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="revertToManual"
            >
              {{ $t('views.PipelineEditorView.revert') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Save as composite dialog -->
    <div
      v-if="showSaveAsComposite"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      @click.self="showSaveAsComposite = false"
    >
      <div class="w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg">
        <h3 class="mb-4 text-lg font-semibold">Save as Composite</h3>
        <p class="mb-4 text-sm text-muted-foreground">
          Extracts selected nodes from this pipeline into a reusable composite template.
          Parameter placeholders (&#123;&#123;parameter.*&#125;&#125;) in agent prompts are auto-detected.
        </p>
        <div class="space-y-4">
          <div>
            <label class="mb-1 block text-sm font-medium">Name *</label>
            <input
              v-model="saveAsName"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              placeholder="My Composite"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Description</label>
            <textarea
              v-model="saveAsDescription"
              class="w-full rounded-lg border border-input bg-background px-3 py-2 text-sm"
              rows="3"
              placeholder="Optional description"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium">Selected Nodes</label>
            <div class="max-h-32 space-y-1 overflow-y-auto">
              <label
                v-for="node in rawNodes"
                :key="node.id"
                class="flex items-center gap-2 rounded-md bg-muted/30 px-3 py-1.5 text-sm"
              >
                <input
                  v-model="saveAsSelectedNodeIds"
                  type="checkbox"
                  :value="node.id"
                  class="h-4 w-4 rounded border-gray-300 text-indigo-500 focus:ring-indigo-500"
                />
                <span>{{ node.label || 'Node ' + shortId(node.id) }}</span>
              </label>
            </div>
          </div>
          <div v-if="saveAsError" class="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            {{ saveAsError }}
          </div>
          <div class="flex justify-end gap-2">
            <button
              class="rounded-lg border border-input bg-background px-4 py-2 text-sm hover:bg-accent"
              @click="showSaveAsComposite = false"
            >
              Cancel
            </button>
            <button
              :disabled="!saveAsName || saveAsSelectedNodeIds.length === 0 || saving"
              class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
              @click="handleSaveAsComposite"
            >
              {{ saving ? 'Saving...' : 'Save' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { VueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { useApi } from '../composables/useApi'
import { formatApiError } from '../lib/api/formatError'
import BackLink from '../components/BackLink.vue'
import { shortId } from '../utils/format'

const { get, post, patch } = useApi()
const route = useRoute()
const router = useRouter()
const pipelineId = route.params.id as string

const loading = ref(true)
const pageError = ref<string | null>(null)

const rawNodes = ref<any[]>([])
const rawEdges = ref<any[]>([])
const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])

const selectedNodeData = ref<any | null>(null)
const selectedEdgeData = ref<any | null>(null)
const showSaveAsDropdown = ref(false)
const nodeTypes = { agent: 'agent', manual: 'manual' }

const agents = ref<any[]>([])
const connectors = ref<any[]>([])
const modelBackends = ref<any[]>([])
const schemas = ref<any[]>([])
const snapshots = ref<any[]>([])

const showAgentPicker = ref(false)
const showRevertDialog = ref(false)
const showSaveAsComposite = ref(false)
const pickerAgentId = ref<string>('')
const pickerConnectorId = ref<string>('')
const revertSnapshotId = ref<string>('')
const convertError = ref<string | null>(null)
const revertError = ref<string | null>(null)
const revertLoading = ref(false)

const saveAsName = ref('')
const saveAsDescription = ref('')
const saveAsSelectedNodeIds = ref<string[]>([])
const saveAsError = ref<string | null>(null)
const saving = ref(false)

const savingEdge = ref(false)
const edgeSaveError = ref<string | null>(null)

const defaultEdgeForm = {
  edge_type: 'normal',
  condition_expression: '',
  hitl_enabled: false,
  label: '',
  description: '',
  claim_expiry_minutes: 15,
  human_only: false,
  condition_type: 'none',
  condition: '',
  eval_name: '',
  eval_threshold: 0.8,
  eval_operator: 'lt',
}

const edgeForm = reactive({ ...defaultEdgeForm })

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
  return s ? s.name : `${dir}_schema_id`
}

const canConvert = computed(() => pickerAgentId.value && pickerConnectorId.value)

function convertBackendNode(n: any): any {
  const nodeType = n.node_type === 'manual' ? 'manual' : 'agent'
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
    data: {
      hitl_gate_config: e.hitl_gate_config || null,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
    },
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
    pageError.value = `Failed to load graph: ${formatApiError(e)}`
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
  } catch (err) {
    console.warn('Failed to load pipeline data:', err)
  }
}

function onNodeClick(event: any) {
  selectedEdgeData.value = null
  const node = event.node
  if (!node) return
  const backendNode = rawNodes.value.find((n: any) => n.id === node.id)
  selectedNodeData.value = backendNode || null
}

function onEdgeClick(event: any) {
  selectedNodeData.value = null
  const edge = event.edge
  if (!edge) return
  const backendEdge = rawEdges.value.find((e: any) => e.id === edge.id)
  if (backendEdge) {
    selectedEdgeData.value = backendEdge
    populateEdgeForm(backendEdge)
  }
}

function populateEdgeForm(edge: any) {
  edgeForm.edge_type = edge.edge_type || 'normal'
  edgeForm.condition_expression = edge.condition_expression || ''
  const hc = edge.hitl_gate_config
  if (hc) {
    edgeForm.hitl_enabled = true
    edgeForm.label = hc.label || ''
    edgeForm.description = hc.description || ''
    edgeForm.claim_expiry_minutes = hc.claim_expiry_minutes || 15
    edgeForm.human_only = hc.human_only || false
    if (hc.condition) {
      edgeForm.condition_type = 'jmespath'
      edgeForm.condition = hc.condition
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    } else if (hc.eval_condition) {
      edgeForm.condition_type = 'eval'
      edgeForm.eval_name = hc.eval_condition.eval_name || ''
      edgeForm.eval_threshold = hc.eval_condition.threshold ?? 0.8
      edgeForm.eval_operator = hc.eval_condition.operator || 'lt'
      edgeForm.condition = ''
    } else {
      edgeForm.condition_type = 'none'
      edgeForm.condition = ''
      edgeForm.eval_name = ''
      edgeForm.eval_threshold = 0.8
      edgeForm.eval_operator = 'lt'
    }
  } else {
    Object.assign(edgeForm, { ...defaultEdgeForm })
  }
}

function buildHitlGateConfig(): any {
  if (!edgeForm.hitl_enabled) return null
  const config: any = {
    label: edgeForm.label || 'Review Gate',
    description: edgeForm.description || '',
    reject_target: selectedEdgeData.value?.hitl_gate_config?.reject_target || null,
    claim_expiry_minutes: edgeForm.claim_expiry_minutes || 15,
    human_only: edgeForm.human_only || false,
    required_team_id: selectedEdgeData.value?.hitl_gate_config?.required_team_id || null,
  }
  if (edgeForm.condition_type === 'jmespath' && edgeForm.condition) {
    config.condition = edgeForm.condition
  }
  if (edgeForm.condition_type === 'eval' && edgeForm.eval_name) {
    config.eval_condition = {
      eval_name: edgeForm.eval_name,
      threshold: edgeForm.eval_threshold,
      operator: edgeForm.eval_operator,
    }
  }
  return config
}

async function saveEdgeConfig() {
  if (!selectedEdgeData.value) return
  savingEdge.value = true
  edgeSaveError.value = null

  // Build updated edge list with the modified edge.
  const updatedEdges = rawEdges.value.map((e: any) => {
    if (e.id === selectedEdgeData.value.id) {
      return {
        id: e.id,
        source_node_id: e.source_node_id,
        target_node_id: e.target_node_id,
        edge_type: edgeForm.edge_type,
        condition_expression: edgeForm.condition_expression || null,
        hitl_gate_config: buildHitlGateConfig(),
      }
    }
    return {
      id: e.id,
      source_node_id: e.source_node_id,
      target_node_id: e.target_node_id,
      edge_type: e.edge_type || 'normal',
      condition_expression: e.condition_expression || null,
      hitl_gate_config: e.hitl_gate_config || null,
    }
  })

  try {
    await patch(`/api/v1/pipelines/${pipelineId}/graph`, {
      nodes: rawNodes.value.map((n: any) => ({
        id: n.id,
        node_type: n.node_type || 'agent',
        label: n.label || null,
        agent_id: n.agent_id || null,
        connector_binding: n.connector_binding || null,
        output_schema_id: n.output_schema_id || null,
        model_backend_id: n.model_backend_id || null,
        role: n.role || null,
        timeout_seconds: n.timeout_seconds || null,
        position: n.position || null,
      })),
      edges: updatedEdges,
    })
    await loadGraph()
    const updatedEdge = rawEdges.value.find((e: any) => e.id === selectedEdgeData.value.id)
    if (updatedEdge) {
      selectedEdgeData.value = updatedEdge
      populateEdgeForm(updatedEdge)
    }
  } catch (e: unknown) {
    edgeSaveError.value = formatApiError(e)
  } finally {
    savingEdge.value = false
  }
}

function onPaneClick() {
  selectedNodeData.value = null
  selectedEdgeData.value = null
  showSaveAsDropdown.value = false
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

function openSaveAsComposite() {
  showSaveAsDropdown.value = false
  saveAsName.value = ''
  saveAsDescription.value = ''
  saveAsSelectedNodeIds.value = rawNodes.value.map((n: any) => n.id)
  saveAsError.value = null
  showSaveAsComposite.value = true
}

async function handleSaveAsComposite() {
  if (!saveAsName.value || saveAsSelectedNodeIds.value.length === 0) return
  saving.value = true
  saveAsError.value = null
  try {
    await post(`/api/v1/pipelines/${pipelineId}/save-as-composite`, {
      name: saveAsName.value,
      description: saveAsDescription.value || null,
      selected_node_ids: saveAsSelectedNodeIds.value,
    })
    showSaveAsComposite.value = false
    router.push({ name: 'library' })
  } catch (e: unknown) {
    saveAsError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
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
    convertError.value = formatApiError(e)
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
    revertError.value = formatApiError(e)
  } finally {
    revertLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadGraph(), loadCatalog()])
  loading.value = false
})
</script>
