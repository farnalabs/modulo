<template>
  <div class="animate-pulse rounded bg-muted" :class="heightClass" />
</template>

<script setup lang="ts">
// Shared pulse placeholder primitive (ux-conformance VIS-3). `heightClass` is
// optional — pass Tailwind height/width classes when the caller needs a size
// the default does not cover (e.g. "h-5 w-32").
withDefaults(
  defineProps<{
    heightClass?: string
  }>(),
  {
    heightClass: 'h-4 w-full',
  },
)
</script>
