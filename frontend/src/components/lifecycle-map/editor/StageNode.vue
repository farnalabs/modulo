<template>
  <div
    :class="[
      'relative min-w-[180px] rounded-lg border-2 px-4 py-3 shadow-sm transition-shadow hover:shadow-md',
      borderClass,
      bgClass,
      selected ? 'ring-2 ring-primary' : '',
    ]"
  >
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-1.5">
        <span class="text-[10px] font-semibold uppercase tracking-wider" :class="labelClass">
          {{ stageTypeLabel }}
        </span>
        <span
          v-if="data.graduated"
          class="inline-flex items-center rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
          title="Graduated"
        >
          <ShieldCheckIcon class="mr-0.5 h-3 w-3" />
          Graduated
        </span>
      </div>
    </div>
    <div class="mt-1 text-sm font-semibold text-foreground">{{ data.name || 'Untitled Stage' }}</div>
    <div v-if="data.description" class="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
      {{ data.description }}
    </div>
    <div v-if="data.owner" class="mt-1.5 flex items-center gap-1">
      <span class="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
        {{ data.owner }}
      </span>
    </div>

    <Handle
      type="target"
      :position="Position.Top"
      class="!h-3 !w-3 !border-2 !border-border !bg-background"
    />
    <Handle
      type="source"
      :position="Position.Bottom"
      class="!h-3 !w-3 !border-2 !border-border !bg-background"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { ShieldCheck as ShieldCheckIcon } from '@lucide/vue'
import type { StageType } from '../../../types/lifecycleMap'

const props = defineProps<{
  data: {
    name: string
    description: string
    stage_type: StageType
    owner: string | null
    graduated: boolean
  }
  selected?: boolean
  id: string
}>()

const stageTypeLabel = computed(() => {
  switch (props.data.stage_type) {
    case 'modulo': return 'Modulo'
    case 'external': return 'External'
    case 'manual': return 'Manual'
    case 'placeholder': return 'Placeholder'
    default: return 'Stage'
  }
})

const borderClass = computed(() => {
  switch (props.data.stage_type) {
    case 'modulo': return 'border-primary/60'
    case 'external': return 'border-dashed border-sky-500/60'
    case 'manual': return 'border-dotted border-amber-500/60'
    case 'placeholder': return 'border border-dashed border-muted-foreground/30 bg-muted/30'
    default: return 'border-border'
  }
})

const bgClass = computed(() => {
  switch (props.data.stage_type) {
    case 'modulo': return 'bg-primary/5'
    case 'external': return 'bg-sky-500/5'
    case 'manual': return 'bg-amber-500/5'
    case 'placeholder': return 'bg-muted/20'
    default: return 'bg-card'
  }
})

const labelClass = computed(() => {
  switch (props.data.stage_type) {
    case 'modulo': return 'text-primary'
    case 'external': return 'text-sky-500'
    case 'manual': return 'text-amber-500'
    case 'placeholder': return 'text-muted-foreground'
    default: return 'text-foreground'
  }
})
</script>
