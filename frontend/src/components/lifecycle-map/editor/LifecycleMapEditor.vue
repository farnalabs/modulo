<template>
  <div class="flex h-[calc(100vh-3.5rem)]">
    <div v-if="loading" class="flex flex-1 items-center justify-center">
      <div class="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>

    <div v-else-if="pageError" class="flex flex-1 items-center justify-center">
      <div class="rounded-lg border border-destructive/50 bg-destructive/10 p-4 text-destructive">{{ pageError }}</div>
    </div>

    <template v-else>
      <!-- Palette sidebar -->
      <aside class="w-56 shrink-0 overflow-y-auto border-r bg-card p-3">
        <StagePalette />
      </aside>

      <!-- Canvas area -->
      <div class="relative flex-1">
        <!-- Toolbar -->
        <div class="absolute left-3 right-3 top-3 z-10 flex items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
          <h2 class="text-sm font-semibold">{{ mapName }}</h2>
          <span class="mx-2 h-4 w-px bg-border" />

          <VersionHistoryDropdown
            :versions="versions"
            :current-version-id="currentVersionId"
            @select="onLoadVersion"
          />

          <span class="mx-2 h-4 w-px bg-border" />

          <button
            class="rounded-md bg-secondary px-3 py-1 text-xs font-medium text-secondary-foreground hover:bg-secondary/80"
            @click="autoLayout"
          >
            <LayersIcon class="mr-1 inline-block h-3.5 w-3.5" />
            Auto Layout
          </button>

          <div class="ml-auto flex items-center gap-2">
            <span v-if="saveError" class="text-xs text-destructive">{{ saveError }}</span>
            <Button :disabled="saving" size="small" class="text-xs" @click="handleSave">
              {{ saving ? 'Saving...' : 'Save' }}
            </Button>
          </div>
        </div>

        <!-- Vue Flow canvas -->
        <VueFlow
          v-model:nodes="flowNodes"
          v-model:edges="flowEdges"
          :node-types="nodeTypes"
          :default-edge-options="{ type: 'smoothstep', animated: true, style: { stroke: '#888', strokeWidth: 2 } }"
          fit-view-on-init
          @drop="onDrop"
          @dragover="onDragOver"
          @node-click="onNodeClick"
          @edge-click="onEdgeClick"
          @pane-click="onPaneClick"
          @connect="onConnect"
        >
          <Background :gap="20" :size="1" />
          <Controls :showInteractive="false" />
          <template #node-lifecycle-stage="nodeProps">
            <StageNode
              v-bind="nodeProps"
              :data="nodeProps.data"
              :selected="nodeProps.selected"
            />
          </template>
        </VueFlow>
      </div>

      <!-- Side panels -->
      <aside
        v-if="selectedNode"
        class="w-80 overflow-y-auto border-l bg-card p-4"
      >
        <div class="mb-4 flex items-center justify-between">
          <h3 class="text-sm font-semibold">{{ $t('components.lifecycle-map.editor.LifecycleMapEditor.stage_config') }}</h3>
          <button
            class="text-muted-foreground hover:text-foreground"
            @click="selectedNode = null"
          >
            <XIcon class="h-4 w-4" />
          </button>
        </div>
        <StageConfigPanel
          :stage-id="selectedNode.id"
          :name="selectedNodeData.name"
          :description="selectedNodeData.description"
          :stage_type="selectedNodeData.stage_type"
          :pipeline_id="selectedNodeData.pipeline_id"
          :external_url="selectedNodeData.external_url"
          :owner="selectedNodeData.owner"
          :graduated="selectedNodeData.graduated"
          :pipelines="pipelines"
          @update="onStageFieldUpdate"
          @graduate="onGraduateClick"
        />
      </aside>

      <aside
        v-else-if="selectedEdge"
        class="w-80 overflow-y-auto border-l bg-card p-4"
      >
        <div class="mb-4 flex items-center justify-between">
          <h3 class="text-sm font-semibold">{{ $t('components.lifecycle-map.editor.LifecycleMapEditor.edge_config') }}</h3>
          <button
            class="text-muted-foreground hover:text-foreground"
            @click="selectedEdge = null"
          >
            <XIcon class="h-4 w-4" />
          </button>
        </div>
        <EdgeConfigPanel
          :trigger_type="selectedEdgeData.trigger_type"
          :description="selectedEdgeData.description"
          :condition_expression="selectedEdgeData.condition_expression"
          :estimated_frequency="selectedEdgeData.estimated_frequency"
          :trigger_link="selectedEdgeData.trigger_link"
          @update="onEdgeFieldUpdate"
        />
      </aside>
    </template>

    <!-- Graduation Dialog -->
    <GraduationDialog
      :open="showGraduation"
      :stage-name="graduationStageName"
      :stage-id="graduationStageId"
      :map-id="mapId"
      :version-id="currentVersionId"
      :pipelines="pipelines"
      @close="showGraduation = false"
      @confirm="onGraduateConfirm"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { VueFlow, useVueFlow, type EdgeMouseEvent, type NodeMouseEvent } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'

