<template>
  <div class="space-y-2">
    <h3 class="px-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Stage Types</h3>
    <div class="space-y-1.5">
      <div role="button" tabindex="0" @keydown.enter="($event.currentTarget as HTMLElement).click()" @keydown.space.prevent="($event.currentTarget as HTMLElement).click()"
        v-for="item in paletteItems"
        :key="item.type"
        draggable="true"
        class="flex cursor-grab items-center gap-3 rounded-lg border border-border bg-card px-3 py-2.5 transition-colors hover:bg-accent active:cursor-grabbing"
        @dragstart="onDragStart($event, item.type)"
      >
        <component :is="item.icon" class="h-5 w-5" :class="item.iconClass" />
        <div class="min-w-0 flex-1">
          <div class="text-sm font-medium">{{ item.label }}</div>
          <div class="text-[11px] text-muted-foreground">{{ item.description }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  Cog as CogIcon,
  Globe as GlobeIcon,
  User as UserIcon,
  CircleHelp as HelpCircleIcon,
} from '@lucide/vue'
import type { StageType } from '../../../types/lifecycleMap'

const paletteItems = [
  {
    type: 'modulo' as StageType,
    label: 'Modulo',
    description: 'Managed by a Modulo pipeline',
    icon: CogIcon,
    iconClass: 'text-primary',
  },
  {
    type: 'external' as StageType,
    label: 'External',
    description: 'Runs outside Modulo',
    icon: GlobeIcon,
    iconClass: 'text-sky-500',
  },
  {
    type: 'manual' as StageType,
    label: 'Manual',
    description: 'Human-performed step',
    icon: UserIcon,
    iconClass: 'text-amber-500',
  },
  {
    type: 'placeholder' as StageType,
    label: 'Placeholder',
    description: 'Not yet defined',
    icon: HelpCircleIcon,
    iconClass: 'text-muted-foreground',
  },
]

function onDragStart(event: DragEvent, type: StageType) {
  if (event.dataTransfer) {
    event.dataTransfer.setData('application/lifecycle-stage', type)
    event.dataTransfer.effectAllowed = 'copy'
  }
}
</script>
