<template>
  <div class="lifecycle-map-renderer w-full h-full min-h-[400px]">
    <div v-if="!mapData" class="flex items-center justify-center h-full text-muted-foreground">
      No map data provided.
    </div>
    <VueFlow
      v-else
      v-model:nodes="flowNodes"
      v-model:edges="flowEdges"
      :default-edge-options="defaultEdgeOptions"
      fit-view-on-init
      :fit-view-options="{ padding: 0.3 }"
      :nodes-draggable="true"
      :nodes-connectable="false"
      :edges-updatable="false"
      :min-zoom="0.3"
      :max-zoom="2"
      class="bg-dot-muted"
      @node-drag-stop="emitPositions"
    >
      <Background :gap="24" :size="1" />
      <Controls :show-interactive="false" position="bottom-right" />
      <template #node-stage="nodeProps">
        <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
          @keydown="onStageKeydown(nodeProps, $event)"
          :aria-label="stageNodeAriaLabel(nodeProps.data)"
          class="stage-node rounded-lg border-2 px-4 py-3 shadow-sm min-w-[180px] max-w-[260px] transition-shadow hover:shadow-md"
          :class="stageNodeClasses(nodeProps.data)"
          @click="onStageClick(nodeProps)"
        >
          <div class="flex items-center justify-between gap-2 mb-1">
            <span class="text-sm font-semibold text-foreground truncate">{{ nodeProps.data.label }}</span>
            <span
              v-if="nodeProps.data.graduated"
              class="shrink-0 inline-flex items-center gap-0.5 rounded-full bg-amber-100 dark:bg-amber-900/30 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300"
              title="Graduated stage"
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2L15.09 8.26L22 9.27L17 14.14L18.18 21.02L12 17.77L5.82 21.02L7 14.14L2 9.27L8.91 8.26L12 2Z"/></svg>
              Graduated
            </span>
            <span
              v-if="nodeProps.data.type === 'placeholder'"
              class="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
            >
              Planned
            </span>
          </div>
          <p v-if="nodeProps.data.description" class="text-xs text-muted-foreground line-clamp-2 mb-1">
            {{ nodeProps.data.description }}
          </p>
          <div v-if="nodeProps.data.ownerBadge" class="flex items-center gap-1 mt-1">
            <span class="inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
              <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
              {{ nodeProps.data.ownerBadge }}
            </span>
          </div>
          <div
            v-if="nodeJourneys(nodeProps.data.stageId).length"
            class="mt-2 flex flex-col gap-1"
          >
            <JourneyCard
              v-for="journey in nodeJourneys(nodeProps.data.stageId)"
              :key="`${journey.kind}:${journey.ref}`"
              :journey="journey"
              @click.stop
              @keydown.enter.stop
              @keydown.space.stop
              @open="onJourneyOpen(journey)"
            />
            <span
              v-if="nodeOverflowCount(nodeProps.data.stageId) > 0"
              class="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
              :title="$t('views.LifecycleMapView.journey.more_on_node_title', { count: nodeOverflowCount(nodeProps.data.stageId) })"
              data-testid="journey-overflow-chip"
            >
              {{ $t('views.LifecycleMapView.journey.more_on_node', { count: nodeOverflowCount(nodeProps.data.stageId) }) }}
            </span>
          </div>
        </div>
      </template>
    </VueFlow>
  </div>
</template>

<script lang="ts">
/**
 * Maximum journey cards rendered per stage node (FAR-742). Newest-moved
 * journeys render first; the remainder collapses into a "+N more" chip.
 */
export const MAX_CARDS_PER_NODE = 5

/**
 * Pixels a focused node moves per arrow-key press. Provides the keyboard
 * equivalent for node repositioning required by ux-conformance A11Y-3 (the
 * `:nodes-draggable="true"` drag interaction has no other keyboard path).
 */
