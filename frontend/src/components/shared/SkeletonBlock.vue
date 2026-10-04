<template>
  <div class="animate-pulse rounded bg-muted" :class="heightClass" />
</template>

<script setup lang="ts">
// Shared pulse placeholder primitive (ux-conformance VIS-3). `heightClass` is
// REQUIRED: a default would always apply and silently fight any size utilities
// a caller passes via `class` (Tailwind wins by stylesheet order, not by
// specificity), so every call site must state its own height/width classes
// (e.g. "h-5 w-32") and vue-tsc enforces it. Margin/layout utilities that do
// not collide with sizing still belong in the caller's `class`.
defineProps<{
  heightClass: string
}>()
</script>
