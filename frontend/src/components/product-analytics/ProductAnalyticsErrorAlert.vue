<template>
  <div
    v-if="store.error"
    class="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
    role="alert"
  >
    {{ store.error }}
    <button
      type="button"
      class="ml-2 underline"
      @click="store.error = null"
    >{{ dismissLabel }}</button>
  </div>
</template>

<script setup lang="ts">
import { useProductAnalyticsStore } from '../../stores/productAnalyticsStore'

defineProps<{ dismissLabel: string }>()

const store = useProductAnalyticsStore()
</script>