export const NODE_NUDGE_STEP = 16
</script>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { MarkerType, VueFlow, type DefaultEdgeOptions } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import type { Node, Edge } from '@vue-flow/core'
import type { LifecycleMap, LifecycleMapStage, LifecycleMapTransition } from '../../stores/lifecycleMaps'
import type { JourneySummary } from '../../types/lifecycleMap'
import { computeLifecycleMapLayout } from '../../stores/lifecycleMaps'
import JourneyCard from './JourneyCard.vue'

const props = defineProps<{
  mapData: LifecycleMap | null
  journeys?: JourneySummary[]
  onModuloStageClick?: (stage: LifecycleMapStage) => void
  onExternalStageClick?: (stage: LifecycleMapStage) => void
  savedPositions?: Record<string, { x: number; y: number }>
}>()

const emit = defineEmits<{
  (e: 'journey-open', journey: JourneySummary): void
  (e: 'positions-changed', positions: Record<string, { x: number; y: number }>): void
}>()

const journeysByStage = computed<Record<string, JourneySummary[]>>(() => {
  const grouped: Record<string, JourneySummary[]> = {}
  for (const journey of props.journeys ?? []) {
    const stageId = journey.current_stage?.stage_id
    if (!stageId) continue
    ;(grouped[stageId] ??= []).push(journey)
  }
  return grouped
})

function journeyTimestamp(journey: JourneySummary): number {
  const parsed = Date.parse(journey.updated_at ?? '')
  return Number.isNaN(parsed) ? 0 : parsed
}

function nodeAllJourneys(stageId: unknown): JourneySummary[] {
  const list = stageId ? (journeysByStage.value[stageId as string] ?? []) : []
  // Newest-moved first (updated_at = when the journey last moved).
  return [...list].sort((a, b) => journeyTimestamp(b) - journeyTimestamp(a))
}

/** Journeys shown on a node: capped at MAX_CARDS_PER_NODE, newest first. */
function nodeJourneys(stageId: unknown): JourneySummary[] {
  return nodeAllJourneys(stageId).slice(0, MAX_CARDS_PER_NODE)
}

/** How many journeys on a node are hidden behind the "+N more" chip. */
function nodeOverflowCount(stageId: unknown): number {
  return Math.max(0, nodeAllJourneys(stageId).length - MAX_CARDS_PER_NODE)
}

function onJourneyOpen(journey: JourneySummary): void {
  emit('journey-open', journey)
}

/** Snapshot all current node positions and emit to the parent. */
function emitPositions(): void {
  const positions: Record<string, { x: number; y: number }> = {}
  for (const node of flowNodes.value) {
    const stageId = ((node.data?.stageId as string) ?? node.id) as string
    positions[stageId] = { x: node.position.x, y: node.position.y }
  }
  emit('positions-changed', positions)
}

const defaultEdgeOptions: DefaultEdgeOptions = {
  type: 'smoothstep',
  animated: false,
  style: { stroke: '#888', strokeWidth: 2 },
  markerEnd: {
    type: MarkerType.ArrowClosed,
    width: 16,
    height: 16,
    color: '#888',
  },
}

function stageNodeClasses(data: Record<string, unknown>): Record<string, boolean> {
  const type = data.type as string
  return {
    'border-blue-500 dark:border-blue-400 bg-blue-50 dark:bg-blue-950/30 cursor-pointer': type === 'modulo',
    'border-emerald-500 dark:border-emerald-400 bg-emerald-50 dark:bg-emerald-950/30 cursor-pointer border-dashed': type === 'external',
    'border-amber-500 dark:border-amber-400 bg-amber-50 dark:bg-amber-950/30 border-dotted': type === 'manual',
    'border-muted-foreground/20 bg-muted/20 border-dashed opacity-60': type === 'placeholder',
  }
}

/** Build the node list from mapData (computed for derivation, ref for mutation).
 *  Saved positions (from localStorage) take priority over explicit stage
 *  positions, which take priority over auto-layout. */
