<template>
  <div class="pointer-events-none absolute inset-0">
    <div v-if="hasHitl" class="absolute -translate-y-4 translate-x-2">
      <span class="rounded bg-warning/20 px-1.5 py-0.5 text-[10px] font-medium text-warning-text" data-testid="edge-badge-hitl">
        HITL
      </span>
    </div>
    <div v-if="isLoop" class="absolute translate-y-4 translate-x-2">
      <span class="rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 dark:bg-blue-900 dark:text-blue-300" data-testid="edge-badge-loop">
        Loop{{ maxIterations ? ` (${maxIterations})` : '' }}
      </span>
    </div>
    <div v-if="isLlm" class="absolute translate-y-4 translate-x-2">
      <span class="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-medium text-purple-600 dark:bg-purple-900 dark:text-purple-300" data-testid="edge-badge-llm">
        LLM{{ routingLabel ? `: ${routingLabel}` : '' }}
      </span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface EdgeData {
  hitl_review_config?: unknown
  edge_type?: string
  max_iterations?: number
  routing_label?: string
}

const props = defineProps<{ data?: EdgeData | null }>()
const data = computed(() => props.data ?? {})
const hasHitl = computed(() => Boolean(data.value.hitl_review_config))
const isLoop = computed(() => data.value.edge_type === 'loop')
const isLlm = computed(() => data.value.edge_type === 'llm')
const maxIterations = computed(() => data.value.max_iterations ?? 0)
const routingLabel = computed(() => data.value.routing_label ?? '')
</script>
