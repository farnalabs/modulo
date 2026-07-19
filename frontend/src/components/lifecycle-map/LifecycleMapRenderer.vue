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
      :nodes-draggable="false"
      :nodes-connectable="false"
      :edges-updatable="false"
      :min-zoom="0.3"
      :max-zoom="2"
      class="bg-dot-muted"
    >
      <Background :gap="24" :size="1" />
      <Controls :show-interactive="false" position="bottom-right" />
      <template #node-stage="nodeProps">
        <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
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
        </div>
      </template>
    </VueFlow>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { MarkerType, VueFlow, type DefaultEdgeOptions } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import type { Node, Edge } from '@vue-flow/core'
import type { LifecycleMap, LifecycleMapStage, LifecycleMapTransition } from '../../stores/lifecycleMaps'

const props = defineProps<{
  mapData: LifecycleMap | null
  onModuloStageClick?: (stage: LifecycleMapStage) => void
  onExternalStageClick?: (stage: LifecycleMapStage) => void
}>()

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

const flowNodes = computed<Node<Record<string, unknown>>[]>(() => {
  if (!props.mapData) return []
  const stages = props.mapData.stages ?? []
  const cols = Math.ceil(Math.sqrt(stages.length))
  const spacingX = 300
  const spacingY = 160
  return stages.map((stage, i) => {
    const col = i % cols
    const row = Math.floor(i / cols)
    return {
      id: stage.id,
      type: 'stage',
      position: { x: col * spacingX + 40, y: row * spacingY + 40 },
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