function buildNodes(): Node<Record<string, unknown>>[] {
  if (!props.mapData) return []
  const stages = props.mapData.stages ?? []
  const transitions = props.mapData.transitions ?? []
  const autoLayout = computeLifecycleMapLayout(
    stages.map((s) => ({ id: s.id })),
    transitions.map((t) => ({ source: t.source_stage_id, target: t.target_stage_id })),
  )
  return stages.map((stage) => {
    const saved = props.savedPositions?.[stage.id]
    const hasPosition = stage.x != null && stage.y != null
    const position = saved
      ? { x: saved.x, y: saved.y }
      : hasPosition
        ? { x: stage.x as number, y: stage.y as number }
        : autoLayout[stage.id] ?? { x: 0, y: 0 }
    return {
      id: stage.id,
      type: 'stage',
      position,
      data: {
        stageId: stage.id,
        label: stage.name,
        description: stage.description,
        type: stage.type,
        ownerBadge: stage.owner_badge,
        graduated: stage.graduated,
        pipelineId: stage.pipeline_id,
        externalUrl: stage.external_url,
      },
    }
  })
}

// Mutable ref so VueFlow can update positions on drag. Seeded from mapData
// (explicit positions or auto-layout); re-seeded when mapData changes (version
// switch, re-fetch). Dragged positions persist for the session so toggling
// "Show work items" or journey filters does not reset the user's arrangement.
const flowNodes = ref(buildNodes())

watch(() => [props.mapData?.id, props.mapData?.stages, props.mapData?.transitions] as const, () => {
  flowNodes.value = buildNodes()
})

const flowEdges = computed<Edge[]>(() => {
  if (!props.mapData) return []
  return (props.mapData.transitions ?? []).map((t: LifecycleMapTransition) => ({
    id: t.id,
    source: t.source_stage_id,
    target: t.target_stage_id,
    label: t.trigger_type ?? '',
    style: { stroke: '#888', strokeWidth: 2 },
    labelStyle: { fontSize: 10, fill: '#888' },
    labelBgStyle: { fill: 'transparent' },
    title: t.description ?? t.trigger_type ?? undefined,
  }))
})

/** Accessible label for a stage node: names it and documents the arrow-key reposition path. */
function stageNodeAriaLabel(data: Record<string, unknown>): string {
  const label = (data.label as string) ?? 'stage'
  return `${label}. Press arrow keys to reposition the node.`
}

/**
 * Keyboard equivalent for dragging (ux-conformance A11Y-3): when a node is
 * focused, arrow keys nudge it one step in the pressed direction. Ignores any
 * other key so Enter/Space click handling is unaffected.
 */
function onStageKeydown(nodeProps: { id: string; data: Record<string, unknown> }, event: KeyboardEvent): void {
  let dx = 0
  let dy = 0
  if (event.key === 'ArrowLeft') dx = -1
  else if (event.key === 'ArrowRight') dx = 1
  else if (event.key === 'ArrowUp') dy = -1
  else if (event.key === 'ArrowDown') dy = 1
  else return
  event.preventDefault()
  nudgeNode(nodeProps, dx, dy)
}

/**
 * Moves the target node one step in the pressed direction. Mutates the bound
 * flowNodes ref so VueFlow's v-model:nodes carries the new position for the
 * session (same persistence model as a drag).
 */
function nudgeNode(nodeProps: { id: string; data: Record<string, unknown> }, dx: number, dy: number): void {
  const nodes = flowNodes.value
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i]
    if (node.id !== nodeProps.id) continue
    const pos = node.position
    node.position = {
      x: (pos?.x ?? 0) + dx * NODE_NUDGE_STEP,
      y: (pos?.y ?? 0) + dy * NODE_NUDGE_STEP,
    }
    emitPositions()
    return
  }
}

function onStageClick(nodeProps: { id: string; data: Record<string, unknown> }): void {
  const type = nodeProps.data.type as string
  const stageId = (nodeProps.data.stageId ?? nodeProps.id) as string
  const stages = props.mapData?.stages ?? []
  const stage = stages.find((s) => s.id === stageId)

  if (!stage) return

  if (type === 'modulo' && props.onModuloStageClick) {
    props.onModuloStageClick(stage)
  } else if (type === 'external' && props.onExternalStageClick) {
    props.onExternalStageClick(stage)
  }
}
</script>
