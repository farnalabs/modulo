<template>
  <div class="h-full overflow-auto bg-muted/20 p-6" :data-testid="dataTestId">
    <div class="mb-4 flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3" :aria-label="$t('views.SystemMapDetailView.graph_controls')">
      <span class="text-sm font-medium">{{ $t('views.SystemMapDetailView.node_types') }}</span>
      <button
        v-for="type in nodeTypes"
        :key="type"
        type="button"
        class="rounded border px-2 py-1 text-xs capitalize hover:bg-accent disabled:opacity-50"
        :disabled="readOnly"
        @click="addNode(type)"
        :data-testid="`system-map-add-node-${type}`"
      >{{ type.replace('_', ' ') }}</button>
      <span class="ml-2 text-sm font-medium">{{ $t('views.SystemMapDetailView.relationship_types') }}</span>
      <label class="sr-only" :for="`${uid}-rel-source`">{{ $t('views.SystemMapDetailView.relationship_source') }}</label>
      <select
        :id="`${uid}-rel-source`"
        v-model="relationshipSource"
        class="rounded border px-2 py-1 text-xs"
        :disabled="readOnly"
        data-testid="system-map-relationship-source"
      >
        <option v-for="node in localNodes" :key="node.node_key" :value="node.node_key">{{ node.display_name }}</option>
      </select>
      <label class="sr-only" :for="`${uid}-rel-type`">{{ $t('views.SystemMapDetailView.relationship_types') }}</label>
      <select :id="`${uid}-rel-type`" v-model="relationshipType" class="rounded border px-2 py-1 text-xs" :disabled="readOnly" :aria-label="$t('views.SystemMapDetailView.relationship_type_label')" data-testid="system-map-relationship-type">
        <option v-for="type in relationshipTypes" :key="type" :value="type">{{ type.replace('_', ' ') }}</option>
      </select>
      <label class="sr-only" :for="`${uid}-rel-target`">{{ $t('views.SystemMapDetailView.relationship_target') }}</label>
      <select
        :id="`${uid}-rel-target`"
        v-model="relationshipTarget"
        class="rounded border px-2 py-1 text-xs"
        :disabled="readOnly"
        data-testid="system-map-relationship-target"
      >
        <option v-for="node in localNodes" :key="node.node_key" :value="node.node_key">{{ node.display_name }}</option>
      </select>
      <button
        type="button"
        class="rounded border px-2 py-1 text-xs disabled:opacity-50"
        :disabled="readOnly || !canAddRelationship"
        @click="addRelationship"
        data-testid="system-map-add-relationship"
      >{{ $t('views.SystemMapDetailView.add_relationship') }}</button>
    </div>

    <p class="mb-4 text-xs text-muted-foreground" aria-live="polite">{{ $t('views.SystemMapDetailView.bounded_editor_note') }}</p>

    <div class="relative min-h-[28rem] rounded-lg border bg-background p-6">
      <div v-if="localNodes.length === 0" class="absolute inset-0 flex items-center justify-center text-sm text-muted-foreground">
        {{ $t('views.SystemMapDetailView.add_node_hint') }}
      </div>
      <div
        v-for="node in localNodes"
        :key="node.node_key"
        class="absolute w-48 rounded-lg border bg-card p-3 shadow-sm"
        :style="{ left: `${nodeLeft(node)}px`, top: `${nodeTop(node)}px` }"
         :data-testid="`system-map-node-${node.node_key}`"
         :data-selected="rendererModel.nodes.find((renderNode) => renderNode.nodeKey === node.node_key)?.selected ? 'true' : 'false'"
         @click="selectedNodeKey = node.node_key"

      >
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0 flex-1">
            <span class="text-[10px] uppercase tracking-wide text-muted-foreground">{{ node.node_type }}</span>
            <input v-model="node.display_name" class="mt-1 w-full truncate bg-transparent text-sm font-medium outline-none" :aria-label="$t('views.SystemMapDetailView.node_name')" :disabled="readOnly" @change="emitNodes" />
          </div>
          <button
            type="button"
            class="cursor-move rounded p-1 text-muted-foreground hover:text-foreground disabled:cursor-default disabled:opacity-50"
            :disabled="readOnly"
            :aria-label="$t('views.SystemMapDetailView.move_node', { name: node.display_name })"
            :data-testid="`system-map-move-node-${node.node_key}`"
            @pointerdown="startDrag($event, node)"
            @keydown="onMoveKeydown($event, node)"
          >
            <span aria-hidden="true">⠿</span>
          </button>
          <button type="button" class="rounded p-1 text-muted-foreground hover:text-destructive disabled:opacity-50" :disabled="readOnly" :aria-label="$t('views.SystemMapDetailView.remove_node', { name: node.display_name })" @click="removeNode(node.node_key)" :data-testid="`system-map-remove-node-${node.node_key}`">×</button>
        </div>
        <input v-model="node.config_json.label" class="mt-2 w-full rounded border bg-background px-2 py-1 text-xs" :placeholder="$t('views.SystemMapDetailView.metadata_placeholder')" :aria-label="$t('views.SystemMapDetailView.metadata')" :disabled="readOnly" @change="emitNodes" />
      </div>

      <svg v-if="edgeLines.length" class="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
        <line
          v-for="edge in edgeLines"
          :key="edge.id"
          :x1="edge.x1"
          :y1="edge.y1"
          :x2="edge.x2"
          :y2="edge.y2"
          stroke="currentColor"
          stroke-opacity=".35"
          stroke-width="2"
        />
      </svg>
    </div>

    <div v-if="localRelationships.length" class="mt-4 rounded-lg border bg-card p-3" data-testid="system-map-relationships">
      <h2 class="text-sm font-medium">{{ $t('views.SystemMapDetailView.relationships') }}</h2>
      <ul class="mt-2 divide-y">
        <li v-for="relationship in localRelationships" :key="relationship.id" class="flex items-center justify-between py-2 text-xs">
          <span>{{ relationship.source_node_key }} → {{ relationship.target_node_key }} <span class="text-muted-foreground">({{ relationship.relationship_type }})</span></span>
          <button type="button" class="rounded px-2 py-1 text-destructive hover:bg-destructive/10 disabled:opacity-50" :disabled="readOnly" @click="removeRelationship(relationship.id)" :aria-label="$t('views.SystemMapDetailView.remove_relationship')" :data-testid="`system-map-remove-relationship-${relationship.id}`">{{ $t('views.SystemMapDetailView.remove') }}</button>
        </li>
      </ul>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, useId, watch } from 'vue'
