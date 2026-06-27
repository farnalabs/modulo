<template>
  <span
    v-if="locked"
    class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium text-muted-foreground"
    :title="tooltipText"
    data-testid="lock-icon"
  >
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" class="h-3.5 w-3.5" aria-hidden="true">
      <path fill-rule="evenodd" d="M8 1a3.5 3.5 0 0 0-3.5 3.5V6H4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-.5V4.5A3.5 3.5 0 0 0 8 1Zm2.5 5V4.5a2.5 2.5 0 0 0-5 0V6h5Z" clip-rule="evenodd" />
    </svg>
    <slot />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  locked: boolean
  tooltip?: string
}>()

const tooltipText = computed(() => props.tooltip ?? 'Locked')
</script>
