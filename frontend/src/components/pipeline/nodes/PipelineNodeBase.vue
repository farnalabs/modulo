<template>
  <div
    :class="[
      'relative min-w-[160px] max-w-[280px] rounded-lg px-4 py-2.5 shadow-sm transition-shadow hover:shadow-md',
      'font-brand-mono text-[11px] font-medium lowercase tracking-wide',
      theme.border,
      theme.bg,
      selected ? 'ring-2 ring-primary' : '',
    ]"
    :data-testid="testId"
  >
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-1.5 min-w-0">
        <span :class="theme.labelColour">{{ glyph }}</span>
        <slot name="extra-badges" />
      </div>
      <slot name="actions" />
    </div>
    <div
      class="mt-1 text-sm font-semibold text-foreground truncate"
      :title="description || undefined"
    >
      {{ label || fallbackLabel }}
    </div>
    <div v-if="description" class="mt-0.5 line-clamp-2 text-xs text-muted-foreground">
      {{ description }}
    </div>
    <slot name="extra-content" />
    <!-- Vue Flow handles -->
    <Handle
      type="target"
      :position="Position.Left"
      class="!h-2.5 !w-2.5 !border-2 !border-border !bg-background"
    />
    <Handle
      type="source"
      :position="Position.Right"
      class="!h-2.5 !w-2.5 !border-2 !border-border !bg-background"
    />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { getNodeRegistryEntry, getNodeType } from './NodeRegistry'

interface Props {
  /** The node data from Vue Flow. */
  data: Record<string, unknown>
  /** Whether the node is currently selected. */
  selected?: boolean
  /** Unique node id. */
  id: string
  /** Optional explicit test id for product-map coverage. */
  testId?: string
}

const props = defineProps<Props>()
defineSlots<{
  /** Additional badges to show next to the glyph. */
  'extra-badges': (props: Record<string, never>) => unknown
  /** Additional actions (e.g. expand button for composites). */
  actions: (props: Record<string, never>) => unknown
  /** Extra content below the description. */
  'extra-content': (props: Record<string, never>) => unknown
}>()

const testId = computed(() => props.testId ?? `pipeline-node-${props.data.node_type ?? getNodeType(props.data)}`)

const registryEntry = computed(() => getNodeRegistryEntry((props.data.node_type as string | undefined) ?? getNodeType(props.data)))
const theme = computed(() => registryEntry.value.theme)
const glyph = computed(() => registryEntry.value.glyph)
const fallbackLabel = computed(() => registryEntry.value.fallbackLabel ?? 'Node')

const label = computed(() => props.data.label as string | undefined)
const description = computed(() => props.data.description as string | undefined)
</script>