import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import { X as XIcon, Layers as LayersIcon } from '@lucide/vue'
import StageNode from './StageNode.vue'
import StageConfigPanel from './StageConfigPanel.vue'
import EdgeConfigPanel from './EdgeConfigPanel.vue'
import StagePalette from './StagePalette.vue'
import GraduationDialog from './GraduationDialog.vue'
import VersionHistoryDropdown from './VersionHistoryDropdown.vue'
import { useApi } from '../../../composables/useApi'
import { formatApiError } from '../../../lib/api/formatError'
import type { StageType, TriggerType, LifecycleStage, LifecycleEdge, LifecycleMapVersion, PipelineSummary } from '../../../types/lifecycleMap'
import Button from 'primevue/button'

function genId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // Fallback for environments without randomUUID — use the CSPRNG, not Math.random.
  const arr = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(arr)
  } else {
    for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256) // NOSONAR: only when no CSPRNG exists
  }
  return Array.from(arr, (b) => b.toString(16).padStart(2, '0')).join('')
}

const props = defineProps<{
  mapId: string
}>()

const emitEvent = defineEmits<{
  saved: []
}>()

const { get, post, put } = useApi()
const { screenToFlowCoordinate } = useVueFlow()

const loading = ref(true)
const pageError = ref<string | null>(null)
const saving = ref(false)
const saveError = ref<string | null>(null)
const mapName = ref('')
const versions = ref<LifecycleMapVersion[]>([])
const currentVersionId = ref('')
const pipelines = ref<PipelineSummary[]>([])

const flowNodes = ref<any[]>([])
const flowEdges = ref<any[]>([])
const selectedNode = ref<any>(null)
const selectedEdge = ref<any>(null)
const showGraduation = ref(false)
const graduationStageId = ref('')
const graduationStageName = ref('')

const nodeTypes = {}

const selectedNodeData = computed(() => {
  if (!selectedNode.value) return { name: '', description: '', stage_type: 'placeholder' as StageType, pipeline_id: null, external_url: null, owner: null, graduated: false }
  return selectedNode.value.data || {}
})

const selectedEdgeData = computed(() => {
  if (!selectedEdge.value) return { trigger_type: 'pipeline_completed' as TriggerType, description: '', condition_expression: null, estimated_frequency: null, trigger_link: null }
  return selectedEdge.value.data || {}
})

function createFlowNode(stage: LifecycleStage): any {
  return {
    id: stage.id,
    type: 'lifecycle-stage',
    position: { x: 0, y: 0 },
    data: {
      name: stage.name,
      description: stage.description,
      stage_type: stage.stage_type,
      pipeline_id: stage.pipeline_id,
      external_url: stage.external_url,
      owner: stage.owner,
      graduated: stage.graduated,
    },
  }
}

function createFlowEdge(edge: LifecycleEdge): any {
  return {
    id: edge.id,
    source: edge.source_stage_id,
    target: edge.target_stage_id,
    type: 'smoothstep',
    animated: true,
    data: {
      trigger_type: edge.trigger_type,
      description: edge.description,
      condition_expression: edge.condition_expression,
      estimated_frequency: edge.estimated_frequency,
      trigger_link: edge.trigger_link,
    },
  }
}

function stageToBackend(node: any): LifecycleStage {
  return {
    id: node.id,
    name: node.data.name || '',
    description: node.data.description || '',
    stage_type: node.data.stage_type || 'placeholder',
    pipeline_id: node.data.pipeline_id || null,
    external_url: node.data.external_url || null,
    owner: node.data.owner || null,
    graduated: node.data.graduated || false,
  }
}

function edgeToBackend(edge: any): LifecycleEdge {
  return {
    id: edge.id,
    source_stage_id: edge.source,
    target_stage_id: edge.target,
    trigger_type: edge.data?.trigger_type || 'pipeline_completed',
    description: edge.data?.description || '',
    condition_expression: edge.data?.condition_expression || null,
    estimated_frequency: edge.data?.estimated_frequency || null,
    trigger_link: edge.data?.trigger_link || null,
  }
}

