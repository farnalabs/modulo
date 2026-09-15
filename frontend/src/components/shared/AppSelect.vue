<template>
  <Select v-bind="$attrs" :append-to="appendTo">
    <template v-for="(_, name) in $slots" #[name]="slotData">
      <slot :name="name" v-bind="slotData ?? {}" />
    </template>
  </Select>
</template>

<script setup lang="ts">
/**
 * AppSelect — thin wrapper around PrimeVue Select that defaults
 * `appendTo` to `'self'` so the overlay is rendered inside the
 * Select's own `position: relative` wrapper instead of being
 * portalled to `<body>`.  This prevents the overlay from detaching
 * to the viewport origin when the trigger lives inside a layout
 * whose offset parent differs from `<body>` (the root cause of
 * FAR-851: filter dropdowns rendering at the left edge of the
 * window).
 *
 * All Select instances across the app should use this wrapper
 * instead of importing `primevue/select` directly.
 */
import Select from 'primevue/select'

withDefaults(defineProps<{
  appendTo?: string
}>(), {
  appendTo: 'self',
})
</script>