import type { SystemMapNode, SystemMapRelationship, NodeType, RelationshipType } from '../../stores/systemMaps'
import { createSystemMapRendererModel } from '../../utils/system-map-renderer'
import type { SystemMapRendererNode } from '../../types/system-map-renderer'

const props = defineProps<{ nodes: SystemMapNode[]; relationships: SystemMapRelationship[]; loopConfigs: unknown[]; nodeTypes: readonly NodeType[]; relationshipTypes: readonly RelationshipType[]; readOnly?: boolean; dataTestId?: string }>()
const emit = defineEmits<{ (e: 'nodes-change', nodes: Array<Record<string, unknown>>): void; (e: 'relationships-change', relationships: Array<Record<string, unknown>>): void }>()

const NODE_WIDTH = 192
const NODE_HEIGHT = 96
const OFFSET = 24
const MOVE_STEP = 16

const uid = useId()
const localNodes = ref<Array<SystemMapNode & { config_json: Record<string, unknown> }>>([])
const localRelationships = ref<SystemMapRelationship[]>([])
const selectedNodeKey = ref<string | null>(null)
const nodeCounters = new Map<NodeType, number>()
const relationshipType = ref<RelationshipType>(props.relationshipTypes[0])
const relationshipSource = ref<string>('')
const relationshipTarget = ref<string>('')
const readOnly = computed(() => props.readOnly === true)
const rendererModel = computed(() => createSystemMapRendererModel(localNodes.value, localRelationships.value, { readOnly: readOnly.value, selectedNodeKey: selectedNodeKey.value }))

watch(() => props.nodes, (nodes) => {
  localNodes.value = nodes.map((node) => ({ ...node, config_json: { ...node.config_json } }))
  for (const node of nodes) {
    const nodeType = node.node_type as NodeType
    const match = node.node_key.match(new RegExp(String.raw`^${nodeType}-(\d+)$`))
    const index = match ? Number(match[1]) : 0
    nodeCounters.set(nodeType, Math.max(nodeCounters.get(nodeType) ?? 0, index))
  }
}, { immediate: true, deep: true })
watch(() => props.relationships, (relationships) => { localRelationships.value = relationships.map((relationship) => ({ ...relationship, config_json: { ...relationship.config_json } })) }, { immediate: true, deep: true })

watch(localNodes, (nodes) => {
  const keys = nodes.map((node) => node.node_key)
  if (!keys.includes(relationshipSource.value)) relationshipSource.value = keys[0] ?? ''
  if (!keys.includes(relationshipTarget.value)) relationshipTarget.value = keys[1] ?? keys[0] ?? ''
}, { immediate: true, deep: true })

