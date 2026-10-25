<template>
  <span class="inline-flex items-center ml-1" aria-hidden="true">
    <svg v-if="direction === 'asc'" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 10l7-7m0 0l7 7m-7-7v18" />
    </svg>
    <svg v-else-if="direction === 'desc'" class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 14l-7 7m0 0l-7-7m7 7V5" />
    </svg>
    <svg v-else class="w-4 h-4 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16V4m0 0L3 8m4-4l4 4M17 16V4m0 0l4 4m-4-4l-4 4" />
    </svg>
  </span>
</template>

<script setup lang="ts">
interface Props {
  direction: 'asc' | 'desc' | 'none'
}

defineProps<Props>()
</script>