async function loadData() {
  try {
    const [mapData, pipelinesData] = await Promise.all([
      get<any>(`/api/v1/lifecycle-maps/${props.mapId}`).catch(() => null),
      get<{ items: PipelineSummary[] }>('/api/v1/pipelines?limit=200').catch(() => ({ items: [] })),
    ])
    mapName.value = mapData?.name ?? ''
    pipelines.value = pipelinesData.items || []

    if (!mapData) {
      // The map fetch failed (e.g. 404 / network error) — surface the error
      // instead of silently rendering an empty canvas (STATE-2: API failures
      // render an inline error).
      pageError.value = `Failed to load lifecycle map (${props.mapId}): map not found.`
      return
    }

    const versionList = await get<LifecycleMapVersion[]>(`/api/v1/lifecycle-maps/${props.mapId}/versions`)
    versions.value = versionList || []

    if (versionList.length > 0) {
      const latest = versionList[0]
      currentVersionId.value = latest.id
      flowNodes.value = (latest.stages || []).map(createFlowNode)
      flowEdges.value = (latest.edges || []).map(createFlowEdge)
    }
  } catch (e: unknown) {
    pageError.value = `Failed to load: ${formatApiError(e)}`
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  saveError.value = null
  try {
    const stages: LifecycleStage[] = flowNodes.value.map(stageToBackend)
    const edges: LifecycleEdge[] = flowEdges.value.map(edgeToBackend)
    const notes = `Saved ${stages.length} stages, ${edges.length} edges`

    if (currentVersionId.value) {
      await put<any>(
        `/api/v1/lifecycle-maps/${props.mapId}/versions/${currentVersionId.value}`,
        { stages, edges, notes }
      )
    } else {
      const created = await post<LifecycleMapVersion>(
        `/api/v1/lifecycle-maps/${props.mapId}/versions`,
        { stages, edges, notes }
      )
      currentVersionId.value = created.id
      versions.value.unshift(created)
    }
    emitEvent('saved')
  } catch (e: unknown) {
    saveError.value = formatApiError(e)
  } finally {
    saving.value = false
  }
}

function onLoadVersion(versionId: string) {
  const version = versions.value.find((v) => v.id === versionId)
  if (!version) return
  flowNodes.value = (version.stages ?? []).map(createFlowNode)
  flowEdges.value = (version.edges ?? []).map(createFlowEdge)
  currentVersionId.value = versionId
  selectedNode.value = null
  selectedEdge.value = null
}

function onDragOver(event: unknown) {
  if (!(event instanceof DragEvent)) return
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = 'copy'
  }
}

function onDrop(event: unknown) {
  if (!(event instanceof DragEvent)) return
  event.preventDefault()
  const type = event.dataTransfer?.getData('application/lifecycle-stage') as StageType | undefined
  if (!type) return

  const position = screenToFlowCoordinate({ x: event.clientX, y: event.clientY })
  const id = genId()
  const stage: LifecycleStage = {
    id,
    name: `New ${type.charAt(0).toUpperCase() + type.slice(1)} Stage`,
    description: '',
    stage_type: type,
    pipeline_id: null,
    external_url: null,
    owner: null,
    graduated: false,
  }
  const node = createFlowNode(stage)
  node.position = position
  flowNodes.value.push(node)
}

function onNodeClick({ node }: NodeMouseEvent) {
  selectedNode.value = node
  selectedEdge.value = null
}

function onEdgeClick({ edge }: EdgeMouseEvent) {
  selectedEdge.value = edge
  selectedNode.value = null
}

function onPaneClick() {
  selectedNode.value = null
  selectedEdge.value = null
}

function onConnect(connection: any) {
  const id = genId()
  const edge: LifecycleEdge = {
    id,
    source_stage_id: connection.source,
    target_stage_id: connection.target,
    trigger_type: 'pipeline_completed',
    description: '',
    condition_expression: null,
    estimated_frequency: null,
    trigger_link: null,
  }
  flowEdges.value.push(createFlowEdge(edge))
}

function onStageFieldUpdate(field: string, value: unknown) {
  if (!selectedNode.value) return
  const node = flowNodes.value.find((n: any) => n.id === selectedNode.value.id)
  if (node) {
    node.data = { ...node.data, [field]: value }
  }
}

function onEdgeFieldUpdate(field: string, value: unknown) {
  if (!selectedEdge.value) return
  const edge = flowEdges.value.find((e: any) => e.id === selectedEdge.value.id)
  if (edge) {
    edge.data = { ...edge.data, [field]: value }
  }
}

function onGraduateClick(data: { id: string; name: string; stage_type: StageType }) {
  graduationStageId.value = data.id
  graduationStageName.value = data.name
  showGraduation.value = true
}

async function onGraduateConfirm(stageId: string, pipelineId: string) {
  try {
    const node = flowNodes.value.find((n: any) => n.id === stageId)
    if (!node) return

    node.data.graduated = true
    node.data.stage_type = 'modulo'
    node.data.pipeline_id = pipelineId

    await handleSave()
    showGraduation.value = false
  } catch (e: unknown) {
    saveError.value = formatApiError(e)
  }
}

function autoLayout() {
  const nodes = flowNodes.value
  if (nodes.length === 0) return

  const startX = 100
  const startY = 80
  const spacingX = 280
  const spacingY = 160
  const cols = Math.ceil(Math.sqrt(nodes.length))

  nodes.forEach((node: any, index: number) => {
    const row = Math.floor(index / cols)
    const col = index % cols
    node.position = {
      x: startX + col * spacingX,
      y: startY + row * spacingY,
    }
  })
}

onMounted(loadData)

watch(() => props.mapId, () => {
  loading.value = true
  loadData()
})
</script>