const canAddRelationship = computed(() => Boolean(relationshipSource.value) && Boolean(relationshipTarget.value) && relationshipSource.value !== relationshipTarget.value)

function nodeLeft(node: SystemMapRendererNode | SystemMapNode): number { return ('x' in node ? node.x : Number(node.position_x)) + OFFSET }
function nodeTop(node: SystemMapRendererNode | SystemMapNode): number { return ('y' in node ? node.y : Number(node.position_y)) + OFFSET }

function centreFor(nodeKey: string): { x: number; y: number } | null {
  const node = rendererModel.value.nodes.find((candidate) => candidate.nodeKey === nodeKey)
  if (!node) return null
  return { x: nodeLeft(node) + NODE_WIDTH / 2, y: nodeTop(node) + NODE_HEIGHT / 2 }
}

const edgeLines = computed(() => rendererModel.value.edges.flatMap((edge) => {
  const source = centreFor(edge.source)
  const target = centreFor(edge.target)
  if (!source || !target) return []
  return [{ id: edge.id, x1: source.x, y1: source.y, x2: target.x, y2: target.y }]
}))

function emitNodes(): void { emit('nodes-change', localNodes.value as unknown as Array<Record<string, unknown>>) }
function emitRelationships(): void { emit('relationships-change', localRelationships.value as unknown as Array<Record<string, unknown>>) }

function addNode(nodeType: NodeType): void {
  const index = (nodeCounters.get(nodeType) ?? 0) + 1
  nodeCounters.set(nodeType, index)
  localNodes.value.push({
    id: `local-${Date.now()}-${nodeType}-${index}`,
    organisation_id: '',
    revision_id: '',
    node_key: `${nodeType}-${index}`,
    node_type: nodeType,
    display_name: `${nodeType.replace('_', ' ')} ${index}`,
    config_json: {},
    pipeline_id: null,
    connector_instance_id: null,
    owner_team_id: null,
    eval_id: null,
    position_x: String(40 + (index % 3) * 220),
    position_y: String(40 + Math.floor(index / 3) * 140),
    deleted_at: null,
    deleted_by: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })
  emitNodes()
}

function removeNode(nodeKey: string): void {
  localNodes.value = localNodes.value.filter((node) => node.node_key !== nodeKey)
  localRelationships.value = localRelationships.value.filter((relationship) => relationship.source_node_key !== nodeKey && relationship.target_node_key !== nodeKey)
  emitNodes()
  emitRelationships()
}

function addRelationship(): void {
  if (!canAddRelationship.value) return
  localRelationships.value.push({
    id: `local-rel-${Date.now()}`,
    organisation_id: '',
    revision_id: '',
    source_node_key: relationshipSource.value,
    target_node_key: relationshipTarget.value,
    relationship_type: relationshipType.value,
    config_json: {},
    deleted_at: null,
    deleted_by: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  })
  emitRelationships()
}

function removeRelationship(id: string): void {
  localRelationships.value = localRelationships.value.filter((relationship) => relationship.id !== id)
  emitRelationships()
}

interface DragState { nodeKey: string; startX: number; startY: number; originX: number; originY: number }
let drag: DragState | null = null

function startDrag(event: PointerEvent, node: SystemMapNode): void {
  if (readOnly.value) return
  event.preventDefault()
  drag = { nodeKey: node.node_key, startX: event.clientX, startY: event.clientY, originX: Number(node.position_x), originY: Number(node.position_y) }
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', endDrag, { once: true })
  window.addEventListener('pointercancel', endDrag, { once: true })
}

function onDragMove(event: PointerEvent): void {
  if (!drag) return
  const node = localNodes.value.find((candidate) => candidate.node_key === drag?.nodeKey)
  if (!node) return
  node.position_x = String(Math.max(0, drag.originX + event.clientX - drag.startX))
  node.position_y = String(Math.max(0, drag.originY + event.clientY - drag.startY))
}

function endDrag(): void {
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointercancel', endDrag)
  if (!drag) return
  drag = null
  emitNodes()
}

function onMoveKeydown(event: KeyboardEvent, node: SystemMapNode): void {
  if (readOnly.value) return
  const deltas: Record<string, [number, number]> = {
    ArrowLeft: [-MOVE_STEP, 0],
    ArrowRight: [MOVE_STEP, 0],
    ArrowUp: [0, -MOVE_STEP],
    ArrowDown: [0, MOVE_STEP],
  }
  const delta = deltas[event.key]
  if (!delta) return
  event.preventDefault()
  node.position_x = String(Math.max(0, Number(node.position_x) + delta[0]))
  node.position_y = String(Math.max(0, Number(node.position_y) + delta[1]))
  emitNodes()
}

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
})
</script>
